"""SSE load test (P8.10, docs/design/testing.md §8): many clients on `/api/stream` at once.

Run it from **outside** the server (the dev Pi or a laptop), against the public entry, outside
peak hours but while cars still move (the delay is measured on real status changes):

    cd backend && uv run python ../scripts/load/sse.py https://<PUBLIC_HOST> \
        --ssh <server> --out ../out/load/run.json

What it does:
1. opens `--clients` (500) streams, `--connect-rate` (100) a minute: the API allows one address
   120 public requests a minute (api.md §7), and all the clients come from this one;
2. once all are connected, measures for `--duration` (600 s): for every `status` event, on every
   client, the delivery delay = receive time here - the event's `updated_at` (set by the API
   when the counts changed), so both clocks must be synchronised (NTP; checked against the
   server's `Date` header). The events counted are the ones whose `updated_at` falls inside
   the measurement; the one each client gets on connecting is the current state, not a
   delivery;
3. every `--sample` (10 s) reads `/healthz` (`stream.clients`, `stream.dropped`) and, with
   `--docker` (and `--ssh HOST` when the API runs on another machine), the API container's
   memory and CPU from `docker stats`.

Exit 0 = **passed** (the guide's Done-when): every client connected, at least `--min-events`
status changes seen, p95 delay < `--p95` (2 s), no errors (failed or refused connects,
disconnects, stalled or malformed streams, events that didn't reach every client, clients that
5 s after the end still hadn't got the last event the server published, events dropped by the
server from a full client queue, failed `/healthz` reads) and the API's memory stable: median
of the last quarter of the measurement vs the first quarter, growth allowed up to
max(`--mem-growth-mb` 16 MB, `--mem-growth-pct` 10%).
Exit 1 = not passed, with the reasons.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import re
import resource
import shlex
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

RETRY_S = 3.0  # like the stream's own `retry: 3000`
READ_TIMEOUT_S = 45.0  # three missed pings (15 s each): the stream is dead
GRACE_S = 5.0  # after the measurement: time for its last events to reach every client
MAX_CLOCK_OFFSET_S = 1.5  # the Date header has whole seconds, so this is "more than a second off"
_UNITS = {"b": 1, "kib": 1024, "kb": 1000, "mib": 1024**2, "mb": 1000**2, "gib": 1024**3}

# ---------------------------------------------------------------------------- pure helpers


@dataclass
class SseEvent:
    event: str = "message"
    id: str | None = None
    data: str = ""


class SseParser:
    """Lines of an event stream -> events (`feed` returns one when a blank line ends it)."""

    def __init__(self) -> None:
        self._event = SseEvent()
        self._data: list[str] = []
        self._seen = False

    def feed(self, line: str) -> SseEvent | None:
        if line == "":
            if not self._seen:
                return None
            done, self._event = self._event, SseEvent()
            done.data = "\n".join(self._data)
            self._data, self._seen = [], False
            return done
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            self._event.event, self._seen = value, True
        elif name == "id":
            self._event.id, self._seen = value, True
        elif name == "data":
            self._data.append(value)
            self._seen = True
        return None  # `retry` and unknown fields are ignored


def parse_ts(text: str) -> float:
    """`2026-10-07T17:05:12.120Z` -> seconds since the epoch."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp()


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (the smallest value with at least `pct`% at or below it)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def parse_docker_stats(text: str) -> tuple[float, float] | None:
    """`"115.2MiB / 512MiB|1.53%"` (`{{.MemUsage}}|{{.CPUPerc}}`) -> (MiB, CPU %)."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([a-zA-Z]+)\s*/[^|]*\|\s*([\d.]+)%\s*", text)
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return float(m.group(1)) * _UNITS[m.group(2).lower()] / 1024**2, float(m.group(3))


def memory_verdict(mem_mb: list[float], growth_mb: float, growth_pct: float) -> dict | None:
    """First quarter vs last quarter of the samples (medians). None with fewer than 8."""
    if len(mem_mb) < 8:
        return None
    quarter = len(mem_mb) // 4
    first = statistics.median(mem_mb[:quarter])
    last = statistics.median(mem_mb[-quarter:])
    allowed = max(growth_mb, first * growth_pct / 100)
    return {
        "first_mb": round(first, 1),
        "last_mb": round(last, 1),
        "max_mb": round(max(mem_mb), 1),
        "growth_mb": round(last - first, 1),
        "allowed_mb": round(allowed, 1),
        "stable": last - first <= allowed,
    }


@dataclass
class Recorder:
    """Everything the clients and the sampler saw; `report` turns it into the result."""

    errors: Counter = field(default_factory=Counter)
    first_error: dict[str, str] = field(default_factory=dict)
    delays: dict[int, list[float]] = field(default_factory=dict)  # event id -> one per client
    samples: list[dict] = field(default_factory=list)
    connected: int = 0
    last_id: dict[int, int] = field(default_factory=dict)  # client number -> last event id
    behind: int = 0  # clients that never got the server's last event of the measurement
    window: tuple[float, float] | None = None  # measurement start / end (epoch seconds)
    _updated: dict[tuple[int, str], float] = field(default_factory=dict)

    def error(self, kind: str, detail: str = "") -> None:
        self.errors[kind] += 1
        self.first_error.setdefault(kind, detail)

    def updated_at(self, event_id: int, data: str) -> float:
        """The event's `updated_at`; parsed once per distinct payload, not once per client."""
        key = (event_id, data)
        if key not in self._updated:
            self._updated = {k: v for k, v in self._updated.items() if k[0] >= event_id - 20}
            self._updated[key] = parse_ts(json.loads(data)["updated_at"])
        return self._updated[key]

    def delivered(self, event_id: int, data: str, received: float) -> None:
        """A `status` event that reached a client at `received` (not its first one); counted
        when the event itself (its `updated_at`) belongs to the measurement."""
        if self.window is None:
            return
        sent = self.updated_at(event_id, data)
        if self.window[0] <= sent <= self.window[1]:
            self.delays.setdefault(event_id, []).append(received - sent)


def report(rec: Recorder, args: argparse.Namespace, clock_offset_s: float | None) -> dict:
    """The result and its verdict (see the module docstring for the rules)."""
    start, end = rec.window or (0.0, 0.0)
    delays = [d for per_event in rec.delays.values() for d in per_event]
    in_window = [s for s in rec.samples if start <= s["t"] <= end]
    mem = [s["mem_mb"] for s in in_window if s.get("mem_mb") is not None]
    cpu = [s["cpu_pct"] for s in in_window if s.get("cpu_pct") is not None]
    before = [s["mem_mb"] for s in rec.samples if s["t"] < start and s.get("mem_mb") is not None]
    server_clients = [s["clients"] for s in in_window if s.get("clients") is not None]
    dropped = [s["dropped"] for s in rec.samples if s.get("dropped") is not None]
    errors = dict(rec.errors)
    undelivered = sum(max(0, rec.connected - len(d)) for d in rec.delays.values())
    if undelivered:
        errors["undelivered"] = undelivered
    if rec.behind:
        errors["clients_behind"] = rec.behind
    if len(dropped) >= 2 and dropped[-1] > dropped[0]:
        errors["dropped_by_server"] = dropped[-1] - dropped[0]
    memory = memory_verdict(mem, args.mem_growth_mb, args.mem_growth_pct)
    p95 = percentile(delays, 95)

    reasons = []
    if rec.connected < args.clients:
        reasons.append(f"only {rec.connected} of {args.clients} clients connected")
    if len(rec.delays) < args.min_events:
        reasons.append(
            f"{len(rec.delays)} status change(s) during the measurement, "
            f"{args.min_events} needed: run it while the counts change"
        )
    elif p95 is None or p95 >= args.p95:
        reasons.append(f"p95 delay {p95:.3f} s, limit {args.p95:g} s")
    if errors:
        reasons.append("errors: " + ", ".join(f"{k} {v}" for k, v in sorted(errors.items())))
    if memory is None and args.docker:
        reasons.append(f"API memory: {len(mem)} docker stats sample(s) of {args.docker}, 8 needed")
    elif memory is None:
        reasons.append("API memory not measured (--docker NAME, with --ssh HOST if remote)")
    elif not memory["stable"]:
        reasons.append(
            f"API memory grew {memory['growth_mb']} MB (allowed {memory['allowed_mb']} MB)"
        )

    def r3(value: float | None) -> float | None:
        return None if value is None else round(value, 3)

    return {
        "url": args.url,
        "started": datetime.fromtimestamp(start, UTC).isoformat(timespec="seconds"),
        "duration_s": round(end - start),
        "clients": {
            "wanted": args.clients,
            "connected": rec.connected,
            "server_min": min(server_clients, default=None),
            "server_max": max(server_clients, default=None),
        },
        "events": len(rec.delays),
        "deliveries": len(delays),
        "delay_s": {
            "p50": r3(percentile(delays, 50)),
            "p95": r3(p95),
            "p99": r3(percentile(delays, 99)),
            "max": r3(max(delays, default=None)),
            "min": r3(min(delays, default=None)),
        },
        "clock_offset_s": r3(clock_offset_s),
        "errors": errors,
        "first_error": dict(rec.first_error),
        "memory": memory,
        "memory_before_mb": round(statistics.median(before), 1) if before else None,
        "cpu_pct": {"mean": round(statistics.fmean(cpu), 1), "max": max(cpu)} if cpu else None,
        "passed": not reasons,
        "reasons": reasons,
    }


def render(result: dict) -> str:
    c, d = result["clients"], result["delay_s"]
    lines = [
        f"SSE load test: {result['url']}  ({result['started']}, {result['duration_s']} s)",
        f"  clients     {c['connected']} of {c['wanted']} connected"
        f" (server saw {c['server_min']}-{c['server_max']} on the stream)",
        f"  events      {result['events']} status changes, {result['deliveries']} deliveries",
    ]
    if result["deliveries"]:
        lines.append(
            f"  delay       p50 {d['p50']:.3f} s  p95 {d['p95']:.3f} s  p99 {d['p99']:.3f} s"
            f"  max {d['max']:.3f} s"
        )
    errors = result["errors"]
    lines.append("  errors      " + (", ".join(f"{k} {v}" for k, v in errors.items()) or "none"))
    for kind, detail in result["first_error"].items():
        if detail:
            lines.append(f"              first {kind}: {detail}")
    m = result["memory"]
    if m:
        lines.append(
            f"  API memory  {m['first_mb']} -> {m['last_mb']} MB (max {m['max_mb']},"
            f" before the clients {result['memory_before_mb']})"
        )
    if result["cpu_pct"]:
        cpu = result["cpu_pct"]
        lines.append(f"  API CPU     mean {cpu['mean']}%  max {cpu['max']}%")
    lines.append("PASSED" if result["passed"] else "NOT PASSED")
    lines += [f"  - {reason}" for reason in result["reasons"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- the run


async def client(
    http: httpx.AsyncClient, url: str, rec: Recorder, number: int, ready: asyncio.Event
) -> None:
    """One phone: keeps the stream open, reconnecting like `EventSource` (each time an error)."""
    counted = False
    while True:
        wait = RETRY_S
        try:
            async with http.stream("GET", url) as response:
                if response.status_code != 200:
                    rec.error(f"http_{response.status_code}")
                    with contextlib.suppress(ValueError):
                        wait = max(wait, float(response.headers.get("Retry-After", "")))
                else:
                    parser, last_id = SseParser(), None
                    async for line in response.aiter_lines():
                        event = parser.feed(line)
                        if event is None or event.event != "status":
                            continue
                        received = time.time()
                        event_id = int(event.id or "")
                        if last_id is None:
                            if not counted:
                                counted = True
                                rec.connected += 1
                            ready.set()
                        else:
                            if event_id != last_id + 1:
                                rec.error("missed_events", f"id {last_id} then {event_id}")
                            rec.delivered(event_id, event.data, received)
                        last_id = rec.last_id[number] = event_id
                    rec.error("disconnected", "the server closed the stream")
        except httpx.ReadTimeout:
            rec.error("stalled", f"nothing for {READ_TIMEOUT_S:g} s")
        except httpx.HTTPError as exc:
            rec.error("connection", f"{type(exc).__name__}: {exc}")
        except (ValueError, KeyError) as exc:
            rec.error("malformed", f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(wait)


async def docker_stats(container: str, ssh: str | None) -> tuple[float, float] | None:
    cmd = ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}|{{.CPUPerc}}", container]
    if ssh:
        cmd = ["ssh", "-o", "BatchMode=yes", ssh, shlex.join(cmd)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except TimeoutError:
        return None
    finally:  # also when the run ends in the middle of a sample
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return parse_docker_stats(out.decode(errors="replace").strip())


async def take_sample(http: httpx.AsyncClient, args: argparse.Namespace, rec: Recorder) -> dict:
    sample: dict = {"t": time.time()}
    try:
        response = await http.get(f"{args.url}/healthz", timeout=10)
        response.raise_for_status()
        stream = response.json()["stream"]
        sample |= {k: stream[k] for k in ("clients", "published", "dropped")}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        rec.error("healthz", f"{type(exc).__name__}: {exc}")
    if args.docker:
        stats = await docker_stats(args.docker, args.ssh)
        if stats is not None:
            sample |= {"mem_mb": round(stats[0], 1), "cpu_pct": stats[1]}
    rec.samples.append(sample)
    return sample


async def sampler(http: httpx.AsyncClient, args: argparse.Namespace, rec: Recorder) -> None:
    while True:
        began = time.monotonic()
        await take_sample(http, args, rec)
        await asyncio.sleep(max(0.0, args.sample - (time.monotonic() - began)))


async def clock_offset(http: httpx.AsyncClient, url: str) -> float | None:
    """Server clock - this clock, from the `Date` header (whole seconds, so ± 1 s)."""
    sent = time.time()
    response = await http.get(f"{url}/healthz", timeout=10)
    response.raise_for_status()
    took = time.time() - sent
    try:
        server = parsedate_to_datetime(response.headers["Date"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return None
    return server + 0.5 - (sent + took / 2)


def say(text: str) -> None:
    print(time.strftime("%H:%M:%S"), text, file=sys.stderr, flush=True)


async def run(args: argparse.Namespace) -> dict:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = args.clients + 64
    if soft < need:
        resource.setrlimit(resource.RLIMIT_NOFILE, (need if hard < 0 else min(need, hard), hard))
    rec = Recorder()
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=0)
    timeout = httpx.Timeout(connect=20, read=READ_TIMEOUT_S, write=20, pool=None)
    verify = not args.insecure
    async with (
        httpx.AsyncClient(verify=verify, limits=limits, timeout=timeout) as streams,
        httpx.AsyncClient(verify=verify) as probe,
    ):
        offset = await clock_offset(probe, args.url)
        if offset is not None and abs(offset) > MAX_CLOCK_OFFSET_S:
            raise SystemExit(
                f"this clock and the server's differ by about {offset:+.0f} s: synchronise them"
                " (NTP), the delay is measured across both"
            )
        tasks = [asyncio.create_task(sampler(probe, args, rec))]
        try:
            say(f"connecting {args.clients} clients, {args.connect_rate:g} a minute")
            ready = []
            for n in range(args.clients):
                ready.append(asyncio.Event())
                tasks.append(
                    asyncio.create_task(
                        client(streams, f"{args.url}/api/stream", rec, n, ready[-1])
                    )
                )
                if (n + 1) % 100 == 0:
                    say(f"  {n + 1} started, {rec.connected} connected")
                await asyncio.sleep(60 / args.connect_rate)
            try:
                await asyncio.wait_for(asyncio.gather(*(e.wait() for e in ready)), args.settle)
            except TimeoutError:
                say(f"  still only {rec.connected} connected after {args.settle:g} s more")
            start = time.time()
            rec.window = (start, start + args.duration)
            say(f"measuring for {args.duration:g} s with {rec.connected} clients")
            while (left := rec.window[1] - time.time()) > 0:
                await asyncio.sleep(min(60, left))
                if rec.window[1] - time.time() > 1:
                    deliveries = sum(len(d) for d in rec.delays.values())
                    errors = sum(rec.errors.values())
                    say(f"  {len(rec.delays)} events, {deliveries} deliveries, errors {errors}")
            published = (await take_sample(probe, args, rec)).get("published", 0)
            await asyncio.sleep(GRACE_S)
            rec.behind = sum(rec.last_id.get(n, 0) < published for n in range(args.clients))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return report(rec, args, offset)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("url", help="the public entry, e.g. https://<PUBLIC_HOST>")
    p.add_argument("--clients", type=int, default=500)
    p.add_argument("--duration", type=float, default=600.0, help="measurement, seconds")
    p.add_argument("--connect-rate", type=float, default=100.0, help="new clients per minute")
    p.add_argument("--settle", type=float, default=60.0, help="extra wait for late connects, s")
    p.add_argument("--sample", type=float, default=10.0, help="seconds between /healthz + stats")
    p.add_argument("--docker", metavar="NAME", help="API container for docker stats")
    p.add_argument("--ssh", metavar="HOST", help="run docker stats there (implies parking-api)")
    p.add_argument("--min-events", type=int, default=10)
    p.add_argument("--p95", type=float, default=2.0, help="limit for the p95 delay, seconds")
    p.add_argument("--mem-growth-mb", type=float, default=16.0)
    p.add_argument("--mem-growth-pct", type=float, default=10.0)
    p.add_argument("--insecure", action="store_true", help="accept a test certificate")
    p.add_argument("--out", type=Path, help="write the result as JSON here too")
    p.add_argument("--json", action="store_true", help="print JSON instead of the summary")
    args = p.parse_args(argv)
    args.url = args.url.rstrip("/")
    if args.ssh and not args.docker:
        args.docker = "parking-api"
    if args.clients < 1 or args.connect_rate <= 0 or args.duration <= 0 or args.sample <= 0:
        p.error("--clients, --connect-rate, --duration and --sample must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except httpx.HTTPError as exc:
        print(f"can't reach {args.url}/healthz: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
