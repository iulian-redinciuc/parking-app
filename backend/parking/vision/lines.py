"""Sanity checks and an overlay for a flow camera's line file (config.md §3, P5.1).

`check_lines` catches lines that the two-line counter can't use: too short, crossing each
other, not roughly parallel, too close together, outside the frame or the ROI, or drawn on a
frame of a different shape than the reference. `draw_lines` renders the ROI, both lines
and the IN direction on the reference frame so a person can confirm the whole lane is seen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np
from shapely.geometry import LineString, Point

from parking.config import LineFile
from parking.geometry import to_polygon
from parking.vision.annotate import GREEN, MAGENTA, WHITE, YELLOW, _put_text, _thickness, text_scale

MIN_LENGTH_FRAC = 0.15  # each line ≥ 15% of the frame diagonal
MIN_GAP_FRAC = 0.03  # lines ≥ 3% of the diagonal apart (~20 px at 640×360)
MAX_GAP_FRAC = 0.45  # farther apart leaves no room to track before/after the lines
MAX_ANGLE_DEG = 25.0  # lines roughly parallel
ASPECT_TOLERANCE = 0.02

BLUE = (230, 140, 30)  # BGR


@dataclass
class LineCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    length_a: float = 0.0
    length_b: float = 0.0
    gap: float = 0.0
    angle: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors


def _angle(line) -> float:
    (x1, y1), (x2, y2) = line
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0


def _midpoint(line) -> tuple[float, float]:
    (x1, y1), (x2, y2) = line
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def check_lines(lf: LineFile, frame_size: tuple[int, int] | None = None) -> LineCheck:
    """Check `lf`; `frame_size` = (w, h) of the reference frame, if there is one."""
    res = LineCheck()
    w, h = lf.image_size
    diag = math.hypot(w, h)
    a, b = LineString(lf.line_a), LineString(lf.line_b)
    res.length_a, res.length_b = a.length, b.length

    if frame_size is not None:
        fw, fh = frame_size
        if abs(fw / fh - w / h) > ASPECT_TOLERANCE * (w / h):
            res.errors.append(
                f"lines were drawn on a {w}x{h} frame but the reference frame is {fw}x{fh} "
                "(different shape; redraw on the reference frame)"
            )
        elif (fw, fh) != (w, h):
            res.warnings.append(f"lines drawn at {w}x{h}, reference frame is {fw}x{fh} (scaled)")

    for name, line in (("line_a", lf.line_a), ("line_b", lf.line_b)):
        if any(not (0 <= x <= w and 0 <= y <= h) for x, y in line):
            res.errors.append(f"{name} has a point outside the {w}x{h} frame")
        length = LineString(line).length
        if length < MIN_LENGTH_FRAC * diag:
            res.errors.append(
                f"{name} is {length:.0f} px long; draw it across the whole lane "
                f"(≥ {MIN_LENGTH_FRAC * diag:.0f} px)"
            )

    if a.intersects(b):
        res.errors.append("line_a and line_b touch or cross; draw them about a car length apart")
    da = abs(_angle(lf.line_a) - _angle(lf.line_b))
    res.angle = min(da, 180.0 - da)
    if res.angle > MAX_ANGLE_DEG:
        res.errors.append(
            f"line_a and line_b are {res.angle:.0f}° apart; make them roughly parallel "
            f"(≤ {MAX_ANGLE_DEG:g}°), both across the direction of travel"
        )

    # gap: distance from each line's midpoint to the other segment, averaged
    gap_a = a.distance(Point(_midpoint(lf.line_b)))
    res.gap = (gap_a + b.distance(Point(_midpoint(lf.line_a)))) / 2
    if not a.intersects(b):
        if res.gap < MIN_GAP_FRAC * diag:
            res.errors.append(
                f"lines are only {res.gap:.0f} px apart; a car must be able to cross one "
                f"and then the other (≥ {MIN_GAP_FRAC * diag:.0f} px, about a car length)"
            )
        elif res.gap > MAX_GAP_FRAC * diag:
            res.warnings.append(
                f"lines are {res.gap:.0f} px apart (most of the frame); about a car length "
                "is enough and leaves room before and after them"
            )

    if lf.roi is not None:
        roi = to_polygon(lf.roi)
        for name, line in (("line_a", a), ("line_b", b)):
            inside = line.intersection(roi).length / line.length if line.length else 0.0
            if inside < 0.9:
                res.errors.append(
                    f"only {inside:.0%} of {name} is inside the ROI; the detector only looks "
                    "inside it"
                )
    return res


def draw_lines(img: np.ndarray, lf: LineFile) -> np.ndarray:
    """Copy of the BGR `img` with the ROI, line A (green), line B (blue) and the IN arrow."""
    out = img.copy()
    h, w = out.shape[:2]
    sx, sy = w / lf.image_size[0], h / lf.image_size[1]
    scale = text_scale(w) * 0.8
    th = _thickness(scale, 3)

    def px(p):
        return (round(p[0] * sx), round(p[1] * sy))

    if lf.roi is not None:
        pts = np.array([px(p) for p in lf.roi], np.int32)
        cv2.polylines(out, [pts], True, YELLOW, th, cv2.LINE_AA)
    for label, line, colour in (("A", lf.line_a, GREEN), ("B", lf.line_b, BLUE)):
        cv2.line(out, px(line[0]), px(line[1]), colour, th + 1, cv2.LINE_AA)
        x, y = px(line[0])
        _put_text(out, label, (x + 4, max(int(20 * scale), y - 6)), scale, colour)
    start, end = _midpoint(lf.line_a), _midpoint(lf.line_b)
    if lf.in_direction == "b_to_a":
        start, end = end, start
    cv2.arrowedLine(out, px(start), px(end), MAGENTA, th + 1, cv2.LINE_AA, tipLength=0.25)
    mx, my = px(_midpoint((start, end)))
    _put_text(out, "IN", (mx + 6, my), scale, WHITE)
    return out
