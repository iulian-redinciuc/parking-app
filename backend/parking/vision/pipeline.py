"""One frame in, one occupancy result out (docs/design/vision.md §2, analyze pipeline).

`analyze_frame` is shared by `parking analyze` (Phase 1) and the occupancy worker (Phase 2).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from parking.config import Camera, SlotFile
from parking.vision.appearance import score_slots_appearance
from parking.vision.detector import Detection, Detector
from parking.vision.occupancy import SlotResult, count_in_zones, score_slots
from parking.vision.slot_classifier import Classifier, score_slots_classifier


@dataclass(frozen=True)
class ZoneTotal:
    free: int
    taken: int
    capacity: int


@dataclass(frozen=True)
class AnalysisResult:
    camera_id: str
    ts: datetime  # UTC, when the frame was analysed
    frame_size: tuple[int, int]  # width, height
    detections: list[Detection]
    slots: list[SlotResult]
    zone_counts: dict[str, int]  # vehicles per `count` zone
    totals: dict[str, ZoneTotal]  # every zone the slot file covers
    timings: dict[str, float] = field(default_factory=dict)  # detect_ms, score_ms, total_ms

    @property
    def inference_ms(self) -> float:
        """The detector's time; without a detector (appearance, classifier) the scoring time."""
        return self.timings.get("detect_ms") or self.timings.get("score_ms", 0.0)

    def banner_totals(self) -> dict[str, tuple[int, int]]:
        """`{zone: (free, capacity)}` as `annotate_occupancy` wants it."""
        return {z: (t.free, t.capacity) for z, t in self.totals.items()}

    def to_observation(self) -> dict[str, Any]:
        """The observation payload (api.md §5) plus `totals` and `timings`."""
        return {
            "v": 1,
            "camera_id": self.camera_id,
            "ts": self.ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "frame_size": list(self.frame_size),
            "inference_ms": round(self.inference_ms),
            "detections": len(self.detections),
            "slots": [
                {"id": s.id, "score": round(s.score, 3), "taken": s.taken} for s in self.slots
            ],
            "zone_counts": dict(self.zone_counts),
            "totals": {z: vars(t).copy() for z, t in self.totals.items()},
            "timings": {k: round(v, 1) for k, v in self.timings.items()},
        }


def zone_totals(
    slots: list[SlotResult],
    slot_zones: Mapping[str, str],
    zone_counts: Mapping[str, int],
    capacities: Mapping[str, int] | None = None,
) -> dict[str, ZoneTotal]:
    """Free/taken/capacity per zone.

    Slot zones: taken = taken slots, capacity = `capacities[zone]` or the number of slots.
    Count zones: taken = vehicles counted; only included if `capacities` has the zone.
    `free` is clamped at 0.
    """
    capacities = capacities or {}
    totals: dict[str, ZoneTotal] = {}
    by_zone: dict[str, list[SlotResult]] = {}
    for s in slots:
        by_zone.setdefault(slot_zones[s.id], []).append(s)
    for zone, results in by_zone.items():
        taken = sum(r.taken for r in results)
        cap = capacities.get(zone, len(results))
        totals[zone] = ZoneTotal(free=max(cap - taken, 0), taken=taken, capacity=cap)
    for zone, taken in zone_counts.items():
        if zone in capacities:
            cap = capacities[zone]
            totals[zone] = ZoneTotal(free=max(cap - taken, 0), taken=taken, capacity=cap)
    return totals


def analyze_frame(
    frame: np.ndarray,
    camera_cfg: Camera,
    slot_file: SlotFile,
    detector: Detector | None,
    capacities: Mapping[str, int] | None = None,
    reference: np.ndarray | None = None,
    classifier: Classifier | None = None,
) -> AnalysisResult:
    """Score the slots, count the count zones and total each zone.

    `occupancy.method: detector` detects vehicles and scores slots by overlap;
    `appearance` (straight-down views, vision.md §2.1) never touches `detector` (it may be
    None), finds no vehicles and scores slots by how unlike pavement they look, using
    `reference` (the empty lot) if given; `classifier` / `ensemble` (vision.md §9) score
    slots with `classifier`'s P(taken), alone or averaged with the appearance score.
    `capacities` (zone -> capacity, e.g. from `LotConfig.zone_capacity`) is optional; without
    it slot zones use their number of slots and count zones get no total.
    """
    ts = datetime.now(UTC)
    h, w = frame.shape[:2]
    frame_size = (w, h)
    occ = camera_cfg.occupancy

    t0 = time.perf_counter()
    if occ.uses_classifier:
        if classifier is None:
            raise ValueError(
                f"camera '{camera_cfg.id}' uses the slot classifier but none was given"
            )
        detections = []
        t1 = t0
        slots = score_slots_classifier(
            frame,
            slot_file.slots,
            frame_size,
            slot_file.image_size,
            classifier,
            occ.classifier.threshold,
            ensemble=occ.method == "ensemble",
            appearance_threshold=occ.threshold,
            appearance=occ.appearance,
            reference=reference,
        )
    elif occ.method == "appearance":
        detections: list[Detection] = []
        t1 = t0  # no detector: detect_ms = 0
        slots = score_slots_appearance(
            frame,
            slot_file.slots,
            frame_size,
            slot_file.image_size,
            occ.threshold,
            occ.appearance,
            reference,
        )
    else:
        if detector is None:
            raise ValueError(f"camera '{camera_cfg.id}' uses the detector but none was given")
        detections = detector.detect(frame)
        t1 = time.perf_counter()
        slots = score_slots(
            detections,
            slot_file.slots,
            frame_size,
            mode=occ.mode,
            threshold=occ.threshold,
            image_size=slot_file.image_size,
        )
    counts = count_in_zones(detections, slot_file.count_zones, frame_size, slot_file.image_size)
    t2 = time.perf_counter()

    slot_zones = {s.id: s.zone for s in slot_file.slots}
    return AnalysisResult(
        camera_id=camera_cfg.id,
        ts=ts,
        frame_size=frame_size,
        detections=detections,
        slots=slots,
        zone_counts=counts,
        totals=zone_totals(slots, slot_zones, counts, capacities),
        timings={
            "detect_ms": (t1 - t0) * 1000,
            "score_ms": (t2 - t1) * 1000,
            "total_ms": (t2 - t0) * 1000,
        },
    )
