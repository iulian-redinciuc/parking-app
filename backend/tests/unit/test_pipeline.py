"""analyze_frame + zone totals (vision.md §2, analyze pipeline)."""

import numpy as np
import pytest

from parking.config import Camera, CountZone, Slot, SlotFile
from parking.vision.detector import Detection
from parking.vision.occupancy import SlotResult
from parking.vision.pipeline import analyze_frame, zone_totals


def rect(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


class ListDetector:
    def __init__(self, detections):
        self.detections = detections

    def detect(self, frame):
        return list(self.detections)


def camera(**occupancy) -> Camera:
    return Camera(
        id="cam-ground",
        role="occupancy",
        zones=["ground", "yard"],
        source="file:x.jpg",
        slots_file="config/slots/cam-ground.json",
        detector={"model": "m"},
        occupancy=occupancy,
    )


# slot file drawn on a 200×100 image, frame is 400×200 (2× scale)
SLOT_FILE = SlotFile(
    version=1,
    camera_id="cam-ground",
    image_size=(200, 100),
    slots=[
        Slot(id="G01", zone="ground", polygon=rect(0, 0, 50, 50)),
        Slot(id="G02", zone="ground", polygon=rect(50, 0, 100, 50)),
        Slot(id="G03", zone="ground", polygon=rect(100, 0, 150, 50)),
    ],
    count_zones=[CountZone(zone="yard", polygon=rect(0, 60, 200, 100))],
)
FRAME = np.zeros((200, 400, 3), np.uint8)


def car(x1, y1, x2, y2):
    return Detection(cls="car", conf=0.9, box=(x1, y1, x2, y2))


def test_analyze_frame_scores_counts_and_totals():
    # box_bottom of this box (y 30..100) covers 70% of G01 (frame px 0..100 × 0..100)
    dets = [car(0, -100, 100, 100), car(300, 120, 340, 180)]
    r = analyze_frame(FRAME, camera(), SLOT_FILE, ListDetector(dets), {"yard": 4})
    assert r.frame_size == (400, 200)
    assert [(s.id, s.taken) for s in r.slots] == [("G01", True), ("G02", False), ("G03", False)]
    assert r.zone_counts == {"yard": 1}
    assert r.banner_totals() == {"ground": (2, 3), "yard": (3, 4)}
    assert set(r.timings) == {"detect_ms", "score_ms", "total_ms"}
    assert r.inference_ms == r.timings["detect_ms"]


def test_analyze_frame_uses_camera_threshold_and_mode():
    # box_bottom covers 35% of G01's height: taken at 0.3, free at 0.5
    det = [car(0, 0, 100, 100)]
    low = analyze_frame(FRAME, camera(threshold=0.3), SLOT_FILE, ListDetector(det))
    high = analyze_frame(FRAME, camera(threshold=0.5), SLOT_FILE, ListDetector(det))
    assert low.slots[0].taken and not high.slots[0].taken
    assert low.slots[0].score == pytest.approx(0.35)


def test_count_zone_without_capacity_has_no_total():
    r = analyze_frame(FRAME, camera(), SLOT_FILE, ListDetector([]))
    assert r.zone_counts == {"yard": 0}
    assert set(r.totals) == {"ground"}


def test_observation_shape():
    r = analyze_frame(FRAME, camera(), SLOT_FILE, ListDetector([car(0, -100, 100, 100)]))
    obs = r.to_observation()
    assert obs["v"] == 1 and obs["camera_id"] == "cam-ground"
    assert obs["ts"].endswith("Z") and len(obs["ts"]) == 24
    assert obs["frame_size"] == [400, 200]
    assert obs["detections"] == 1
    assert isinstance(obs["inference_ms"], int)
    assert obs["slots"][0] == {"id": "G01", "score": 0.7, "taken": True}
    assert obs["totals"] == {"ground": {"free": 2, "taken": 1, "capacity": 3}}


def test_zone_totals_clamps_free_at_zero():
    slots = [SlotResult("A", 1.0, True), SlotResult("B", 1.0, True)]
    totals = zone_totals(
        slots, {"A": "ground", "B": "ground"}, {"yard": 9}, {"ground": 1, "yard": 5}
    )
    assert (totals["ground"].free, totals["ground"].capacity) == (0, 1)
    assert (totals["yard"].free, totals["yard"].taken) == (0, 9)
