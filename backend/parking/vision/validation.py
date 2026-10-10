"""Validation set helpers (P4.8): pick frames out of the debug captures and check tag coverage.

`pick` reads `data/debug/<camera>/<YYYY-MM-DD>/<HHMMSS>_<ms>-<reasons>.jpg|json` (vision.md §6.1)
and spreads the picks over strata of (time of day × occupancy level) so the set isn't 80 %
quiet afternoons. The stratum only steers the pick; the labeller sets the real condition tags
in the slot editor's label mode. `coverage` is the "Done when" of P4.8.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from parking.config import LabelFile

# condition tags the validation set must cover (phase-4 guide, P4.8 step 2)
REQUIRED_TAGS = ("morning", "noon", "evening", "night", "dry", "rain", "empty", "busy", "full")
MIN_IMAGES = 200
MIN_PER_TAG = 15

NAME = re.compile(r"^(\d{6})_(\d{3})-[a-z+]+\.jpg$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def held_out(name: str, share: float) -> bool:
    """Deterministic frame split by file name: the same frames are held out every run, by the
    slot classifier's training (vision.md §9) and the detector dataset (detector-training.md)."""
    return int(hashlib.sha1(name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < share


def period(hour: int) -> str:
    """Lot-local hour → time-of-day tag (night 21–06, morning 06–11, noon 11–15, evening 15–21)."""
    if 6 <= hour < 11:
        return "morning"
    if 11 <= hour < 15:
        return "noon"
    if 15 <= hour < 21:
        return "evening"
    return "night"


def level(taken: int, total: int) -> str:
    """Occupancy level tag: empty ≤ 20 % taken, full ≥ 90 %, busy in between."""
    if total <= 0:
        return "unknown"
    share = taken / total
    if share <= 0.2:
        return "empty"
    if share >= 0.9:
        return "full"
    return "busy"


@dataclass(frozen=True)
class Capture:
    path: Path  # the JPEG
    local: datetime  # lot-local time (naive) from the folder + file name
    period: str
    level: str

    @property
    def stratum(self) -> tuple[str, str]:
        return (self.period, self.level)

    @property
    def out_name(self) -> str:
        """`<date>_<HHMMSS>_<ms>.jpg`: unique across days, sorts by time, no capture reasons."""
        return f"{self.local:%Y-%m-%d_%H%M%S}_{self.local.microsecond // 1000:03d}.jpg"


def scan(debug_dir: Path) -> list[Capture]:
    """Every capture under `debug_dir` with a readable observation JSON, oldest first."""
    found = []
    for day in sorted(p for p in debug_dir.iterdir() if p.is_dir() and DATE.match(p.name)):
        for jpg in sorted(day.glob("*.jpg")):
            m = NAME.match(jpg.name)
            meta = jpg.with_suffix(".json")
            if not m or not meta.is_file():
                continue
            try:
                slots = json.loads(meta.read_text())["observation"]["slots"]
                taken = sum(1 for s in slots if s["taken"])
            except (OSError, ValueError, KeyError, TypeError):
                continue  # half-written or pruned under us
            local = datetime.strptime(f"{day.name} {m[1]} {m[2]}", "%Y-%m-%d %H%M%S %f")
            found.append(Capture(jpg, local, period(local.hour), level(taken, len(slots))))
    found.sort(key=lambda c: c.local)
    return found


def _quotas(sizes: dict[tuple[str, str], int], count: int) -> dict[tuple[str, str], int]:
    """Share `count` as evenly as possible over the strata, none above its size."""
    quota = dict.fromkeys(sizes, 0)
    left = min(count, sum(sizes.values()))
    while left:
        open_ = [k for k in sorted(sizes) if quota[k] < sizes[k]]
        for k in open_[:left]:
            quota[k] += 1
            left -= 1
    return quota


def _spread(items: Sequence[Capture], n: int) -> list[Capture]:
    """`n` items evenly spaced in time (the middle of each of `n` equal runs)."""
    k = len(items)
    return [items[int((i + 0.5) * k / n)] for i in range(n)]


def pick(captures: Iterable[Capture], count: int) -> list[Capture]:
    """Up to `count` captures, stratified by (period, level) and spread in time, oldest first."""
    groups: dict[tuple[str, str], list[Capture]] = {}
    for c in captures:
        groups.setdefault(c.stratum, []).append(c)
    quota = _quotas({k: len(v) for k, v in groups.items()}, count)
    chosen = [c for k, v in groups.items() if quota[k] for c in _spread(v, quota[k])]
    return sorted(chosen, key=lambda c: c.local)


@dataclass(frozen=True)
class Coverage:
    images: int
    tags: dict[str, int]  # every tag used, plus the required ones (0 if unused)
    missing: dict[str, int]  # required tags under the minimum → their count
    min_images: int
    min_per_tag: int

    @property
    def ok(self) -> bool:
        return self.images >= self.min_images and not self.missing


def coverage(
    labels: LabelFile,
    required: Sequence[str] = REQUIRED_TAGS,
    min_images: int = MIN_IMAGES,
    min_per_tag: int = MIN_PER_TAG,
) -> Coverage:
    """Count labelled images and condition tags against the P4.8 target."""
    counts = Counter(t for e in labels.images.values() for t in set(e.conditions))
    tags = {t: counts.get(t, 0) for t in [*required, *sorted(set(counts) - set(required))]}
    missing = {t: counts.get(t, 0) for t in required if counts.get(t, 0) < min_per_tag}
    return Coverage(len(labels.images), tags, missing, min_images, min_per_tag)
