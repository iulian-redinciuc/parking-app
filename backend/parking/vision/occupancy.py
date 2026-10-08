"""Slot occupancy scoring and zone counting (docs/design/vision.md §2)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from shapely import STRtree
from shapely.geometry.base import BaseGeometry

from parking.config import CountZone, Slot
from parking.geometry import bottom_center, box_bottom, overlap_ratio, point_in, to_polygon
from parking.vision.detector import Detection

Mode = Literal["mask", "box_bottom"]
Size = tuple[int, int]  # width, height


@dataclass(frozen=True)
class SlotResult:
    id: str
    score: float  # 0..1 overlap ratio
    taken: bool  # score >= threshold (before temporal smoothing)


def _scaled(points: Sequence[Sequence[float]], image_size: Size | None, frame_size: Size):
    if image_size is None:
        return points
    sx, sy = frame_size[0] / image_size[0], frame_size[1] / image_size[1]
    return [(x * sx, y * sy) for x, y in points]


def footprint(det: Detection, mode: Mode = "mask") -> BaseGeometry:
    """Where a detection covers the ground: its mask, or the bottom 35% of its box.

    In mask mode a detection without a usable mask falls back to `box_bottom`.
    """
    if mode == "mask" and det.mask is not None and len(det.mask) >= 3:
        poly = to_polygon(np.asarray(det.mask, dtype=float).tolist())
        if not poly.is_empty:
            return poly
    return box_bottom(det.box)


def score_slots(
    detections: Sequence[Detection],
    slots: Sequence[Slot],
    frame_size: Size,
    mode: Mode = "mask",
    threshold: float = 0.30,
    image_size: Size | None = None,
) -> list[SlotResult]:
    """Score each slot by the best single overlap (max, not sum) with a detection footprint.

    Slot polygons are in `image_size` pixels (the slot file's) and are rescaled to
    `frame_size`; with `image_size=None` they are already in frame pixels.
    """
    if mode not in ("mask", "box_bottom"):
        raise ValueError(f"unknown mode {mode!r}")
    feet = [footprint(d, mode) for d in detections]
    tree = STRtree(feet)
    results = []
    for slot in slots:
        poly = to_polygon(_scaled(slot.polygon, image_size, frame_size))
        near = tree.query(poly) if feet else []
        score = max((overlap_ratio(feet[i], poly) for i in near), default=0.0)
        results.append(SlotResult(id=slot.id, score=score, taken=score >= threshold))
    return results


def count_in_zones(
    detections: Sequence[Detection],
    count_zones: Sequence[CountZone],
    frame_size: Size,
    image_size: Size | None = None,
) -> dict[str, int]:
    """Vehicles per zone: a vehicle counts if its box's bottom-centre is inside the polygon.

    Every zone in `count_zones` appears in the result (0 if empty); several polygons of the
    same zone are combined (a vehicle counts once per zone).
    """
    polys: dict[str, list[BaseGeometry]] = {}
    for cz in count_zones:
        poly = to_polygon(_scaled(cz.polygon, image_size, frame_size))
        polys.setdefault(cz.zone, []).append(poly)
    counts = {}
    for zone, zone_polys in polys.items():
        counts[zone] = sum(
            any(point_in(p, bottom_center(d.box)) for p in zone_polys) for d in detections
        )
    return counts
