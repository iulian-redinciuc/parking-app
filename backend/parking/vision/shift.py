"""Camera shift detection (vision.md §6): has the camera moved since the reference frame?

A bumped camera makes every slot polygon point at the wrong pixels. Every
`shift_check_every_s` the occupancy worker compares a healthy frame with the reference frame
(`data/reference/<camera>.jpg`, saved by `save_reference` / `parking grab`):

1. Both are scaled to `WORK_WIDTH` px wide grayscale (the current frame first to the
   reference's size). ORB features (`ORB_FEATURES`) are found only on the **static
   background**: the inverse of the slot polygons, each grown by `SLOT_MARGIN` of the image
   width so cars overhanging their space don't count.
2. Ratio-test matching (`RATIO`), then `cv2.estimateAffinePartial2D` with RANSAC.
3. The **median displacement of the inlier matches**, in pixels at reference size, is compared
   with `shift_max_px`. Over it in `SHIFT_CHECKS` checks in a row → `shifted`.

A check with fewer than `MIN_INLIERS` inliers (a featureless frame: fog, night against a
daytime reference) is **inconclusive**: it neither counts towards nor resets the run. A view
turned far away still finds a few chance inliers with a huge displacement, so it does count as
shifted. `shifted` never clears by itself; a new reference (or a reload) starts over with
`reset()`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from parking.config import HealthCfg, SlotFile

WORK_WIDTH = 1280
ORB_FEATURES = 1000
RATIO = 0.75
RANSAC_PX = 3.0  # reprojection threshold at work size
MIN_INLIERS = 12
SHIFT_CHECKS = 3
SLOT_MARGIN = 0.01  # slot polygons grow by this share of the image width
MIN_BACKGROUND = 0.05  # less background than this share of the image → use the whole frame


@dataclass(frozen=True)
class ShiftMeasure:
    # median inlier displacement in reference pixels; None = inconclusive
    displacement_px: float | None
    matches: int  # after the ratio test
    inliers: int


def _gray(image: np.ndarray, width: int) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    h, w = gray.shape[:2]
    if w != width:
        interp = cv2.INTER_AREA if w > width else cv2.INTER_LINEAR
        gray = cv2.resize(gray, (width, max(1, round(h * width / w))), interpolation=interp)
    return gray


def background_mask(slot_file: SlotFile | None, size: tuple[int, int]) -> np.ndarray:
    """255 outside the slot polygons (grown by `SLOT_MARGIN`), 0 inside; `size` = (w, h).
    The whole frame if there are no slots or almost no background is left."""
    w, h = size
    mask = np.full((h, w), 255, np.uint8)
    if slot_file is None or not slot_file.slots:
        return mask
    sw, sh = slot_file.image_size
    scale = np.array([w / sw, h / sh])
    polys = [np.round(np.asarray(s.polygon) * scale).astype(np.int32) for s in slot_file.slots]
    cv2.fillPoly(mask, polys, 0)
    grow = max(1, round(SLOT_MARGIN * w))
    mask = cv2.erode(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * grow + 1, 2 * grow + 1)))
    if np.count_nonzero(mask) < MIN_BACKGROUND * w * h:
        return np.full((h, w), 255, np.uint8)
    return mask


class ShiftDetector:
    """Compares frames with one reference frame; see the module docstring."""

    def __init__(
        self,
        reference: np.ndarray,
        slot_file: SlotFile | None,
        cfg: HealthCfg | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg or HealthCfg()
        self.clock = clock
        self.ref_size = (reference.shape[1], reference.shape[0])
        self.work_width = min(WORK_WIDTH, self.ref_size[0])
        self.scale = self.ref_size[0] / self.work_width  # work px → reference px
        ref = _gray(reference, self.work_width)
        self.mask = background_mask(slot_file, (ref.shape[1], ref.shape[0]))
        self._orb = cv2.ORB_create(ORB_FEATURES)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self._ref_kp, self._ref_des = self._orb.detectAndCompute(ref, self.mask)
        self.shifted = False
        self.over = 0  # checks in a row over `shift_max_px`
        self.last: ShiftMeasure | None = None
        self._last_check: float | None = None

    def reset(self) -> None:
        self.shifted = False
        self.over = 0
        self.last = None
        self._last_check = None

    def measure(self, image: np.ndarray) -> ShiftMeasure:
        """Median displacement of `image` against the reference (no state change)."""
        if (image.shape[1], image.shape[0]) != self.ref_size:
            image = cv2.resize(image, self.ref_size, interpolation=cv2.INTER_AREA)
        cur = _gray(image, self.work_width)
        kp, des = self._orb.detectAndCompute(cur, self.mask)
        if des is None or self._ref_des is None or len(kp) < 2 or len(self._ref_kp) < 2:
            return ShiftMeasure(None, 0, 0)
        good = [
            pair[0]
            for pair in self._matcher.knnMatch(self._ref_des, des, k=2)
            if len(pair) == 2 and pair[0].distance < RATIO * pair[1].distance
        ]
        if len(good) < MIN_INLIERS:
            return ShiftMeasure(None, len(good), 0)
        src = np.float32([self._ref_kp[m.queryIdx].pt for m in good])
        dst = np.float32([kp[m.trainIdx].pt for m in good])
        model, inl = cv2.estimateAffinePartial2D(
            src, dst, method=cv2.RANSAC, ransacReprojThreshold=RANSAC_PX
        )
        if model is None or inl is None:
            return ShiftMeasure(None, len(good), 0)
        keep = inl.ravel().astype(bool)
        n = int(keep.sum())
        if n < MIN_INLIERS:
            return ShiftMeasure(None, len(good), n)
        moved = np.linalg.norm(dst[keep] - src[keep], axis=1)
        return ShiftMeasure(float(np.median(moved)) * self.scale, len(good), n)

    def due(self) -> bool:
        return self._last_check is None or (
            self.clock() - self._last_check >= self.cfg.shift_check_every_s
        )

    def check(self, image: np.ndarray, force: bool = False) -> ShiftMeasure | None:
        """Measure if a check is due (or `force`), update the run and `shifted`; None if not due."""
        if not (force or self.due()):
            return None
        self._last_check = self.clock()
        m = self.last = self.measure(image)
        if m.displacement_px is not None:
            if m.displacement_px > self.cfg.shift_max_px:
                self.over += 1
                if self.over >= SHIFT_CHECKS:
                    self.shifted = True
            else:
                self.over = 0
        return m
