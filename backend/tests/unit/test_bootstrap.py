import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from parking.config import SlotFile
from parking.vision.bootstrap import (
    CarryError,
    carry_slots,
    footprint,
    format_json,
    propose_slots,
    reading_order,
    slot_file,
)
from parking.vision.detector import Detection, FakeDetector

FIXTURES = Path(__file__).parent.parent / "fixtures"
REPO = Path(__file__).resolve().parents[3]


def _car(box, conf=0.9):
    return Detection(cls="car", conf=conf, box=box)


def test_footprint_box_bottom_shrunk_10_percent():
    fp = footprint(_car((0, 0, 100, 200)))
    # bottom 35% of the box is 100 × 70 at y 130..200; shrunk 10% around its centre
    assert fp.bounds == pytest.approx((5, 133.5, 95, 196.5))
    assert len(fp.exterior.coords) == 5  # 4 corners + closing point


def test_footprint_box_kind_uses_whole_box():
    assert footprint(_car((0, 0, 100, 200)), "box").bounds == pytest.approx((5, 10, 95, 190))
    assert footprint(_car((0, 0, 100, 200)), "box", shrink=0).area == 100 * 200


def test_reading_order_rows_then_columns():
    def sq(x, y):
        return Polygon([(x, y), (x + 10, y), (x + 10, y + 10), (x, y + 10)])

    # second row is slightly skewed: its first space sits 4 px lower than the next
    polys = [sq(50, 100), sq(0, 104), sq(30, 2), sq(0, 0), sq(60, 1)]
    ordered = reading_order(polys)
    assert [(p.bounds[0], p.bounds[1]) for p in ordered] == [
        (0, 0), (30, 2), (60, 1), (0, 104), (50, 100)
    ]  # fmt: skip
    assert reading_order([]) == []


def test_propose_slots_names_dedupes_and_orders():
    dets = [
        _car((300, 0, 400, 100), 0.8),
        _car((0, 0, 100, 100), 0.9),
        _car((2, 1, 101, 99), 0.3),  # same car twice: the weaker one is dropped
        _car((0, 300, 100, 400), 0.7),
        _car((50, 50, 50, 80), 0.9),  # zero-width box: no footprint
    ]
    slots = propose_slots(dets, "underground")
    assert [s["id"] for s in slots] == ["U01", "U02", "U03"]
    assert [s["polygon"][0][0] for s in slots] == [5, 305, 5]
    assert all(s["zone"] == "underground" and s["type"] == "standard" for s in slots)
    assert all(len(s["polygon"]) == 4 for s in slots)


def test_propose_slots_none():
    assert propose_slots([], "ground") == []


def test_ids_grow_past_99():
    dets = [_car((i * 20, 0, i * 20 + 10, 10)) for i in range(100)]
    ids = [s["id"] for s in propose_slots(dets, "ground")]
    assert ids[0] == "G01" and ids[-1] == "G100" and len(set(ids)) == 100


def test_fixture_file_matches_and_is_valid():
    """The committed fixture is what bootstrapping the fixture detections gives.

    `tools/slot-editor/editor.test.js` opens the same file, so a bootstrapped file is known
    to load in the editor and to export back unchanged.
    """
    dets = FakeDetector(FIXTURES / "bootstrap-detections.json", conf=0.25).detect(None)
    data = slot_file("cam-test", (1000, 800), propose_slots(dets, "ground"), "data/x.jpg")
    text = format_json(data)
    assert text == (FIXTURES / "bootstrap-cam-test.json").read_text()
    loaded = SlotFile.model_validate_json(text)
    # person and the 0.10 car are filtered; the 0.30 duplicate is dropped
    assert [s.id for s in loaded.slots] == ["G01", "G02", "G03", "G04", "G05"]
    assert loaded.count_zones == []


@pytest.mark.parametrize("path", ["config/slots/cam-ground.json", "data/labels/cam-ground.json"])
def test_format_json_matches_the_editor_layout(path):
    file = REPO / path
    if not file.is_file():
        pytest.skip(f"{path} not present")
    text = file.read_text()
    assert format_json(json.loads(text)) == text


def test_format_json_small_cases():
    assert format_json({}) == "{}\n"
    assert format_json({"a": [], "b": "é"}) == '{\n  "a": [],\n  "b": "é"\n}\n'
    assert format_json({"x": [{}]}) == '{\n  "x": [\n    {}\n  ]\n}\n'


def test_slot_file_without_reference():
    assert "reference_image" not in slot_file("cam", (10, 10), [])


# --- carry_slots (P4.5): an old slot file moved onto another view of the lot ---

CARRY_SLOTS = [  # one row of spaces across the middle of a 960×540 view
    [[200 + 110 * i, 200], [290 + 110 * i, 200], [290 + 110 * i, 340], [200 + 110 * i, 340]]
    for i in range(5)
]


def scene(seed: int, w: int = 960, h: int = 540):
    """A lot-like view: grey gradient with lots of random shapes (corners for ORB)."""
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    img = cv2.cvtColor(np.tile(np.linspace(70, 150, w, dtype=np.uint8), (h, 1)), cv2.COLOR_GRAY2BGR)
    for _ in range(400):
        x, y, s = int(rng.integers(0, w)), int(rng.integers(0, h)), int(rng.integers(4, 32))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        cv2.rectangle(img, (x, y), (x + s, y + int(rng.integers(4, s + 5))), color, -1)
    return img


def with_cars(img, seed: int):
    """New "cars" (plain rectangles) in some of the spaces."""
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    out = img.copy()
    for x in range(210, 700, 110):
        if rng.random() < 0.6:
            color = tuple(int(c) for c in rng.integers(0, 256, 3))
            cv2.rectangle(out, (x, 210), (x + 70, 330), color, -1)
    return out


def _old_slots(polys=CARRY_SLOTS, size=(960, 540)) -> SlotFile:
    return SlotFile.model_validate(
        {
            "version": 1,
            "camera_id": "cam",
            "image_size": list(size),
            "slots": [
                {"id": f"G{i + 1:02d}", "zone": "ground", "polygon": p, "type": t}
                for i, (p, t) in enumerate(
                    zip(
                        polys, ["standard", "ev", "standard", "accessible", "standard"], strict=True
                    )
                )
            ],
        }
    )


def _remount(img, out_size, k=40.0, turn=3.0, move=(30, -20)):
    """Another mount: turned, moved and tilted (a keystone), into `out_size` (w, h)."""
    import cv2
    import numpy as np

    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[k, 0], [w - k, 0], [w, h], [0, h]]) * 0.9
    a = np.deg2rad(turn)
    rot = np.float32([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    dst = (dst - dst.mean(0)) @ rot.T + np.float32(
        [out_size[0] / 2 + move[0], out_size[1] / 2 + move[1]]
    )
    hom = cv2.getPerspectiveTransform(src, dst.astype(np.float32))
    return cv2.warpPerspective(img, hom, out_size, borderValue=(30, 30, 30)), hom


def test_carry_slots_follows_a_remounted_camera_at_another_size():
    import cv2
    import numpy as np

    old_ref = scene(1)
    new, hom = _remount(with_cars(old_ref, 5), (1200, 900))  # other cars, other resolution
    carried = carry_slots(_old_slots(), old_ref, new)
    assert carried.inliers >= 100 and carried.outside == []
    assert [(s["id"], s["zone"], s["type"]) for s in carried.slots] == [
        ("G01", "ground", "standard"),
        ("G02", "ground", "ev"),
        ("G03", "ground", "standard"),
        ("G04", "ground", "accessible"),
        ("G05", "ground", "standard"),
    ]
    for s, poly in zip(carried.slots, CARRY_SLOTS, strict=True):
        truth = cv2.perspectiveTransform(np.float64([poly]), hom)[0]
        assert np.abs(np.float64(s["polygon"]) - truth).max() <= 3  # whole pixels
        assert all(isinstance(v, int) for pt in s["polygon"] for v in pt)
    SlotFile.model_validate(slot_file("cam", (1200, 900), carried.slots))


def test_carry_slots_scales_the_slot_file_to_its_reference():
    """A slot file drawn at half the reference image's size still lands right."""
    import numpy as np

    old_ref = scene(2)
    half = [[[x // 2, y // 2] for x, y in p] for p in CARRY_SLOTS]
    carried = carry_slots(_old_slots(half, (480, 270)), old_ref, old_ref.copy())
    for s, poly in zip(carried.slots, CARRY_SLOTS, strict=True):
        assert np.abs(np.float64(s["polygon"]) - np.float64(poly)).max() <= 2


def test_carry_slots_clips_and_lists_spaces_outside_the_new_view():

    old_ref = scene(3)
    new = old_ref[:, :560].copy()  # the new view stops in the middle of the row
    carried = carry_slots(_old_slots(), old_ref, new)
    assert carried.outside == ["G04", "G05"]
    assert max(x for s in carried.slots for x, _ in s["polygon"]) == 559


def test_carry_slots_refuses_another_place():
    import numpy as np

    with pytest.raises(CarryError, match="same lot"):
        carry_slots(_old_slots(), scene(4), scene(99))
    with pytest.raises(CarryError, match="no features"):
        carry_slots(_old_slots(), scene(4), np.zeros((540, 960, 3), np.uint8))
