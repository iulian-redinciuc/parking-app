"""Health-threshold tuning from logged frame metrics (vision.md §5, P4.4).

With `LOG_LEVEL=DEBUG` the occupancy worker logs one `health_metrics` line per frame
(`format_metrics`). `parking health-stats` reads those logs back (`parse_lines`), groups them by
lot-local hour (`summarize`) and suggests thresholds (`suggest`):

- `black_mean_max`: half the 1st percentile of `mean` (below the darkest valid frame).
- `blur_laplacian_min`: half the 1st percentile of `laplacian_var` (below the softest night frame).
- `frozen_diff_max`: kept at the configured value unless 1% of real frame-to-frame differences
  are smaller, then half that.

Percentiles, not minimums, so a few frames from covering the lens or a passing bird don't set
the threshold; cut test periods out with `--since/--until` anyway.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from parking.config import HealthCfg
from parking.vision.health import HealthMetrics

PREFIX = "health_metrics"
LINE_RE = re.compile(
    PREFIX + r" camera=(?P<camera>\S+) ts=(?P<ts>\S+) mean=(?P<mean>[\d.]+) "
    r"lap=(?P<lap>[\d.]+) diff=(?P<diff>[\d.]+|-) issue=(?P<issue>\S+)"
)
PERCENTILE = 1  # the "darkest/softest valid frame", robust to a few outliers
MARGIN = 0.5  # suggested threshold = MARGIN × that percentile


def format_metrics(camera: str, ts: datetime, m: HealthMetrics, issue: str | None) -> str:
    diff = "-" if m.frame_diff is None else f"{m.frame_diff:.3f}"
    return (
        f"{PREFIX} camera={camera} ts={ts.isoformat()} mean={m.mean:.2f} "
        f"lap={m.laplacian_var:.2f} diff={diff} issue={issue or '-'}"
    )


@dataclass(frozen=True)
class Record:
    camera: str
    ts: datetime
    mean: float
    laplacian_var: float
    frame_diff: float | None
    issue: str | None


def parse_lines(lines: Iterable[str]) -> Iterable[Record]:
    """`health_metrics` records found anywhere in the lines (docker/journal prefixes are fine)."""
    for line in lines:
        m = LINE_RE.search(line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m["ts"])
        except ValueError:
            continue
        yield Record(
            m["camera"],
            ts,
            float(m["mean"]),
            float(m["lap"]),
            None if m["diff"] == "-" else float(m["diff"]),
            None if m["issue"] == "-" else m["issue"],
        )


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p1": None, "median": None}
    a = np.asarray(values)
    return {
        "min": round(float(a.min()), 2),
        "p1": round(float(np.percentile(a, PERCENTILE)), 2),
        "median": round(float(np.median(a)), 2),
    }


@dataclass
class Group:
    frames: int = 0
    mean: list[float] = field(default_factory=list)
    lap: list[float] = field(default_factory=list)
    diff: list[float] = field(default_factory=list)
    issues: dict[str, int] = field(default_factory=dict)

    def add(self, r: Record) -> None:
        self.frames += 1
        self.mean.append(r.mean)
        self.lap.append(r.laplacian_var)
        if r.frame_diff is not None:
            self.diff.append(r.frame_diff)
        if r.issue:
            self.issues[r.issue] = self.issues.get(r.issue, 0) + 1

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "mean": _stats(self.mean),
            "laplacian_var": _stats(self.lap),
            "frame_diff": _stats(self.diff),
            "issues": dict(sorted(self.issues.items())),
        }


def summarize(records: Iterable[Record], tz: str) -> tuple[dict[int, Group], Group]:
    """Group by lot-local hour (0–23); also return the overall group."""
    zone = ZoneInfo(tz)
    hours: dict[int, Group] = {}
    total = Group()
    for r in records:
        hours.setdefault(r.ts.astimezone(zone).hour, Group()).add(r)
        total.add(r)
    return dict(sorted(hours.items())), total


def suggest(total: Group, current: HealthCfg) -> dict[str, float]:
    """Suggested `health:` values (see the module docstring); empty with no frames."""
    if not total.frames:
        return {}
    out = {
        "black_mean_max": round(MARGIN * float(np.percentile(total.mean, PERCENTILE)), 1),
        "blur_laplacian_min": round(MARGIN * float(np.percentile(total.lap, PERCENTILE)), 1),
    }
    frozen = current.frozen_diff_max
    if total.diff:
        p = float(np.percentile(total.diff, PERCENTILE))
        if p < frozen:
            frozen = round(MARGIN * p, 3)
    out["frozen_diff_max"] = frozen
    return out


def report(hours: dict[int, Group], total: Group, current: HealthCfg) -> str:
    """Plain-text table: one row per lot-local hour, then the overall row and suggestions."""
    head = (
        f"{'hour':>5} {'frames':>7} {'mean p1':>8} {'mean med':>9} {'lap p1':>8} "
        f"{'lap med':>8} {'diff p1':>8}  issues"
    )
    rows = [head]

    def row(label: str, g: Group) -> str:
        d = g.as_dict()

        def f(v: float | None) -> str:
            return "-" if v is None else f"{v:.1f}" if v >= 10 else f"{v:.2f}"

        issues = ", ".join(f"{k} {v}" for k, v in d["issues"].items()) or "-"
        return (
            f"{label:>5} {g.frames:>7} {f(d['mean']['p1']):>8} {f(d['mean']['median']):>9} "
            f"{f(d['laplacian_var']['p1']):>8} {f(d['laplacian_var']['median']):>8} "
            f"{f(d['frame_diff']['p1']):>8}  {issues}"
        )

    rows += [row(f"{h:02d}h", g) for h, g in hours.items()]
    rows.append(row("all", total))
    s = suggest(total, current)
    if s:
        rows.append("")
        rows.append("Suggested health: (lot.yaml cameras[].health; current in brackets)")
        for k, v in s.items():
            rows.append(f"  {k}: {v}  [{getattr(current, k)}]")
        if len(hours) < 24:
            rows.append(
                f"Only {len(hours)} of 24 lot-local hours logged: collect a full day and night "
                "(and some rain) before using these."
            )
    return "\n".join(rows)
