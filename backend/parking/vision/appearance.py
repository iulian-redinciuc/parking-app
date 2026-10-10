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
GAIN_RANGE = (0.5, 2.0)  # frame / reference lightness a slot may vote for
GAIN_CHROMA_MAX = 4.0  # ... if its a,b are this close to the reference's (scaled)
GAIN_SPREAD_MAX = 1.5  # ... and its colours spread at most this much wider than the reference's
GAIN_TOL = 0.04  # votes this close agree


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
    return colour, _spread(pooled, colour)


def _spread(pixels: np.ndarray, colour: np.ndarray) -> float:
    return float(np.median(np.linalg.norm(pixels - colour, axis=1)))


def illumination(labs: Sequence[np.ndarray], ref_labs: Sequence[np.ndarray]) -> float:
    """How much brighter (> 1) or darker the frame's pavement is than the reference's.

    Each slot that still looks like its own pavement votes with the ratio of its median
    lightness to the same slot's in the reference: the ratio must be plausible, the chroma
    the reference's (scaled) and the crop as even as the reference's (a car has windows, a
    roof and a bonnet). The answer is the median of the largest group of agreeing votes;
    1.0 when no slot votes (a full lot).
    """
    votes = []
    for lab, ref in zip(labs, ref_labs, strict=True):
        px, ref_px = lab.reshape(-1, 3), ref.reshape(-1, 3)
        m, r = np.median(px, axis=0), np.median(ref_px, axis=0)
        ratio = float(m[0] / max(float(r[0]), 1e-6))
        if (
            GAIN_RANGE[0] <= ratio <= GAIN_RANGE[1]
            and np.linalg.norm(m[1:] - r[1:] * ratio) <= GAIN_CHROMA_MAX
            and _spread(px, m) <= GAIN_SPREAD_MAX * _spread(ref_px, r) + 1
        ):
            votes.append(ratio)
    if not votes:
        return 1.0
    v = np.array(votes)
    groups = [v[np.abs(v - x) <= GAIN_TOL] for x in v]
    return float(np.median(max(groups, key=len)))


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
    of pooling the slots, which only works while about half the slots are free; its colour is
    scaled to the frame's light (`illumination`).
    """
    p = params or AppearanceCfg()
    polys = [_scaled(s.polygon, image_size, frame_size) for s in slots]
    labs = [to_lab(slot_crop(frame, poly, p.inset)) for poly in polys]
    if reference is not None:
        rh, rw = reference.shape[:2]
        ref_polys = [_scaled(s.polygon, image_size, (rw, rh)) for s in slots]
        ref_labs = [to_lab(slot_crop(reference, q, p.inset)) for q in ref_polys]
        colour, spread = pavement_model(ref_labs)
        colour = colour * illumination(labs, ref_labs)  # the light changes, the reference doesn't
    else:
        colour, spread = pavement_model(labs) if labs else (np.zeros(3), 0.0)
    results = []
    for slot, lab in zip(slots, labs, strict=True):
        score = largest_blob(non_pavement(lab, colour, spread, p), p.morph_frac)
        results.append(SlotResult(id=slot.id, score=score, taken=score >= threshold))
    return results
