import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from parking.config import SlotFile
from parking.vision.bootstrap import (
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
