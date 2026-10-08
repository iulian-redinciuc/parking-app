"""First draft of a slot file from vehicle detections (docs/design/vision.md §4).

Each detected vehicle becomes one 4-point slot: its footprint, shrunk 10%, named in reading
order. Empty spaces aren't found and angles are rough, so the result is fixed by hand in
`tools/slot-editor`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from statistics import median
from typing import Any, Literal

import shapely
from shapely import affinity
from shapely.geometry import Polygon

from parking.geometry import box_bottom
from parking.vision.detector import Detection

Footprint = Literal["box_bottom", "box"]

SHRINK = 0.10  # each side of the footprint loses this fraction
MAX_IOU = 0.5  # a footprint overlapping a more confident one more than this is a duplicate


def footprint(det: Detection, kind: Footprint = "box_bottom", shrink: float = SHRINK) -> Polygon:
    """The detection's footprint (`box_bottom` for angled views, `box` seen from above), shrunk."""
    shape = box_bottom(det.box) if kind == "box_bottom" else shapely.box(*det.box)
    return affinity.scale(shape, 1 - shrink, 1 - shrink, origin="centroid")


def _iou(a: Polygon, b: Polygon) -> float:
    union = a.union(b).area
    return a.intersection(b).area / union if union > 0 else 0.0


def reading_order(polys: Sequence[Polygon]) -> list[Polygon]:
    """Sort into rows top→bottom, each row left→right.

    A polygon joins the current row while its centre is within half the median height of
    the row's first one; otherwise it starts a new row.
    """
    if not polys:
        return []
    half = median(p.bounds[3] - p.bounds[1] for p in polys) / 2
    rows: list[list[Polygon]] = []
    for p in sorted(polys, key=lambda p: p.centroid.y):
        if rows and p.centroid.y - rows[-1][0].centroid.y <= half:
            rows[-1].append(p)
        else:
            rows.append([p])
    return [p for row in rows for p in sorted(row, key=lambda p: p.centroid.x)]


def _corners(poly: Polygon) -> list[list[int]]:
    """Top-left, top-right, bottom-right, bottom-left, rounded to whole pixels."""
    x1, y1, x2, y2 = (round(v) for v in poly.bounds)
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def propose_slots(
    detections: Sequence[Detection],
    zone: str,
    kind: Footprint = "box_bottom",
    shrink: float = SHRINK,
) -> list[dict[str, Any]]:
    """Slot dicts (config.md §2) named `<Z>01…` with `Z` the zone's first letter."""
    kept: list[Polygon] = []
    for det in sorted(detections, key=lambda d: -d.conf):
        fp = footprint(det, kind, shrink)
        if fp.area > 0 and all(_iou(fp, k) <= MAX_IOU for k in kept):
            kept.append(fp)
    prefix = zone[:1].upper()
    return [
        {"id": f"{prefix}{n:02d}", "zone": zone, "polygon": _corners(p), "type": "standard"}
        for n, p in enumerate(reading_order(kept), start=1)
    ]


def slot_file(
    camera_id: str,
    image_size: tuple[int, int],
    slots: list[dict[str, Any]],
    reference_image: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"version": 1, "camera_id": camera_id, "image_size": list(image_size)}
    if reference_image:
        out["reference_image"] = reference_image
    out["slots"] = slots
    out["count_zones"] = []
    return out


def format_json(value: Any) -> str:
    """JSON laid out like the slot editor's `formatJson`, so an import + export is unchanged."""

    def dump(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False)  # like JSON.stringify

    def inline(v: Any) -> str:
        if isinstance(v, list):
            return "[" + ", ".join(inline(x) for x in v) + "]"
        if isinstance(v, dict):
            if not v:
                return "{}"
            return "{ " + ", ".join(f"{dump(k)}: {inline(x)}" for k, x in v.items()) + " }"
        return dump(v)

    def fmt(v: Any, indent: str) -> str:
        pad = indent + "  "
        if isinstance(v, list):
            if not v or all(not isinstance(x, dict) for x in v):
                return inline(v)
            return "[\n" + ",\n".join(pad + inline(x) for x in v) + f"\n{indent}]"
        if isinstance(v, dict):
            if len(indent) >= 4:
                return inline(v)
            if not v:
                return "{}"
            body = ",\n".join(f"{pad}{dump(k)}: {fmt(x, pad)}" for k, x in v.items())
            return "{\n" + body + f"\n{indent}}}"
        return dump(v)

    return fmt(value, "") + "\n"
