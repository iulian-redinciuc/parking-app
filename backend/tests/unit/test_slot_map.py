"""P9.1: the slot map generator and reader (docs/design/slot-map.md)."""

from pathlib import Path
from xml.etree import ElementTree

import pytest

from parking.config import SlotFile, load_slots
from parking.slot_map import (
    MAX_BYTES,
    SVG_NS,
    SlotMapError,
    build_slot_map,
    compare_slot_map,
    map_slot_ids,
)

REPO = Path(__file__).resolve().parents[3]


def slot_file(*slots, zone="ground"):
    return SlotFile.model_validate(
        {
            "version": 1,
            "camera_id": "cam-ground",
            "image_size": [2000, 2000],
            "slots": [{"id": i, "zone": z, "polygon": p} for i, p, z in _zoned(slots, zone)],
        }
    )


def _zoned(slots, zone):
    return [(s[0], s[1], s[2] if len(s) > 2 else zone) for s in slots]


def box(x, y, w, h):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def rects(svg):
    root = ElementTree.fromstring(svg)
    return root, {
        el.get("id"): {k: el.get(k) for k in ("x", "y", "width", "height", "transform")}
        for el in root.iter(f"{{{SVG_NS}}}rect")
    }


def test_one_rect_per_slot_laid_out_as_in_the_picture():
    slots = slot_file(("G01", box(100, 100, 400, 200)), ("G02", box(100, 300, 400, 200)))
    root, shapes = rects(build_slot_map(slots, "ground", gap=0))
    # 400 x 400 px of slots -> 960 units + a 20 unit border
    assert root.get("viewBox") == "0 0 1000 1000"
    assert root.get("data-zone") == "ground" and root.get("data-camera") == "cam-ground"
    assert shapes == {
        "G01": {"x": "20", "y": "20", "width": "960", "height": "480", "transform": None},
        "G02": {"x": "20", "y": "500", "width": "960", "height": "480", "transform": None},
    }


def test_gap_keeps_neighbours_apart():
    slots = slot_file(("G01", box(0, 0, 400, 200)), ("G02", box(0, 200, 400, 200)))
    _, shapes = rects(build_slot_map(slots, "ground", gap=0.1))
    first, second = shapes["G01"], shapes["G02"]
    assert float(first["y"]) + float(first["height"]) < float(second["y"])


def test_slightly_turned_slots_are_drawn_upright_and_others_keep_their_angle():
    tilted = [[0, 0], [400, 35], [383, 234], [-17, 199]]  # about 5 degrees
    diagonal = [[1000, 0], [1283, 283], [1141, 424], [859, 141]]  # 45 degrees
    slots = slot_file(("G01", tilted), ("G02", diagonal))
    _, shapes = rects(build_slot_map(slots, "ground"))
    assert shapes["G01"]["transform"] is None
    assert float(shapes["G01"]["width"]) > float(shapes["G01"]["height"])
    assert shapes["G02"]["transform"].startswith(
        ("rotate(45 ", "rotate(-45 ", "rotate(44.", "rotate(-44.")
    )

    _, exact = rects(build_slot_map(slots, "ground", snap_deg=0))
    assert exact["G01"]["transform"].startswith("rotate(5")


def test_only_the_zone_asked_for_and_ids_are_escaped():
    slots = slot_file(
        ("A&B", box(0, 0, 10, 20)), ("G02", box(20, 0, 10, 20)), ("U01", box(40, 0, 10, 20), "up")
    )
    svg = build_slot_map(slots, "ground")
    assert map_slot_ids(svg) == ["A&B", "G02"]
    with pytest.raises(SlotMapError, match="no slots of zone 'roof'"):
        build_slot_map(slots, "roof")


def test_map_slot_ids_reads_hand_drawn_maps():
    svg = f"""<svg xmlns="{SVG_NS}" viewBox="0 0 10 10">
      <g id="layer1"><polygon id="G01" points="0,0 1,0 1,1"/><path id="G02" d="M0 0h1v1z"/></g>
      <line id="lane" x1="0" y1="0" x2="1" y2="1"/><rect width="1" height="1"/>
    </svg>"""
    assert map_slot_ids(svg) == ["G01", "G02"]


@pytest.mark.parametrize(
    ("svg", "message"),
    [
        ("<svg", "not valid XML"),
        ('<svg viewBox="0 0 1 1"/>', "root element"),
        (f'<svg xmlns="{SVG_NS}"/>', "viewBox"),
    ],
)
def test_map_slot_ids_rejects_other_files(svg, message):
    with pytest.raises(SlotMapError, match=message):
        map_slot_ids(svg)


def test_compare_names_missing_and_unknown_ids():
    slots = slot_file(("G01", box(0, 0, 10, 20)), ("G02", box(20, 0, 10, 20)))
    svg = build_slot_map(slots, "ground")
    assert compare_slot_map(svg, slots, "ground") == ([], [])
    svg = svg.replace('id="G02"', 'id="G99"')
    assert compare_slot_map(svg, slots, "ground") == (["G02"], ["G99"])


def test_committed_ground_map_matches_the_committed_slot_file():
    slots = load_slots(REPO / "config" / "slots" / "cam-ground.json")
    path = REPO / "config" / "maps" / "ground.svg"
    assert compare_slot_map(path.read_text(), slots, "ground") == ([], [])
    assert path.stat().st_size < MAX_BYTES
