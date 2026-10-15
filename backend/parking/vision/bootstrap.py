"""First draft of a slot file (docs/design/vision.md §4).

From vehicle detections: each detected vehicle becomes one 4-point slot, its footprint shrunk
10%, named in reading order. Empty spaces aren't found and angles are rough.

Or carried over from an existing slot file (`carry_slots`, P4.5): the old view's reference
image is matched with the new one and every polygon is moved through the homography between
them. The spaces lie on one plane (the ground), so a homography maps them exactly wherever the
match is good; a new camera at the same window starts from the slots drawn on the sample photo.

Either way the result is checked and fixed by hand in `tools/slot-editor`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any, Literal

import cv2
import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import Polygon

from parking.config import SlotFile
from parking.geometry import box_bottom
from parking.vision.detector import Detection
from parking.vision.shift import RATIO, WORK_WIDTH, background_mask, work_gray

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


CARRY_FEATURES = 4000  # ORB features per image (more than the shift check: any view, one-off)
CARRY_RANSAC_PX = 4.0  # reprojection threshold at work size
CARRY_MIN_INLIERS = 25


class CarryError(ValueError):
    """The two views don't match well enough to carry the slots over."""


@dataclass(frozen=True)
class Carried:
    slots: list[dict[str, Any]]
    inliers: int
    matches: int
    homography: np.ndarray  # old slot-file pixels → new image pixels
    outside: list[str]  # slots with a corner outside the new image (clipped to it)


def carry_slots(old: SlotFile, old_reference: np.ndarray, image: np.ndarray) -> Carried:
    """Move `old`'s slots (drawn on `old_reference`) onto `image`, another view of the lot.

    ORB features of both images at `WORK_WIDTH` px, the old one only on the background (outside
    its slot polygons, where cars come and go), ratio test, then a RANSAC homography. Ids,
    zones and types are kept; corners are rounded and clipped to the new image.
    """
    rw = old_reference.shape[1]
    nw, nh = image.shape[1], image.shape[0]
    ref = work_gray(old_reference, min(WORK_WIDTH, rw))
    cur = work_gray(image, min(WORK_WIDTH, nw))
    orb = cv2.ORB_create(CARRY_FEATURES)
    ref_kp, ref_des = orb.detectAndCompute(ref, background_mask(old, (ref.shape[1], ref.shape[0])))
    kp, des = orb.detectAndCompute(cur, None)
    if ref_des is None or des is None or len(ref_kp) < 2 or len(kp) < 2:
        raise CarryError("no features to match (a blank or black image?)")
    good = [
        pair[0]
        for pair in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(ref_des, des, k=2)
        if len(pair) == 2 and pair[0].distance < RATIO * pair[1].distance
    ]
    if len(good) < CARRY_MIN_INLIERS:
        raise CarryError(f"only {len(good)} matching features: is it the same lot?")
    src = np.float32([ref_kp[m.queryIdx].pt for m in good])
    dst = np.float32([kp[m.trainIdx].pt for m in good])
    h_work, inl = cv2.findHomography(src, dst, cv2.RANSAC, CARRY_RANSAC_PX)
    n = 0 if inl is None else int(inl.sum())
    if h_work is None or n < CARRY_MIN_INLIERS:
        raise CarryError(f"only {n} of {len(good)} matches agree on one view: is it the same lot?")

    sw, sh = old.image_size
    slot_to_ref_work = np.diag([ref.shape[1] / sw, ref.shape[0] / sh, 1.0])
    cur_work_to_image = np.diag([nw / cur.shape[1], nh / cur.shape[0], 1.0])
    homography = cur_work_to_image @ h_work @ slot_to_ref_work
    slots: list[dict[str, Any]] = []
    outside: list[str] = []
    for s in old.slots:
        pts = cv2.perspectiveTransform(np.float64([s.polygon]), homography)[0]
        if (pts < 0).any() or (pts[:, 0] > nw - 1).any() or (pts[:, 1] > nh - 1).any():
            outside.append(s.id)
        pts = np.clip(np.round(pts), 0, [nw - 1, nh - 1]).astype(int)
        slots.append({"id": s.id, "zone": s.zone, "polygon": pts.tolist(), "type": s.type})
    return Carried(slots, n, len(good), homography, outside)


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
