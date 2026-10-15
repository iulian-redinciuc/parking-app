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
CAST_MAX = 8.0  # how far the frame's pavement a,b may be from the reference's (a colour cast)
CAST_TOL = 3.0  # slots whose a,b are this close agree on the cast
CAST_MIN_SLOTS = 3  # fewer agreeing slots than this → lightness only (`illumination`)
LIGHT_GRID = (32, 24)  # blocks the frame is compared with the reference in (`match_light`)
LIGHT_MIN = 6.0  # reference blocks darker than this (0..255) don't vote
LIGHT_ROUNDS = 5  # robust refits; blocks further than LIGHT_K x MAD from the fit drop out
LIGHT_K = 2.5
LIGHT_OUTSIDE = 0.9  # blocks at least this much outside the slots vote
LIGHT_MIN_SHARE = 0.1  # ... and at least this share of all blocks must, or no relighting
GREY_CHROMA_MAX = 2.0  # mean |B-G|, |R-G| below this = a grey (infrared) frame


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


def _gain_field(
    frame: np.ndarray, reference: np.ndarray, polys: Sequence[Sequence[Sequence[float]]] = ()
) -> np.ndarray | None:
    """Per-channel gain (frame / reference) as a smooth surface over the image, shape (h, w, 3).

    The frame and the reference are averaged in `LIGHT_GRID` blocks; only blocks outside the
    slot polygons `polys` vote (the lot's unchanging background: aisles, lines, walls), so a lot
    full of cars of one colour can't pass for the light. Each channel's log ratio is fitted
    with a quadratic in x, y (a dimmer day, a warm dusk, an infrared light that is bright in the
    middle), refitted `LIGHT_ROUNDS` times without the blocks that disagree (a car in the aisle,
    a person). None when too little background is left to go by (`LIGHT_MIN_SHARE`).
    """
    h, w = frame.shape[:2]
    gw, gh = LIGHT_GRID
    outside = np.ones((h, w), np.float32)
    for poly in polys:
        cv2.fillPoly(outside, [np.round(np.asarray(poly)).astype(np.int32)], 0.0)
    outside = cv2.resize(outside, (gw, gh), interpolation=cv2.INTER_AREA).reshape(-1)
    f = cv2.resize(frame, (gw, gh), interpolation=cv2.INTER_AREA).astype(np.float32)
    r = cv2.resize(reference, (gw, gh), interpolation=cv2.INTER_AREA).astype(np.float32)
    y, x = np.mgrid[0:gh, 0:gw].astype(np.float32)
    x, y = (x + 0.5) / gw - 0.5, (y + 0.5) / gh - 0.5
    terms = np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=-1).reshape(-1, 6)
    gains = []
    for c in range(3):
        ok = (r[..., c] >= LIGHT_MIN).reshape(-1) & (outside >= LIGHT_OUTSIDE)
        if ok.sum() < LIGHT_MIN_SHARE * gw * gh:
            return None
        ratio = np.log(np.maximum(f[..., c], 0.5) / np.maximum(r[..., c], 0.5)).reshape(-1)
        use = ok.copy()
        for _ in range(LIGHT_ROUNDS):
            coef = np.linalg.lstsq(terms[use], ratio[use], rcond=None)[0]
            res = np.abs(ratio - terms @ coef)
            mad = float(np.median(res[use])) + 1e-3
            use = ok & (res <= LIGHT_K * 1.4826 * mad)
            if use.sum() < LIGHT_MIN_SHARE * gw * gh / 2:
                return None
        gains.append(np.exp(terms @ coef).reshape(gh, gw).astype(np.float32))
    # the quadratic is smooth, so the block grid scaled up linearly is the same surface
    return cv2.resize(np.stack(gains, axis=-1), (w, h), interpolation=cv2.INTER_LINEAR)


def is_grey(image: np.ndarray) -> bool:
    """An infrared (night mode) picture: no colour."""
    px = cv2.resize(image, (64, 48), interpolation=cv2.INTER_AREA).astype(np.float32)
    b, g, r = px[..., 0], px[..., 1], px[..., 2]
    return float(np.mean(np.abs(b - g)) + np.mean(np.abs(r - g))) / 2 < GREY_CHROMA_MAX


def match_light(
    frame: np.ndarray, reference: np.ndarray, polys: Sequence[Sequence[Sequence[float]]] = ()
) -> tuple[np.ndarray, np.ndarray]:
    """The frame relit to the reference's light, and the reference to compare it with.

    The reference (the empty lot, any size) is resized to the frame and, when the frame is grey
    (the camera's infrared night mode), made grey too. The frame is divided by the smooth
    per-channel gain between the two outside the slots `polys` (frame pixels; `_gain_field`),
    so the pavement looks as it does in the reference whatever the time of day: what's left is
    cars, and noise. The frame is left as it is when there's too little background to go by.
    """
    h, w = frame.shape[:2]
    ref = cv2.resize(reference, (w, h), interpolation=cv2.INTER_AREA)
    if is_grey(frame) and not is_grey(ref):
        ref = cv2.cvtColor(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    gain = _gain_field(frame, ref, polys)
    if gain is None:
        return frame, ref
    relit = frame.astype(np.float32) / gain
    return np.clip(relit, 0, 255).astype(np.uint8), ref


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


def frame_pavement(
    labs: Sequence[np.ndarray], ref_labs: Sequence[np.ndarray], ref_colour: np.ndarray
) -> np.ndarray | None:
    """The reference pavement colour as this frame's light shows it, from the slots that
    still look empty.

    The light changes more than its strength: a warmer or cooler cast, another white balance.
    A slot is a candidate when its lightness is plausible against its own reference crop, the
    crop is as even as the reference's and its median a,b lie within `CAST_MAX` of the
    reference pavement's. The largest group of candidates that agree on a,b (`CAST_TOL`)
    gives the colour: a,b are the median of their pooled pixels, and the lightness is the
    reference's times the median of their lightness ratios (per slot against its own
    reference crop, so it doesn't matter whether the free slots are in the bright or the dim
    part of the lot). None when fewer than `CAST_MIN_SLOTS` agree (a nearly full lot), so a
    few evenly painted cars can't pass for pavement.
    """
    found = []
    for lab, ref in zip(labs, ref_labs, strict=True):
        px, ref_px = lab.reshape(-1, 3), ref.reshape(-1, 3)
        m, r = np.median(px, axis=0), np.median(ref_px, axis=0)
        ratio = float(m[0] / max(float(r[0]), 1e-6))
        if (
            GAIN_RANGE[0] <= ratio <= GAIN_RANGE[1]
            and np.linalg.norm(m[1:] - ref_colour[1:]) <= CAST_MAX
            and _spread(px, m) <= GAIN_SPREAD_MAX * _spread(ref_px, r) + 1
        ):
            found.append((m[1:], px[:, 1:], ratio))
    if len(found) < CAST_MIN_SLOTS:
        return None
    ab = np.array([f[0] for f in found])
    groups = [np.flatnonzero(np.linalg.norm(ab - x, axis=1) <= CAST_TOL) for x in ab]
    best = max(groups, key=len)
    if len(best) < CAST_MIN_SLOTS:
        return None
    gain = float(np.median([found[i][2] for i in best]))
    cast = np.median(np.concatenate([found[i][1] for i in best]), axis=0)
    return np.array([ref_colour[0] * gain, cast[0], cast[1]], dtype=ref_colour.dtype)


def non_pavement(
    lab: np.ndarray, colour: np.ndarray, spread: float, p: AppearanceCfg, shadows: bool = True
):
    """Boolean mask of pixels that don't look like pavement (shadows count as pavement, unless
    `shadows` is off: in a grey picture every darker pixel would pass for one)."""
    limit = max(p.k_mad * spread, p.min_delta_e)
    diff = np.linalg.norm(lab - colour, axis=2) > limit
    if not shadows:
        return diff
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
    of pooling the slots, which only works while about half the slots are free. Its colour
    follows the frame's light: the colour of the slots that still look empty
    (`frame_pavement`), or the reference's scaled to the frame's lightness (`illumination`)
    when too few are left.
    """
    p = params or AppearanceCfg()
    # infrared at night: lit from the camera, so no sun shadows, and no colour to tell them by
    shadows = not is_grey(frame)
    polys = [_scaled(s.polygon, image_size, frame_size) for s in slots]
    if reference is not None and p.match_light:
        frame, reference = match_light(frame, reference, polys)
    labs = [to_lab(slot_crop(frame, poly, p.inset)) for poly in polys]
    if reference is not None:
        rh, rw = reference.shape[:2]
        ref_polys = [_scaled(s.polygon, image_size, (rw, rh)) for s in slots]
        ref_labs = [to_lab(slot_crop(reference, q, p.inset)) for q in ref_polys]
        colour, spread = pavement_model(ref_labs)
        seen = frame_pavement(labs, ref_labs, colour)  # the light changes, the reference doesn't
        colour = seen if seen is not None else colour * illumination(labs, ref_labs)
    else:
        colour, spread = pavement_model(labs) if labs else (np.zeros(3), 0.0)
    results = []
    for slot, lab in zip(slots, labs, strict=True):
        score = largest_blob(non_pavement(lab, colour, spread, p, shadows), p.morph_frac)
        results.append(SlotResult(id=slot.id, score=score, taken=score >= threshold))
    return results
