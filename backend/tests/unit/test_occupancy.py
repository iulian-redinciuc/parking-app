import numpy as np
import pytest

from parking.config import CountZone, Slot
from parking.vision.detector import Detection
from parking.vision.occupancy import count_in_zones, footprint, score_slots

FRAME = (400, 300)


def slot(sid, x1, y1, x2, y2, zone="ground"):
    return Slot(id=sid, zone=zone, polygon=[(x1, y1), (x2, y1), (x2, y2), (x1, y2)])


def car(box, mask=None):
    m = None if mask is None else np.asarray(mask, dtype=np.float32)
    return Detection(cls="car", conf=0.9, box=box, mask=m)


def rect(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def scores(results):
    return {r.id: r.score for r in results}


def test_car_fully_inside_slot():
    det = car((90, 90, 210, 210), mask=rect(90, 90, 210, 210))
    [r] = score_slots([det], [slot("G01", 100, 100, 200, 200)], FRAME)
    assert r.score == pytest.approx(1.0)
    assert r.taken


def test_car_half_over_two_slots():
    det = car((50, 100, 150, 200), mask=rect(50, 100, 150, 200))
    s = scores(
        score_slots([det], [slot("G01", 0, 100, 100, 200), slot("G02", 100, 100, 200, 200)], FRAME)
    )
    assert s == pytest.approx({"G01": 0.5, "G02": 0.5})


def test_two_partial_cars_use_max_not_sum():
    left = car((0, 0, 20, 100), mask=rect(80, 0, 100, 100))  # 20% of the slot from the left
    right = car((0, 0, 20, 100), mask=rect(180, 0, 200, 100))  # 20% from the right
    [r] = score_slots([left, right], [slot("G01", 100, 0, 200, 100)], FRAME)
    # each covers 20 of the slot's 100 px width
    assert r.score == pytest.approx(0.2)
    assert not r.taken


def test_mask_vs_box_bottom_on_angled_car():
    # A car in G01 seen at an angle: its box reaches down into G02 (the slot in front),
    # but its mask stays inside G01 apart from a thin sliver.
    g01, g02 = slot("G01", 0, 0, 100, 100), slot("G02", 0, 100, 100, 200)
    mask = [(10, 10), (90, 10), (90, 105), (10, 105)]
    det = car((0, 0, 100, 180), mask=mask)
    by_mask = scores(score_slots([det], [g01, g02], FRAME, mode="mask"))
    by_box = scores(score_slots([det], [g01, g02], FRAME, mode="box_bottom"))
    assert by_mask["G01"] > 0.6 and by_mask["G02"] < 0.1
    # box_bottom = y 117..180 → 63% of G02, none of G01: the box spilled into the neighbour
    assert by_box["G02"] == pytest.approx(0.63)
    assert by_box["G01"] == 0


@pytest.mark.parametrize(("covered", "taken"), [(0.30, True), (0.2999, False)])
def test_threshold_boundary(covered, taken):
    # slot is 0..10000 wide so the overlap ratio is exact to 4 decimals
    w = 10_000
    det = car((0, 0, 1, 1), mask=rect(0, 0, covered * w, 10))
    [r] = score_slots([det], [slot("G01", 0, 0, w, 10)], (w, 10), threshold=0.30)
    assert r.score == pytest.approx(covered)
    assert r.taken is taken


def test_no_detections_all_free():
    res = score_slots([], [slot("G01", 0, 0, 10, 10), slot("G02", 10, 0, 20, 10)], FRAME)
    assert [(r.id, r.score, r.taken) for r in res] == [("G01", 0, False), ("G02", 0, False)]


def test_far_detection_ignored():
    det = car((300, 200, 400, 300), mask=rect(300, 200, 400, 300))
    [r] = score_slots([det], [slot("G01", 0, 0, 100, 100)], FRAME)
    assert r.score == 0


def test_slots_rescaled_from_image_size():
    # slot drawn on a 2× larger reference image; the car covers it in frame pixels
    det = car((50, 50, 100, 100), mask=rect(50, 50, 100, 100))
    [r] = score_slots([det], [slot("G01", 100, 100, 200, 200)], (200, 150), image_size=(400, 300))
    assert r.score == pytest.approx(1.0)


def test_mask_mode_falls_back_to_box_bottom():
    no_mask = car((0, 0, 100, 100))
    degenerate = car((0, 0, 100, 100), mask=[(0, 0), (10, 10), (20, 20)])  # zero area
    s = slot("G01", 0, 65, 100, 100)
    assert score_slots([no_mask], [s], FRAME)[0].score == pytest.approx(1.0)
    assert score_slots([degenerate], [s], FRAME)[0].score == pytest.approx(1.0)
    assert footprint(no_mask, "mask").equals(footprint(no_mask, "box_bottom"))


def test_unknown_mode():
    with pytest.raises(ValueError, match="unknown mode"):
        score_slots([], [], FRAME, mode="box")  # type: ignore[arg-type]


def test_count_in_zones_bottom_centre():
    zones = [
        CountZone(zone="ground", polygon=rect(0, 100, 200, 300)),
        CountZone(zone="ground", polygon=rect(200, 100, 400, 300)),
        CountZone(zone="underground", polygon=rect(0, 0, 400, 50)),
    ]
    dets = [
        car((10, 20, 50, 150)),  # bottom-centre (30, 150) → ground, first polygon
        car((210, 120, 250, 160)),  # (230, 160) → ground, second polygon
        car((10, 0, 50, 90)),  # (30, 90) → in no zone, though its box overlaps ground
        car((180, 120, 220, 200)),  # (200, 200) → on the shared edge: counted once
    ]
    assert count_in_zones(dets, zones, FRAME) == {"ground": 3, "underground": 0}


def test_count_in_zones_rescaled():
    zones = [CountZone(zone="ground", polygon=rect(0, 0, 200, 200))]
    det = car((0, 0, 20, 80))  # (10, 80) in a half-size frame → (20, 160) in image pixels
    assert count_in_zones([det], zones, (200, 150), image_size=(400, 300)) == {"ground": 1}
    assert count_in_zones([car((0, 0, 20, 120))], zones, (200, 150), image_size=(400, 300)) == {
        "ground": 0
    }
