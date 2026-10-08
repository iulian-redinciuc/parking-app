"""Frame health checks (vision.md §5), run on every frame before detection.

Each frame is checked on a 320-px-wide grayscale copy, in this order (first match wins):

- `connect_failed`: the source gave no frame (`check(None)`).
- `black`: mean brightness < `black_mean_max`.
- `frozen`: mean absolute difference vs the previous frame < `frozen_diff_max` for
  `frozen_frames` frames in a row. Off for replay sources (`check_frozen=False`), which repeat
  stored images on purpose.
- `blurry`: variance of the Laplacian < `blur_laplacian_min`.

`FrameHealth` also keeps the last `WINDOW` results: `unhealthy_ratio`, `degraded` (> 50% of
them unhealthy) and `is_down()` (no healthy frame for `stale_after_s`).
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Literal

import cv2
import numpy as np

from parking.config import HealthCfg
from parking.vision.sources import Frame

Issue = Literal["black", "frozen", "blurry", "connect_failed"]

CHECK_WIDTH = 320
WINDOW = 20
DEGRADED_RATIO = 0.5


def small_gray(image: np.ndarray, width: int = CHECK_WIDTH) -> np.ndarray:
    """Grayscale copy scaled to `width` px wide (never upscaled), as float32."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    h, w = gray.shape[:2]
    if w > width:
        gray = cv2.resize(gray, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    return gray.astype(np.float32)


def blur_score(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


class FrameHealth:
    def __init__(self, cfg: HealthCfg | None = None, check_frozen: bool = True):
        self.cfg = cfg or HealthCfg()
        self.check_frozen = check_frozen
        self.last_issue: Issue | None = None
        self.last_healthy_at: datetime | None = None
        self._started_at = datetime.now(UTC)
        self._prev: np.ndarray | None = None
        self._still = 0  # frames in a row that barely differ from the one before
        self._recent: deque[bool] = deque(maxlen=WINDOW)  # True = unhealthy

    def check(self, frame: Frame | None) -> Issue | None:
        """Check one frame, record the result, return the issue (or `None` if healthy)."""
        issue = self._issue(frame)
        self.last_issue = issue
        self._recent.append(issue is not None)
        if issue is None:
            self.last_healthy_at = frame.ts if frame is not None else datetime.now(UTC)
        return issue

    def _issue(self, frame: Frame | None) -> Issue | None:
        if frame is None or frame.image is None or frame.image.size == 0:
            return "connect_failed"
        gray = small_gray(frame.image)
        frozen = self._update_frozen(gray)
        if gray.mean() < self.cfg.black_mean_max:
            return "black"
        if frozen:
            return "frozen"
        if blur_score(gray) < self.cfg.blur_laplacian_min:
            return "blurry"
        return None

    def _update_frozen(self, gray: np.ndarray) -> bool:
        prev, self._prev = self._prev, gray
        if not self.check_frozen:
            return False
        if prev is not None and prev.shape == gray.shape:
            still = float(np.abs(gray - prev).mean()) < self.cfg.frozen_diff_max
            self._still = self._still + 1 if still else 0
        else:
            self._still = 0
        return self._still >= self.cfg.frozen_frames

    @property
    def unhealthy_ratio(self) -> float:
        """Share of the last `WINDOW` frames that were unhealthy (0 before the first frame)."""
        return sum(self._recent) / len(self._recent) if self._recent else 0.0

    @property
    def degraded(self) -> bool:
        return self.unhealthy_ratio > DEGRADED_RATIO

    def is_down(self, stale_after_s: float, now: datetime | None = None) -> bool:
        """No healthy frame for `stale_after_s` (counted from start-up if there never was one)."""
        since = self.last_healthy_at or self._started_at
        return ((now or datetime.now(UTC)) - since).total_seconds() > stale_after_s
