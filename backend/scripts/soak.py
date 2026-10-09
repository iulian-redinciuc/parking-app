"""Soak-test sampler and report (P4.11, docs/design/testing.md §8).

Standard library only, so it runs with the host's `python3` next to the Compose stack (it needs
`docker` on the host and `/sys/class/thermal`, which the containers don't see):

    # on the vision host, from the repo root; one JSON line per minute for 7 days
    nohup python3 backend/scripts/soak.py sample --out out/soak/soak.jsonl --interval 60 &
    # any time (also while it runs); exit 0 = passed, 1 = not (yet)
    python3 backend/scripts/soak.py report out/soak/soak.jsonl [--days 7] [--json]

Each sample: CPU temperature, `vcgencmd get_throttled` (if present), load, free host memory;
for every `parking-*` container its state, restart count, start time, memory and CPU (docker
stats); the API's `/healthz` (camera states, ingest counters) and `/api/status` (zone `stale`);
and how many times each worker logged "camera back after …" since the last sample (reconnects).

The report's verdict (the guide's Done-when) is **passed** when the samples span `--days`,
nothing is still down at the last sample (no unrecovered outage) and no container's memory
grew: the median of the last 24 h vs the median of the first 24 h after a 1 h warm-up, growth
allowed up to max(`--mem-growth-mb` 25 MB, `--mem-growth-pct` 10%).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

CONTAINERS = ("parking-api", "parking-vision-occupancy", "parking-tunnel")
WORKERS = ("parking-vision-occupancy", "parking-vision-flow")
THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
RECONNECT = re.compile(r"(snapshot|rtsp): camera back after")
_UNITS = {
    "b": 1,
    "kib": 1024,
    "kb": 1000,
    "mib": 1024**2,
    "mb": 1000**2,
    "gib": 1024**3,
    "gb": 1000**3,
}

WARMUP = timedelta(hours=1)
WINDOW = timedelta(hours=24)


# ---------------------------------------------------------------------------- sampling


def parse_size_mb(text: str) -> float | None:
    """`"180.3MiB"` → 180.3 (MiB). None if it can't be read."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([a-zA-Z]+)\s*", text)
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return round(float(m.group(1)) * _UNITS[m.group(2).lower()] / 1024**2, 1)


def _run(args: list[str], timeout: float = 20) -> str | None:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout + r.stderr if r.returncode == 0 else None


def host_sample(thermal: Path = THERMAL) -> dict:
    out: dict = {"temp_c": None, "throttled": None, "load1": os.getloadavg()[0]}
    with contextlib.suppress(OSError, ValueError):
        out["temp_c"] = round(int(thermal.read_text().strip()) / 1000, 1)
    t = _run(["vcgencmd", "get_throttled"], timeout=5)
    if t and "=" in t:
        out["throttled"] = t.strip().split("=", 1)[1]
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                out["mem_avail_mb"] = round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return out


def containers_sample(names: tuple[str, ...], since: float | None) -> tuple[dict, dict]:
    """Per-container state + memory/CPU, and reconnect counts from the workers' logs."""
    out: dict[str, dict] = {}
    for name in names:
        raw = _run(["docker", "inspect", "--format", "{{json .}}", name])
        if raw is None:
            out[name] = {"status": "missing"}
            continue
        info = json.loads(raw.strip().splitlines()[0])
        out[name] = {
            "status": info["State"]["Status"],
            "restarts": info.get("RestartCount", 0),
            "started_at": info["State"].get("StartedAt"),
        }
    present = [n for n in names if out[n]["status"] != "missing"]  # stats fails on any unknown
    raw = _run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *present])
    for line in (raw or "").splitlines() if present else []:
        try:
            s = json.loads(line)
        except ValueError:
            continue
        cpu = s.get("CPUPerc", "").rstrip("%")
        if s.get("Name") in out:
            out[s["Name"]]["mem_mb"] = parse_size_mb(s.get("MemUsage", "").split("/")[0])
            out[s["Name"]]["cpu_pct"] = float(cpu) if re.fullmatch(r"[\d.]+", cpu) else None
    reconnects: dict[str, int] = {}
    for name in present:
        if name in WORKERS and since is not None:
            logs = _run(["docker", "logs", "--since", f"{since:.0f}", name])
            reconnects[name] = len(RECONNECT.findall(logs or ""))
    return out, reconnects


def _get_json(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (our own loopback API)
        return json.loads(r.read())


def api_sample(base: str) -> tuple[dict, dict]:
    try:
        h = _get_json(f"{base}/healthz")
        api = {"ok": True, "cameras": h.get("cameras", {}), "ingest": h.get("ingest", {})}
    except Exception as e:  # noqa: BLE001 (any failure = API down for this sample)
        return {"ok": False, "error": type(e).__name__}, {}
    zones: dict = {}
    try:
        for z in _get_json(f"{base}/api/status").get("zones", []):
            zones[z["id"]] = {"stale": z.get("stale"), "free": z.get("free")}
    except Exception as e:  # noqa: BLE001 (503 before the first data)
        api["status_error"] = type(e).__name__
    return api, zones


def take_sample(base: str, names: tuple[str, ...], since: float | None) -> dict:
    containers, reconnects = containers_sample(names, since)
    api, zones = api_sample(base)
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": host_sample(),
        "containers": containers,
        "api": api,
        "zones": zones,
        "reconnects": reconnects,
    }


def cmd_sample(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    names = tuple(args.containers.split(","))
    end = time.monotonic() + args.duration if args.duration else None
    since: float | None = None
    while True:
        started = time.time()
        sample = take_sample(args.api.rstrip("/"), names, since)
        since = started
        with out.open("a") as f:
            f.write(json.dumps(sample, separators=(",", ":")) + "\n")
        if args.once or (end is not None and time.monotonic() >= end):
            return 0
        time.sleep(max(0.0, args.interval - (time.time() - started)))


# ---------------------------------------------------------------------------- report


def load(path: Path) -> list[dict]:
    samples = []
    for line in path.read_text().splitlines():
        try:
            s = json.loads(line)
            s["_t"] = datetime.fromisoformat(s["ts"])
        except (ValueError, KeyError):
            continue  # a line cut off by a power loss
        samples.append(s)
    samples.sort(key=lambda s: s["_t"])
    return samples


def _checks(s: dict) -> dict[str, str | None]:
    """Every watched item in one sample → None if fine, else what's wrong."""
    out: dict[str, str | None] = {}
    for name, c in s["containers"].items():
        status = c.get("status", "missing")
        out[f"container {name}"] = None if status == "running" else status
    out["api"] = None if s["api"].get("ok") else "unreachable"
    for cam, state in s["api"].get("cameras", {}).items():
        out[f"camera {cam}"] = None if state == "ok" else state
    for zone, z in s.get("zones", {}).items():
        out[f"zone {zone}"] = "stale" if z.get("stale") else None
    return out


def outages(samples: list[dict], gap_after: timedelta) -> tuple[list[dict], list[str]]:
    """Problem periods (and sampling gaps) of items that were fine at least once; plus the
    items never fine (not deployed: a camera that doesn't exist yet, the tunnel without
    `--profile public`), which are listed but not counted."""
    seen_ok: set[str] = set()
    every: set[str] = set()
    open_: dict[str, tuple[datetime, str]] = {}
    done: list[dict] = []
    prev = None
    for s in samples:
        t = s["_t"]
        if prev is not None and t - prev > gap_after:
            done.append({"what": "no samples (host or sampler down)", "start": prev, "end": t})
        checks = _checks(s)
        every |= checks.keys()
        for key, problem in checks.items():
            if problem is None:
                seen_ok.add(key)
                if key in open_:
                    start, what = open_.pop(key)
                    done.append({"what": what, "start": start, "end": t})
            elif key in seen_ok and key not in open_:
                open_[key] = (t, f"{key} {problem}")
        prev = t
    done += [{"what": what, "start": start, "end": None} for start, what in open_.values()]
    for o in done:
        end = o["end"] or prev
        o["minutes"] = round((end - o["start"]).total_seconds() / 60, 1)
    return sorted(done, key=lambda o: o["start"]), sorted(every - seen_ok)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _slope_mb_per_day(points: list[tuple[datetime, float]]) -> float | None:
    if len(points) < 3 or points[-1][0] - points[0][0] < timedelta(days=1):
        return None  # extrapolating minutes to a day says nothing
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 86400 for t, _ in points]
    ys = [y for _, y in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return (
        round(sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den, 2)
        if den
        else None
    )


def memory(samples: list[dict], growth_mb: float, growth_pct: float) -> dict[str, dict]:
    first_t, last_t = samples[0]["_t"], samples[-1]["_t"]
    names = sorted({n for s in samples for n in s["containers"]})
    out = {}
    for name in names:
        pts = [
            (s["_t"], c["mem_mb"])
            for s in samples
            if (c := s["containers"].get(name, {})).get("mem_mb") is not None
        ]
        if not pts:
            continue
        warm = [(t, m) for t, m in pts if t >= first_t + WARMUP] or pts
        base = _median([m for t, m in warm if t < warm[0][0] + WINDOW])
        final = _median([m for t, m in warm if t > last_t - WINDOW])
        allowed = max(growth_mb, base * growth_pct / 100)
        restarts = [
            c["restarts"] for s in samples if "restarts" in (c := s["containers"].get(name, {}))
        ]
        starts = {
            c.get("started_at")
            for s in samples
            if (c := s["containers"].get(name, {})).get("started_at")
        }
        out[name] = {
            "baseline_mb": base,
            "final_mb": final,
            "max_mb": max(m for _, m in pts),
            "growth_mb": round(final - base, 1),
            "allowed_mb": round(allowed, 1),
            "slope_mb_per_day": _slope_mb_per_day(warm),
            "grew": final - base > allowed,
            "restarts": (restarts[-1] - restarts[0]) if restarts else 0,
            "starts": max(len(starts) - 1, 0),
        }
    return out


def report(
    samples: list[dict],
    days: float = 7,
    interval: float = 60,
    growth_mb: float = 25,
    growth_pct: float = 10,
) -> dict:
    if not samples:
        return {"passed": False, "reasons": ["no samples"]}
    span = samples[-1]["_t"] - samples[0]["_t"]
    outs, never_ok = outages(samples, timedelta(seconds=max(5 * interval, 300)))
    mem = memory(samples, growth_mb, growth_pct)
    temps = sorted(t for s in samples if (t := s["host"].get("temp_c")) is not None)
    throttled = sorted(
        {t for s in samples if (t := s["host"].get("throttled")) not in (None, "0x0")}
    )
    reconnects: dict[str, int] = {}
    for s in samples:
        for name, n in s.get("reconnects", {}).items():
            reconnects[name] = reconnects.get(name, 0) + n
    reasons = []
    if span < timedelta(days=days):
        reasons.append(f"only {span.total_seconds() / 86400:.2f} of {days:g} days sampled")
    if "api" in never_ok:
        reasons.append("the API never answered")
    unrecovered = [o["what"] for o in outs if o["end"] is None]
    if unrecovered:
        reasons.append("still down at the last sample: " + ", ".join(sorted(unrecovered)))
    grew = [n for n, m in mem.items() if m["grew"]]
    if grew:
        reasons.append("memory grew: " + ", ".join(grew))
    return {
        "passed": not reasons,
        "reasons": reasons,
        "start": samples[0]["ts"],
        "end": samples[-1]["ts"],
        "days": round(span.total_seconds() / 86400, 2),
        "samples": len(samples),
        "memory": mem,
        "temp_c": {
            "median": _median(temps),
            "p95": temps[int(0.95 * (len(temps) - 1))] if temps else None,
            "max": temps[-1] if temps else None,
        },
        "throttled": throttled,
        "never_ok": never_ok,
        "reconnects": reconnects,
        "outages": [
            {**o, "start": o["start"].isoformat(), "end": o["end"] and o["end"].isoformat()}
            for o in outs
        ],
    }


def format_report(r: dict) -> str:
    if "samples" not in r:
        return "soak: FAILED (no samples)"
    lines = [
        f"soak {r['start']} → {r['end']}: {r['days']} days, {r['samples']} samples",
        "temperature °C: median {median}, p95 {p95}, max {max}".format(**r["temp_c"])
        + (
            f"; throttled flags seen: {', '.join(r['throttled'])}"
            if r["throttled"]
            else "; never throttled"
        ),
        "reconnects: "
        + (", ".join(f"{n} {c}" for n, c in r["reconnects"].items()) or "none logged"),
        "memory (median first 24 h after 1 h warm-up → last 24 h):",
    ]
    for name, m in r["memory"].items():
        lines.append(
            f"  {name}: {m['baseline_mb']} → {m['final_mb']} MB ({m['growth_mb']:+} MB, "
            f"allowed {m['allowed_mb']}), max {m['max_mb']}, slope {m['slope_mb_per_day']} MB/day, "
            f"{m['restarts']} restart(s), {m['starts']} re-start(s)"
            + (" GREW" if m["grew"] else "")
        )
    if r["never_ok"]:
        lines.append("never ok, not counted: " + ", ".join(r["never_ok"]))
    lines.append(f"outages: {len(r['outages'])}")
    for o in r["outages"]:
        end = o["end"] or "STILL DOWN"
        lines.append(f"  {o['start']} → {end} ({o['minutes']} min) {o['what']}")
    lines.append(
        "verdict: " + ("PASSED" if r["passed"] else "NOT PASSED: " + "; ".join(r["reasons"]))
    )
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    r = report(
        load(Path(args.file)), args.days, args.interval, args.mem_growth_mb, args.mem_growth_pct
    )
    print(json.dumps(r, indent=2) if args.json else format_report(r))
    return 0 if r["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="append one JSON line every --interval seconds")
    s.add_argument("--out", default="out/soak/soak.jsonl")
    s.add_argument("--interval", type=float, default=60)
    s.add_argument("--api", default="http://127.0.0.1:8000")
    s.add_argument("--containers", default=",".join(CONTAINERS))
    s.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = never)")
    s.add_argument("--once", action="store_true")
    s.set_defaults(func=cmd_sample)
    r = sub.add_parser("report", help="summarise a sample file and give the verdict")
    r.add_argument("file")
    r.add_argument("--days", type=float, default=7)
    r.add_argument("--interval", type=float, default=60, help="the sampler's interval")
    r.add_argument("--mem-growth-mb", type=float, default=25)
    r.add_argument("--mem-growth-pct", type=float, default=10)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_report)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
