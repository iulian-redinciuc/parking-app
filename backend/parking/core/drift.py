"""Flow-count drift over a live test (docs/design/vision.md §10, P5.11).

A flow zone's number is a running sum, so every missed or extra crossing stays in it until a
correction. The drift test measures how fast that error grows: correct the zone to the true
count on day 0, then once a day note the true count next to the app's (`parking drift-note`),
without correcting. `parking drift-report` reads the notes back:

- error = app − true (positive = the app shows more cars than there are)
- drift per day = |error now − error at the first note| / days since the first note

Each note's change from the one before is listed too, so a pattern (nights? rush hour?) shows.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DRIFT_CSV_HEADER = ("ts", "true_occupied", "app_occupied", "note")
TARGET_PER_DAY = 2.0  # cars/day (phase 5 exit criteria)
MIN_DAYS = 7.0
# The daily count isn't taken at the same minute: a last note this much short still counts.
DAYS_SLACK = 0.5


@dataclass(frozen=True)
class DriftNote:
    ts: datetime
    true_occupied: int
    app_occupied: int
    note: str = ""

    @property
    def error(self) -> int:
        return self.app_occupied - self.true_occupied


@dataclass(frozen=True)
class DriftStep:
    note: DriftNote
    days: float  # since the first note
    step: int  # error change since the previous note
    step_per_day: float | None  # None for the first note


@dataclass(frozen=True)
class DriftResult:
    steps: list[DriftStep]
    days: float
    drift_per_day: float | None  # None until two notes exist
    target: float
    min_days: float

    @property
    def verdict(self) -> str:
        if self.drift_per_day is None or self.days < self.min_days - DAYS_SLACK:
            return "INCOMPLETE"
        return "PASSED" if self.drift_per_day <= self.target else "FAILED"

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "days": round(self.days, 2),
            "drift_per_day": None if self.drift_per_day is None else round(self.drift_per_day, 2),
            "target_per_day": self.target,
            "min_days": self.min_days,
            "notes": [
                {
                    "ts": s.note.ts.isoformat(),
                    "days": round(s.days, 2),
                    "true_occupied": s.note.true_occupied,
                    "app_occupied": s.note.app_occupied,
                    "error": s.note.error,
                    "step": s.step,
                    "step_per_day": None if s.step_per_day is None else round(s.step_per_day, 2),
                    "note": s.note.note,
                }
                for s in self.steps
            ],
        }


def parse_drift_notes(text: str, name: str = "notes") -> list[DriftNote]:
    """Read a drift notes CSV (config.md §4): header `ts,true_occupied,app_occupied[,note]`,
    then one row per count; blank lines are skipped. Sorted by time."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"{name}: empty file (header {','.join(DRIFT_CSV_HEADER)} expected)")
    head = tuple(c.strip() for c in rows[0])
    if head not in (DRIFT_CSV_HEADER, DRIFT_CSV_HEADER[:3]):
        want, got = ",".join(DRIFT_CSV_HEADER), ",".join(head)
        raise ValueError(f"{name}: header must be {want}, got {got}")
    out = []
    for n, row in enumerate(rows[1:], start=2):
        if len(row) > len(head) or len(row) < 3:
            raise ValueError(f"{name} line {n}: expected {len(head)} columns, got {len(row)}")
        try:
            ts = datetime.fromisoformat(row[0].strip())
        except ValueError:
            raise ValueError(f"{name} line {n}: ts {row[0]!r} is not an ISO time") from None
        counts = []
        for col, value in zip(DRIFT_CSV_HEADER[1:3], row[1:3], strict=True):
            try:
                counts.append(int(value))
            except ValueError:
                raise ValueError(
                    f"{name} line {n}: {col} {value!r} is not a whole number"
                ) from None
            if counts[-1] < 0:
                raise ValueError(f"{name} line {n}: {col} must be >= 0")
        out.append(
            DriftNote(
                ts if ts.tzinfo else ts.replace(tzinfo=UTC),
                counts[0],
                counts[1],
                row[3].strip() if len(row) > 3 else "",
            )
        )
    return sorted(out, key=lambda e: e.ts)


def load_drift_notes(path: Path) -> list[DriftNote]:
    return parse_drift_notes(path.read_text(encoding="utf-8"), path.name)


def append_drift_note(path: Path, note: DriftNote) -> None:
    """Add one row, writing the header first if the file is new or empty."""
    new = not path.is_file() or not path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if new:
            w.writerow(DRIFT_CSV_HEADER)
        w.writerow(
            [
                note.ts.isoformat(timespec="seconds"),
                note.true_occupied,
                note.app_occupied,
                note.note,
            ]
        )


def measure_drift(
    notes: list[DriftNote], target: float = TARGET_PER_DAY, min_days: float = MIN_DAYS
) -> DriftResult:
    notes = sorted(notes, key=lambda e: e.ts)
    steps: list[DriftStep] = []
    for i, n in enumerate(notes):
        days = (n.ts - notes[0].ts).total_seconds() / 86400
        if i == 0:
            steps.append(DriftStep(n, 0.0, 0, None))
            continue
        prev = notes[i - 1]
        gap = (n.ts - prev.ts).total_seconds() / 86400
        step = n.error - prev.error
        steps.append(DriftStep(n, days, step, step / gap if gap > 0 else None))
    days = steps[-1].days if steps else 0.0
    drift = abs(notes[-1].error - notes[0].error) / days if days > 0 else None
    return DriftResult(steps, days, drift, target, min_days)


def report(result: DriftResult, zone: str) -> str:
    """The table `parking drift-report` prints."""
    lines = [
        f"Drift test, zone '{zone}': {len(result.steps)} notes over {result.days:.1f} days",
        f"{'time':<16} {'day':>5} {'true':>5} {'app':>5} {'error':>6} {'step':>5} {'/day':>6}"
        "  note",
    ]
    for s in result.steps:
        per_day = "" if s.step_per_day is None else f"{s.step_per_day:+.1f}"
        step = "" if s is result.steps[0] else f"{s.step:+d}"
        lines.append(
            f"{s.note.ts.strftime('%Y-%m-%d %H:%M'):<16} {s.days:>5.1f} {s.note.true_occupied:>5} "
            f"{s.note.app_occupied:>5} {s.note.error:>+6d} {step:>5} {per_day:>6}  {s.note.note}"
        )
    if result.steps and result.steps[0].note.error != 0:
        lines.append(
            f"note: the first error is {result.steps[0].note.error:+d}, not 0: "
            "correct the zone to the true count on day 0"
        )
    if result.drift_per_day is None:
        lines.append("drift: needs at least two notes at different times")
    else:
        lines.append(
            f"drift: {result.drift_per_day:.2f} cars/day (target <= {result.target:g}/day "
            f"over >= {result.min_days:g} days)"
        )
    lines.append(f"verdict: {result.verdict}")
    return "\n".join(lines)
