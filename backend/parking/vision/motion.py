"""Motion gate (vision.md §7.2): is anything moving in the flow camera's ROI right now?

The flow worker runs the detector + tracker only while the gate is active, so an empty ramp
costs almost no CPU. Per frame:

1. Crop the ROI's bounding box (the line file's `roi`, scaled from its `image_size` to the
   frame; no ROI = the whole frame), black out what's outside the polygon and scale it to at
   most `WORK_WIDTH` px wide.
2. `cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=32, detectShadows=True)`;
   shadow pixels (127) are ignored, the rest is opened with a small kernel to drop noise.
3. Active when the largest contour's area is ≥ `motion_min_area_px`. The threshold is in
   pixels of the line file's `image_size` (the sub-stream the lines were drawn on; the frame's
   own pixels without a line file), scaled to the work image.
4. It stays active for `HOLD_S` after the last motion, so a car that stops on the ramp keeps
   being tracked.

The first frame only teaches the model the background (MOG2 marks it all shadow). A frame of
another size starts over with a fresh model.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from parking.config import LineFile
from parking.vision.sources import FrameSource

WORK_WIDTH = 320  # px; a 640×360 sub-stream ROI is halved
HISTORY = 500
VAR_THRESHOLD = 32
SHADOW = 127  # MOG2's value for shadow pixels
HOLD_S = 2.0
OPEN_KERNEL = 3  # px at work size


@dataclass(frozen=True)
class MotionResult:
    active: bool
    moving: bool  # motion in this frame (active may still be the hold-over)
    largest_area_px: float  # largest contour, in the threshold's pixels


class MotionGate:
    """Feed every frame to `update(image, ts)`; see the module docstring."""

    def __init__(
        self,
        min_area_px: float,
        lines: LineFile | None = None,
        hold_s: float = HOLD_S,
        work_width: int = WORK_WIDTH,
    ):
        self.min_area_px = min_area_px
        self.lines = lines
        self.hold_s = hold_s
        self.work_width = work_width
        self.reset()

    def reset(self) -> None:
        """Forget the background and the hold-over (after a reconnect or a reload)."""
        self._subtractor: cv2.BackgroundSubtractorMOG2 | None = None
        self._size: tuple[int, int] | None = None
        self._last_motion: float | None = None

    def _setup(self, w: int, h: int) -> None:
        self._size = (w, h)
        x0, y0, x1, y1 = 0, 0, w, h
        poly = None
        area_scale = 1.0  # frame pixels per line-file pixel
        if self.lines is not None:
            sw, sh = self.lines.image_size
            area_scale = (w / sw) * (h / sh)
        if self.lines is not None and self.lines.roi:
            poly = np.asarray(self.lines.roi, np.float64) * (w / sw, h / sh)
            x0, y0 = np.clip(np.floor(poly.min(axis=0)).astype(int), 0, (w, h))
            x1, y1 = np.clip(np.ceil(poly.max(axis=0)).astype(int), 0, (w, h))
            if x1 - x0 < 2 or y1 - y0 < 2:  # ROI outside the frame: use the whole frame
                x0, y0, x1, y1, poly = 0, 0, w, h, None
        self._crop = (slice(int(y0), int(y1)), slice(int(x0), int(x1)))
        cw, ch = int(x1 - x0), int(y1 - y0)
        self._scale = min(1.0, self.work_width / cw)
        self._work = (max(1, round(cw * self._scale)), max(1, round(ch * self._scale)))
        self._mask = None
        if poly is not None:
            mask = np.zeros((self._work[1], self._work[0]), np.uint8)
            pts = np.round((poly - (x0, y0)) * self._scale).astype(np.int32)
            cv2.fillPoly(mask, [pts], 255)
            self._mask = mask
        self._area_scale = area_scale * self._scale**2  # line-file px → work px
        self._min_area_work = self.min_area_px * self._area_scale
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (OPEN_KERNEL, OPEN_KERNEL))
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=HISTORY, varThreshold=VAR_THRESHOLD, detectShadows=True
        )

    def update(self, image: np.ndarray, ts: float) -> MotionResult:
        """`ts` in seconds (any monotonic clock, e.g. the frame's timestamp)."""
        h, w = image.shape[:2]
        if self._subtractor is None or self._size != (w, h):
            self.reset()
            self._setup(w, h)
        work = image[self._crop]
        if self._work != (work.shape[1], work.shape[0]):
            work = cv2.resize(work, self._work, interpolation=cv2.INTER_AREA)
        if self._mask is not None:
            work = cv2.bitwise_and(work, work, mask=self._mask)
        fg = self._subtractor.apply(work)
        fg[fg == SHADOW] = 0
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self._kernel)
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        largest = max((cv2.contourArea(c) for c in contours), default=0.0)
        moving = largest > 0 and largest >= self._min_area_work
        if moving:
            self._last_motion = ts
        active = self._last_motion is not None and ts - self._last_motion <= self.hold_s
        return MotionResult(active, moving, largest / self._area_scale)


@dataclass(frozen=True)
class MotionStats:
    frames: int
    active_frames: int
    seconds: float  # first to last frame timestamp
    bursts: int  # inactive → active switches

    @property
    def ratio(self) -> float:
        return self.active_frames / self.frames if self.frames else 0.0

    def to_dict(self) -> dict:
        return {
            "frames": self.frames,
            "active_frames": self.active_frames,
            "seconds": round(self.seconds, 2),
            "bursts": self.bursts,
            "ratio": round(self.ratio, 4),
        }


def measure_motion(
    src: FrameSource,
    gate: MotionGate,
    seconds: float,
    start_timeout: float = 20.0,
    poll_s: float = 0.005,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_frame: Callable[[float, MotionResult], None] | None = None,
) -> MotionStats:
    """Run `gate` over the new frames of `src` for `seconds` of **frame time** (the frames'
    `ts`, so a recording read with `realtime=false` still covers its own hour).

    Repeated frames (same `seq`) are skipped. Stops early when the source is exhausted (end of
    a recording) or no frame came within `start_timeout` s (wall clock).
    `on_frame(elapsed_s, result)` is called per frame.
    """
    frames = active = bursts = 0
    first = last = None
    last_seq: int | None = None
    was_active = False
    t0 = clock()
    while not getattr(src, "exhausted", False):
        if first is None and clock() - t0 >= start_timeout:
            break
        frame = src.read()
        if frame is None or (frame.seq is not None and frame.seq == last_seq):
            sleep(poll_s)
            continue
        last_seq = frame.seq
        ts = frame.ts.timestamp()
        if first is None:
            first = ts
        if ts - first >= seconds:
            break
        last = ts
        res = gate.update(frame.image, ts)
        frames += 1
        active += res.active
        bursts += res.active and not was_active
        was_active = res.active
        if on_frame is not None:
            on_frame(ts - first, res)
    span = last - first if first is not None and last is not None else 0.0
    return MotionStats(frames, active, span, bursts)
