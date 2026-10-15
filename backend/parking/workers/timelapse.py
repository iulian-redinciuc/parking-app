"""Weeks of debug captures made from images (P4.8 PoC; docs/design/vision.md §6.1).

Stands in for 1–2 weeks of `DEBUG_CAPTURE=true` on a real camera until there is one: the
labelled replay frames are lit for each lot-local hour (`lighting.light`: day, dusk, IR night,
rain), checked by `FrameHealth` and analysed by the camera's real `analyze_frame`, and every
observation goes through the worker's own `DebugCapture` with a simulated clock, so the files
are exactly what a worker writes. Because each frame comes from a labelled replay frame, its
truth is known: `run` also returns a label file for the would-be validation names
(`<date>_<HHMMSS>_<ms>.jpg`, as `parking validation pick` copies them), with the condition
tags a labeller would set (time of day, dry/rain, empty/busy/full) plus `simulated`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from parking.config import ImageLabels, LabelFile
from parking.vision import lighting
from parking.vision.health import FrameHealth
from parking.vision.sources import Frame
from parking.vision.validation import level, period
from parking.workers.debug_capture import DebugCapture
from parking.workers.occupancy_worker import to_observation

RAIN_SPELLS_PER_DAY = 0.8  # mean rainy spells a day, each 1–5 h, any time of day


def rain_spells(days: int, seed: int) -> list[tuple[float, float]]:
    """Rainy spells as (start, end) hours since the first midnight, the same for the same seed."""
    rng = np.random.default_rng([seed, 7])
    spells = []
    for d in range(days):
        for _ in range(int(rng.poisson(RAIN_SPELLS_PER_DAY))):
            start = 24 * d + float(rng.uniform(0, 24))
            spells.append((start, start + float(rng.uniform(1, 5))))
    return spells


def validation_name(local: datetime) -> str:
    """The name `parking validation pick` gives a capture taken at `local` (lot-local)."""
    return f"{local:%Y-%m-%d_%H%M%S}_{local.microsecond // 1000:03d}.jpg"


@dataclass
class Result:
    frames: int = 0
    unhealthy: dict[str, int] = field(default_factory=dict)
    saved: int = 0
    truth: dict[str, ImageLabels] = field(default_factory=dict)  # validation name -> labels
    slot_errors: int = 0  # analysed slots whose `taken` differs from the truth (unsure skipped)
    slots: int = 0


def run(
    files: Sequence[Path],
    labels: LabelFile,
    analyse: Callable[[np.ndarray], object],
    health: FrameHealth,
    *,
    root: Path,
    camera_id: str,
    lot_id: str | None,
    tz: str,
    start: datetime,
    days: int,
    every_min: float,
    capture_every_min: float = 10.0,
    seed: int = 1,
    progress: Callable[[int, int], None] | None = None,
) -> Result:
    """Light, check, analyse and capture one frame every `every_min` for `days` from the
    lot-local midnight `start` (naive); `files` cycle in order and must all be in `labels`.
    Captures go to `data/debug/<camera>/` under `root`, every `capture_every_min` + flips."""
    zone = ZoneInfo(tz)
    now = [0.0]  # the worker's monotonic clock, simulated
    capture = DebugCapture(
        root,
        camera_id,
        tz,
        enabled=True,
        every_s=capture_every_min * 60,
        retention_s=float("inf"),
        clock=lambda: now[0],
    )
    spells = rain_spells(days, seed)
    total = int(days * 24 * 60 / every_min)
    out = Result()
    t0 = datetime(start.year, start.month, start.day, tzinfo=zone)
    for i in range(total):
        hours = i * every_min / 60
        local = t0 + timedelta(hours=hours)
        # the frame's hour on the wall clock (DST days are 23 or 25 h long)
        hour = local.hour + local.minute / 60 + local.second / 3600
        wet = any(a <= hours < b for a, b in spells)
        src = files[i % len(files)]
        image = cv2.imread(str(src))
        if image is None:
            raise ValueError(f"can't read {src}")
        lit, how = lighting.light(image, hour, wet, seed, i)
        out.frames += 1
        ts = local.astimezone(ZoneInfo("UTC"))
        issue = health.check(Frame(lit, ts))
        if issue:
            out.unhealthy[issue] = out.unhealthy.get(issue, 0) + 1
        else:
            result = analyse(lit)
            obs = to_observation(result, ts, lot_id)
            now[0] = hours * 3600
            if capture.maybe_save(lit, obs) is not None:
                out.saved += 1
                truth = labels.images[src.name]
                taken = set(truth.taken)
                unsure = set(truth.unsure)
                for s in obs.slots:
                    if s.id not in unsure:
                        out.slots += 1
                        out.slot_errors += s.taken != (s.id in taken)
                tags = [
                    period(local.hour),
                    "rain" if wet else "dry",
                    level(len(taken), len(obs.slots)),
                    *(["dusk"] if how.phase == "dusk" else []),
                    "simulated",
                ]
                out.truth[validation_name(local)] = ImageLabels(
                    conditions=tags, taken=sorted(taken), unsure=sorted(unsure)
                )
        if progress:
            progress(i + 1, total)
    return out
