"""Draw slots, detections and totals on a frame (docs/design/vision.md §2, annotated output).

Everything is drawn on a copy of the BGR frame; line widths and text size scale with the
frame width so the result stays readable from 720p up to 4K, and on a phone screen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import cv2
import numpy as np

from parking.config import Slot
from parking.vision.detector import Detection
from parking.vision.occupancy import Size, SlotResult

# BGR colours
GREEN = (60, 200, 60)
RED = (40, 40, 220)
YELLOW = (0, 220, 255)
MAGENTA = (255, 0, 255)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

FONT = cv2.FONT_HERSHEY_SIMPLEX
FILL_ALPHA = 0.35  # opacity of the red fill on taken slots
BANNER_ALPHA = 0.7


def text_scale(width: int) -> float:
    """cv2 font scale for a frame `width` px wide: ~1.0 at 1280, ~3.0 at 3840."""
    return max(0.5, width / 1280)


def _thickness(scale: float, factor: float = 2.0) -> int:
    return max(1, round(scale * factor))


def _put_text(img: np.ndarray, text: str, org: tuple[int, int], scale: float, color=WHITE):
    """Text with a dark outline so it reads on any background."""
    th = _thickness(scale)
    outline = th + max(2, _thickness(scale, 2.5))
    cv2.putText(img, text, org, FONT, scale, BLACK, outline, cv2.LINE_AA)
    cv2.putText(img, text, org, FONT, scale, color, th, cv2.LINE_AA)


def _points(points, sx: float, sy: float) -> np.ndarray:
    arr = np.asarray(points, dtype=np.float64).reshape(-1, 2) * (sx, sy)
    return np.round(arr).astype(np.int32)


def banner_text(totals: Mapping[str, tuple[int, int]], inference_ms: float | None = None) -> str:
    """'Ground: 12 free / 40 | 143 ms' (one part per zone; `totals` is zone -> (free, capacity)).

    ASCII only: cv2's Hershey fonts draw any other character (such as '·') as '?'.
    """
    parts = [f"{zone.capitalize()}: {free} free / {cap}" for zone, (free, cap) in totals.items()]
    if inference_ms is not None:
        parts.append(f"{inference_ms:.0f} ms")
    return " | ".join(parts)


def annotate_occupancy(
    frame: np.ndarray,
    slots: Sequence[Slot],
    results: Sequence[SlotResult],
    detections: Sequence[Detection],
    totals: Mapping[str, tuple[int, int]],
    inference_ms: float | None = None,
    image_size: Size | None = None,
) -> np.ndarray:
    """An annotated copy of `frame` (BGR).

    - slots: green outline if free, red translucent fill if taken, with id + score
    - detections: mask (or box if no mask) in thin yellow
    - a top banner with `totals` (zone -> (free, capacity)) and the inference time

    Slot polygons are in `image_size` pixels and are rescaled to the frame, as in
    `score_slots`; `None` means they are already in frame pixels.
    """
    out = frame.copy()
    h, w = out.shape[:2]
    sx, sy = (w / image_size[0], h / image_size[1]) if image_size else (1.0, 1.0)
    scale = text_scale(w)
    by_id = {r.id: r for r in results}
    polys = {s.id: _points(s.polygon, sx, sy) for s in slots}

    # translucent fill of taken slots, blended in one pass
    taken = [polys[sid] for sid, r in by_id.items() if r.taken and sid in polys]
    if taken:
        overlay = out.copy()
        cv2.fillPoly(overlay, taken, RED)
        out = cv2.addWeighted(overlay, FILL_ALPHA, out, 1 - FILL_ALPHA, 0)

    thin = _thickness(scale, 1.5)
    for det in detections:
        if det.mask is not None and len(det.mask) >= 3:
            cv2.polylines(out, [_points(det.mask, 1, 1)], True, YELLOW, thin, cv2.LINE_AA)
        else:
            x1, y1, x2, y2 = (round(v) for v in det.box)
            cv2.rectangle(out, (x1, y1), (x2, y2), YELLOW, thin, cv2.LINE_AA)

    outline = _thickness(scale, 3)
    for s in slots:
        r = by_id.get(s.id)
        color = RED if r is not None and r.taken else GREEN
        cv2.polylines(out, [polys[s.id]], True, color, outline, cv2.LINE_AA)

    # labels last so outlines never cover them
    label_scale = max(0.5, scale * 0.8)
    for s in slots:
        r = by_id.get(s.id)
        lines = [s.id] if r is None else [s.id, f"{r.score:.2f}"]
        cx, cy = polys[s.id].mean(axis=0)
        line_h = cv2.getTextSize("Ag", FONT, label_scale, _thickness(label_scale))[0][1] * 1.6
        top = cy - line_h * (len(lines) - 1) / 2
        for i, text in enumerate(lines):
            tw, th = cv2.getTextSize(text, FONT, label_scale, _thickness(label_scale))[0]
            x = min(max(cx - tw / 2, 2), w - tw - 2)  # keep labels of edge slots in frame
            org = (round(x), round(top + i * line_h + th / 2))
            _put_text(out, text, org, label_scale)

    text = banner_text(totals, inference_ms)
    if text:
        # shrink the banner font if the text would not fit the width
        bscale = scale * 1.2
        tw, th = cv2.getTextSize(text, FONT, bscale, _thickness(bscale))[0]
        if tw > w * 0.96:
            bscale *= w * 0.96 / tw
            tw, th = cv2.getTextSize(text, FONT, bscale, _thickness(bscale))[0]
        bh = round(th * 2.2)
        band = out[:bh].copy()
        band[:] = BLACK
        out[:bh] = cv2.addWeighted(band, BANNER_ALPHA, out[:bh], 1 - BANNER_ALPHA, 0)
        _put_text(out, text, (round(w * 0.02), round((bh + th) / 2)), bscale)
    return out


def highlight_slots(
    frame: np.ndarray,
    slots: Sequence[Slot],
    notes: Mapping[str, str],
    image_size: Size | None = None,
) -> np.ndarray:
    """A copy of `frame` with the slots in `notes` (id -> short text) outlined thickly in magenta
    and the note written near the bottom of each one (used for evaluation mistakes)."""
    out = frame.copy()
    h, w = out.shape[:2]
    sx, sy = (w / image_size[0], h / image_size[1]) if image_size else (1.0, 1.0)
    scale = max(0.5, text_scale(w) * 0.7)
    for s in slots:
        if s.id not in notes:
            continue
        poly = _points(s.polygon, sx, sy)
        cv2.polylines(out, [poly], True, MAGENTA, _thickness(scale, 6), cv2.LINE_AA)
        text = notes[s.id]
        tw, th = cv2.getTextSize(text, FONT, scale, _thickness(scale))[0]
        cx = poly[:, 0].mean()
        y = poly[:, 1].max() - th  # under the id/score label, inside the slot
        x = min(max(cx - tw / 2, 2), w - tw - 2)
        _put_text(out, text, (round(x), round(min(max(y, th + 2), h - 2))), scale, MAGENTA)
    return out
