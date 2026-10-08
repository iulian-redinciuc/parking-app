"""Top-down appearance scoring (docs/design/vision.md §2.1): the MVP stand-in for cameras
looking straight down, where COCO detectors don't recognise cars.

A slot is scored by how much of it doesn't look like empty pavement. No model, no training;
the trained per-slot classifier (vision.md §9) replaces it once there are enough photos.
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np

from parking.config import AppearanceCfg, Slot
from parking.vision.occupancy import Size, SlotResult, _scaled

AppearanceParams = AppearanceCfg

BLUR_FRAC = 0.03  # light blur (fraction of the crop's short side) that hides paver joints


def _corners(points: Sequence[Sequence[float]]) -> np.ndarray:
    """4 corners in polygon order; polygons with more points use their minimum-area rectangle."""
    pts = np.asarray(points, dtype=np.float32)
    if len(pts) != 4:
        pts = cv2.boxPoints(cv2.minAreaRect(pts)).astype(np.float32)
    return pts


def slot_crop(frame: np.ndarray, points: Sequence[Sequence[float]], inset: float) -> np.ndarray:
    """The slot warped to an upright rectangle, shrunk by `inset` per side (BGR)."""
    c = _corners(points)
    w = (np.linalg.norm(c[1] - c[0]) + np.linalg.norm(c[2] - c[3])) / 2
    h = (np.linalg.norm(c[2] - c[1]) + np.linalg.norm(c[3] - c[0])) / 2
    w, h = max(int(round(w)), 1), max(int(round(h)), 1)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    crop = cv2.warpPerspective(frame, cv2.getPerspectiveTransform(c, dst), (w, h))
    dx, dy = int(w * inset), int(h * inset)
    return crop[dy : h - dy or h, dx : w - dx or w]


def to_lab(crop: np.ndarray) -> np.ndarray:
    """Lightly blurred float Lab (L 0..100)."""
    k = max(int(min(crop.shape[:2]) * BLUR_FRAC) | 1, 3)
    blurred = cv2.GaussianBlur(crop, (k, k), 0)
    return cv2.cvtColor(blurred.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)


def pavement_model(labs: Sequence[np.ndarray]) -> tuple[np.ndarray, float]:
    """Median Lab colour of the pooled pixels and the MAD of their distance to it."""
    pooled = np.concatenate([lab.reshape(-1, 3) for lab in labs])
    colour = np.median(pooled, axis=0)
    spread = float(np.median(np.linalg.norm(pooled - colour, axis=1)))
    return colour, spread


def non_pavement(lab: np.ndarray, colour: np.ndarray, spread: float, p: AppearanceCfg):
    """Boolean mask of pixels that don't look like pavement (shadows count as pavement)."""
    limit = max(p.k_mad * spread, p.min_delta_e)
    diff = np.linalg.norm(lab - colour, axis=2) > limit
    # shadow: darker, and the chroma shrinks with the lightness (same surface, less light)
    ratio = lab[..., 0] / max(float(colour[0]), 1e-6)
    lo, hi = p.shadow_l_range
    chroma_off = np.linalg.norm(lab[..., 1:] - colour[1:] * ratio[..., None], axis=2)
    shadow = (ratio >= lo) & (ratio <= hi) & (chroma_off <= p.shadow_chroma_max)
    return diff & ~shadow


def largest_blob(mask: np.ndarray, morph_frac: float) -> float:
    """Share of the crop covered by the largest blob after a morphological open + close."""
    k = max(int(min(mask.shape) * morph_frac), 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, kernel)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel)
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n <= 1:
        return 0.0
    return float(stats[1:, cv2.CC_STAT_AREA].max()) / m.size


def score_slots_appearance(
    frame: np.ndarray,
    slots: Sequence[Slot],
    frame_size: Size,
    image_size: Size | None = None,
    threshold: float = 0.30,
    params: AppearanceCfg | None = None,
    reference: np.ndarray | None = None,
) -> list[SlotResult]:
    """Score each slot by the share of its crop that isn't pavement (largest blob, 0..1).

    `frame` is BGR at `frame_size`; slot polygons are in `image_size` pixels and are rescaled.
    `reference` (an image of the empty lot, any size) is used for the pavement colour instead
    of pooling the slots, which only works while about half the slots are free.
    """
    p = params or AppearanceCfg()
    polys = [_scaled(s.polygon, image_size, frame_size) for s in slots]
    labs = [to_lab(slot_crop(frame, poly, p.inset)) for poly in polys]
    if reference is not None:
        rh, rw = reference.shape[:2]
        ref_polys = [_scaled(s.polygon, image_size, (rw, rh)) for s in slots]
        ref_labs = [to_lab(slot_crop(reference, q, p.inset)) for q in ref_polys]
        colour, spread = pavement_model(ref_labs)
    else:
        colour, spread = pavement_model(labs) if labs else (np.zeros(3), 0.0)
    results = []
    for slot, lab in zip(slots, labs, strict=True):
        score = largest_blob(non_pavement(lab, colour, spread, p), p.morph_frac)
        results.append(SlotResult(id=slot.id, score=score, taken=score >= threshold))
    return results
