"""Power and network drills (P8.11, docs/design/testing.md §9): watch the app during a cut.

The script only **watches** (`/healthz`, `/api/status` and, with `ADMIN_TOKEN` in the
environment, `/api/admin/alerts`); the person at the lot pulls the plug and puts it back, and
touches nothing else. Run it from outside the machines under test (the dev Pi or a laptop):

    cd backend && uv run python ../scripts/resilience/drill.py power-server https://<PUBLIC_HOST>
    ... wait for "live: pull the plug now", cut for the scenario's time, restore, then wait

Scenarios (what is cut -> what has to be seen):
- `power-server`  power of the API machine, 1 min -> unreachable, then live again
- `power-site`    power of the lot box, 1 min -> the API stays reachable, zones stale, then live
- `internet`      the lot's internet, 10 min (T2) -> as `power-site`, and at least `--min-flow`
                  (1) flow events accepted once the link is back: drive a car over the ramp
                  during the cut (the worker's outbox delivers it afterwards)
- `internet-t1`   the same on one machine at the lot (T1) -> as `power-server`
- `camera`        one camera's cable, 10 min, `--zone` = its zone -> only that zone stale, an
                  admin alert (`camera_down` or `stale`) while it is out, gone afterwards, live

Every run needs: live at the start, the fault seen within `--fault-wait` (300 s), then live
again and staying live for `--settle` (60 s), all within `--max-outage` after the fault was
first seen (300 s for the 1 min cuts: the cut + 3 min to boot + a margin; 780 s for the 10 min
ones). Exit 0 = **passed**, 1 = not passed (with the reasons), 2 = could not start.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

SCENARIOS = ("power-server", "power-site", "internet", "internet-t1", "camera")
UNREACHABLE = ("power-server", "internet-t1")  # the fault is the API not answering
MAX_OUTAGE_S = {"power-server": 300, "power-site": 300, "internet": 780, "internet-t1": 780}
MAX_OUTAGE_S["camera"] = 780
BLIP_SAMPLES = 2  # this many failed reads in a row are the network, not the API
ALERT_KINDS = ("camera_down", "stale")

# ---------------------------------------------------------------------------- pure helpers


@dataclass(frozen=True)
class Sample:
    """One look at the API. `stale` is None while `/api/status` has no data (or no answer)."""

    t: float
    up: bool
    stale: frozenset[str] | None = None
    zones: frozenset[str] = frozenset()
    flow_events: int | None = None
    alerts: frozenset[tuple[str, str]] | None = None  # active (kind, subject); None = not read

    @property
    def live(self) -> bool:
        return self.up and self.stale is not None and not self.stale

    @property
    def state(self) -> str:
        if not self.up:
            return "unreachable"
        if self.stale is None:
            return "no data"
        return f"stale: {', '.join(sorted(self.stale))}" if self.stale else "live"


def in_fault(scenario: str, sample: Sample, zone: str | None = None) -> bool:
    if scenario in UNREACHABLE:
        return not sample.up
    if not sample.up:
        return False
    if scenario == "camera":
        return sample.stale is None or zone in sample.stale
    return not sample.live


def fault_index(scenario: str, samples: list[Sample], zone: str | None = None) -> int | None:
    """The first sample of the fault. A short run of failed reads is not an outage."""
    run = 0
    for i, sample in enumerate(samples):
        if not in_fault(scenario, sample, zone):
            run = 0
            continue
        run += 1
        if scenario not in UNREACHABLE or run > BLIP_SAMPLES:
            return i - run + 1
    return None


def recovered_index(samples: list[Sample], after: int) -> int | None:
    """The first sample after `after` from which every later one is live."""
    index = None
    for i in range(len(samples) - 1, after, -1):
        if not samples[i].live:
            break
        index = i
    return index


def settled(samples: list[Sample], after: int, settle: float) -> bool:
    index = recovered_index(samples, after)
    return index is not None and samples[-1].t - samples[index].t >= settle


def longest_down(samples: list[Sample]) -> int:
    longest = run = 0
    for sample in samples:
        run = 0 if sample.up else run + 1
        longest = max(longest, run)
    return longest


def timeline(samples: list[Sample]) -> list[dict]:
    """The state changes: `[{"t", "state"}]`."""
    out: list[dict] = []
    for sample in samples:
        if not out or out[-1]["state"] != sample.state:
            out.append({"t": sample.t, "state": sample.state})
    return out


def judge(
    scenario: str,
    samples: list[Sample],
    *,
    zone: str | None = None,
    max_outage: float | None = None,
    settle: float = 60.0,
    min_flow: int = 1,
) -> dict:
    """The verdict of one drill: `passed`, the `reasons` it didn't, and the measured times."""
    max_outage = MAX_OUTAGE_S[scenario] if max_outage is None else max_outage
    result: dict = {"scenario": scenario, "zone": zone, "samples": len(samples)}
    result |= {"timeline": timeline(samples), "outage_s": None}
    reasons: list[str] = []
    result["reasons"] = reasons

    def done() -> dict:
        result["passed"] = not reasons
        return result

    if not samples or not samples[0].live:
        reasons.append("not live at the start: fix that first, the drill starts from a live app")
        return done()
    first = fault_index(scenario, samples, zone)
    if first is None:
        reasons.append("the fault was never seen (nothing was cut, or the cut was too short)")
        return done()
    back = recovered_index(samples, first)
    if back is None or samples[-1].t - samples[back].t < settle:
        reasons.append(f"did not come back by itself: {samples[-1].state} at the end")
        return done()
    outage = samples[back].t - samples[first].t
    result["outage_s"] = round(outage, 1)
    result["stale_seen"] = any(s.up and not s.live for s in samples[first:back])
    if outage > max_outage:
        reasons.append(f"back after {outage:.0f} s, more than the {max_outage:.0f} s allowed")

    if scenario not in UNREACHABLE and longest_down(samples) > BLIP_SAMPLES:
        reasons.append("the API was unreachable during the drill (it has to keep answering)")
    if scenario == "camera":
        others = set().union(*(s.stale or () for s in samples)) - {zone}
        if others:
            reasons.append(f"other zones went stale too: {', '.join(sorted(others))}")
        seen = [s for s in samples[first:back] if s.alerts is not None]
        alerted = [s for s in seen if any(kind in ALERT_KINDS for kind, _ in s.alerts)]
        if not seen:
            reasons.append("admin alerts not read: set ADMIN_TOKEN in the environment")
        elif not alerted:
            reasons.append("no admin alert (camera_down / stale) while the camera was out")
        else:
            result["alert_after_s"] = round(alerted[0].t - samples[first].t, 1)
        left = [a for a in samples[-1].alerts or () if a[0] in ALERT_KINDS]
        if left:
            reasons.append(f"the alert is still open at the end: {left[0][0]} {left[0][1]}")
    if scenario == "internet":
        before, after = samples[first - 1].flow_events, samples[-1].flow_events
        delta = None if before is None or after is None else after - before
        result["flow_events"] = delta
        if min_flow and (delta is None or delta < min_flow):
            reasons.append(
                f"{delta or 0} flow events arrived after the reconnect, expected at least "
                f"{min_flow}: a car has to cross the ramp during the cut"
            )
    return done()


# ---------------------------------------------------------------------------- watching


def take_sample(client: httpx.Client, admin: bool, now: float) -> Sample:
    try:
        health = client.get("/healthz")
        if health.status_code != 200 or health.json().get("status") != "ok":
            return Sample(now, False)
        flow = health.json().get("ingest", {}).get("flow_events")
        status = client.get("/api/status")
        if status.status_code != 200:
            return Sample(now, True, flow_events=flow)
        zones = status.json()["zones"]
        alerts = None
        if admin:
            answer = client.get("/api/admin/alerts")
            if answer.status_code == 200:
                issues = answer.json()["issues"]
                alerts = frozenset((i["kind"], i["subject"]) for i in issues if i["active"])
        return Sample(
            now,
            True,
            frozenset(z["id"] for z in zones if z["stale"]),
            frozenset(z["id"] for z in zones),
            flow,
            alerts,
        )
    except (httpx.HTTPError, ValueError, KeyError):
        return Sample(now, False)


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%H:%M:%SZ")


def watch(args: argparse.Namespace, client: httpx.Client, say=print) -> list[Sample]:
    """Samples until the drill is over: recovered and settled, no fault seen, or out of time."""
    admin = "Authorization" in client.headers
    samples: list[Sample] = []
    start = time.time()
    while True:
        sample = take_sample(client, admin, time.time())
        if not samples and not sample.live:
            return [sample]
        if not samples and args.zone and args.zone not in sample.zones:
            return [sample]
        if not samples:
            say(f"{_clock(sample.t)}  live: pull the plug now ({args.scenario})")
        elif sample.state != samples[-1].state:
            say(f"{_clock(sample.t)}  {sample.state}")
        samples.append(sample)
        first = fault_index(args.scenario, samples, args.zone)
        if first is None and sample.t - start > args.fault_wait:
            return samples
        if first is not None:
            if settled(samples, first, args.settle):
                return samples
            if sample.t - samples[first].t > args.max_outage + args.settle:
                return samples
        time.sleep(args.interval)


def report(result: dict) -> str:
    lines = [f"{_clock(step['t'])}  {step['state']}" for step in result["timeline"]]
    if result["outage_s"] is not None:
        lines.append(f"fault seen for {result['outage_s']:.0f} s")
    if "alert_after_s" in result:
        lines.append(f"admin alert {result['alert_after_s']:.0f} s after the zone went stale")
    if result.get("flow_events") is not None:
        lines.append(f"flow events accepted since the cut: {result['flow_events']}")
    if result["scenario"] in UNREACHABLE and result["outage_s"] is not None:
        lines.append(f"stale shown before live: {'yes' if result['stale_seen'] else 'no'}")
    lines.append("PASSED" if result["passed"] else "NOT PASSED")
    lines += [f"  - {reason}" for reason in result["reasons"]]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch the app during a power or network drill.")
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("url", help="the public entry, e.g. https://parking.example.org")
    parser.add_argument("--zone", help="camera: the zone of the camera that is unplugged")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between looks")
    parser.add_argument("--fault-wait", type=float, default=300.0)
    parser.add_argument("--max-outage", type=float, help="default: 300 s (1 min cuts), 780 s")
    parser.add_argument("--settle", type=float, default=60.0)
    parser.add_argument("--min-flow", type=int, default=1, help="internet: 0 = no flow camera")
    parser.add_argument("--out", type=Path, help="write the result as JSON")
    args = parser.parse_args(argv)
    if args.scenario == "camera" and not args.zone:
        parser.error("camera needs --zone")
    if args.max_outage is None:
        args.max_outage = float(MAX_OUTAGE_S[args.scenario])
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("ADMIN_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=5, headers=headers) as client:
        samples = watch(args, client)
    if not samples[0].live:
        print(f"drill: the app is not live ({samples[0].state}); nothing watched", file=sys.stderr)
        return 2
    if args.zone and args.zone not in samples[0].zones:
        print(f"drill: no zone {args.zone!r} in /api/status", file=sys.stderr)
        return 2
    result = judge(
        args.scenario,
        samples,
        zone=args.zone,
        max_outage=args.max_outage,
        settle=args.settle,
        min_flow=args.min_flow,
    )
    print(report(result))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
