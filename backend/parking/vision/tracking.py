"""Vehicle tracking for the flow camera (vision.md §7.1, P5.4).

`VehicleTracker.update(image, ts, active)` is called for every new frame, with the motion
gate's verdict:

- **active**: everything outside the line file's ROI is blacked out, then the backend runs
  detection + ByteTrack (`UltralyticsTracker`: `model.track(persist=True,
  tracker="bytetrack.yaml", classes=[2, 3, 5, 7], conf=0.4, imgsz=640)`) and returns
  `Detection`s with a `track_id`;
- **inactive**: no detection; the backend gets an empty update so lost tracks age as usual.
  After `RESET_AFTER_S` (10 s) inactive the tracker is reset to drop stale tracks. Not at
  once: a car that stops on the ramp keeps its id through a short idle spell.

ByteTrack restarts its ids at 1 after a reset; `VehicleTracker` adds an offset so a track id is
never reused while the process runs (the two-line counter keys its state by id).
`TrackLog` collects per-track first/last sightings and `draw_tracks` annotates a frame for
`parking track-check --debug-video`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import cv2
import numpy as np

from parking.config import LineFile
from parking.vision.annotate import BANNER_ALPHA, BLACK, YELLOW, _put_text, _thickness, text_scale
from parking.vision.detector import COCO_VEHICLES, Detection, class_ids
from parking.vision.lines import draw_lines
from parking.vision.motion import MotionGate
from parking.vision.sources import FrameSource

RESET_AFTER_S = 10.0
TRACKER_CFG = "bytetrack.yaml"

# one colour per track id (BGR), cycled
PALETTE = [
    (60, 200, 60),
    (230, 140, 30),
    (40, 40, 220),
    (0, 200, 255),
    (255, 0, 255),
    (255, 255, 0),
    (0, 128, 255),
    (180, 105, 255),
]


class TrackBackend(Protocol):
    def track(self, image: np.ndarray) -> list[Detection]:
        """Detect + associate on one (ROI-masked) frame; returns the confirmed tracks."""
        ...

    def idle(self, image: np.ndarray) -> None:
        """An empty update (no detections) for a frame the gate skipped."""
        ...

    def reset(self) -> None:
        """Drop every track; ids may start over."""
        ...


class UltralyticsTracker:
    """ByteTrack through ultralytics `model.track(persist=True)` on an exported YOLO model."""

    def __init__(
        self,
        model_path: str,
        imgsz: int = 640,
        conf: float = 0.4,
        classes: Iterable[str] = tuple(COCO_VEHICLES),
        tracker: str = TRACKER_CFG,
    ):
        from ultralytics import YOLO  # heavy import, only when a real model is used

        self.model = YOLO(str(model_path), task="detect")
        self.names: dict[int, str] = dict(self.model.names)
        self.imgsz = imgsz
        self.conf = conf
        self.classes = list(classes)
        self.tracker = tracker
        self._class_ids = class_ids(self.names, self.classes)

    def _trackers(self) -> list:
        predictor = self.model.predictor
        return list(getattr(predictor, "trackers", None) or []) if predictor is not None else []

    def track(self, image: np.ndarray) -> list[Detection]:
        result = self.model.track(
            image,
            persist=True,
            tracker=self.tracker,
            classes=self._class_ids,
            conf=self.conf,
            imgsz=self.imgsz,
            verbose=False,
        )[0]
        boxes = result.boxes
        if boxes is None or boxes.id is None:
            return []
        out = []
        for box, cid, c, tid in zip(
            boxes.xyxy.tolist(),
            boxes.cls.tolist(),
            boxes.conf.tolist(),
            boxes.id.tolist(),
            strict=True,
        ):
            name = self.names.get(int(cid))
            if name not in self.classes:
                continue
            x1, y1, x2, y2 = (float(v) for v in box)
            out.append(Detection(name, float(c), (x1, y1, x2, y2), track_id=int(tid)))
        return out

    def idle(self, image: np.ndarray) -> None:
        trackers = self._trackers()
        if not trackers:  # nothing tracked yet
            return
        from ultralytics.engine.results import Boxes

        empty = Boxes(np.empty((0, 6), np.float32), image.shape[:2])
        for t in trackers:
            t.update(empty, image)

    def reset(self) -> None:
        for t in self._trackers():
            t.reset()


class RoiMask:
    """Blacks out everything outside the line file's ROI (scaled from its `image_size`)."""

    def __init__(self, lines: LineFile | None):
        self.lines = lines
        self._size: tuple[int, int] | None = None
        self._mask: np.ndarray | None = None

    def __call__(self, image: np.ndarray) -> np.ndarray:
        if self.lines is None or not self.lines.roi:
            return image
        h, w = image.shape[:2]
        if self._size != (w, h):
            sw, sh = self.lines.image_size
            pts = np.round(np.asarray(self.lines.roi, np.float64) * (w / sw, h / sh))
            mask = np.zeros((h, w), np.uint8)
            cv2.fillPoly(mask, [pts.astype(np.int32)], 255)
            self._size, self._mask = (w, h), mask
        return cv2.bitwise_and(image, image, mask=self._mask)


class VehicleTracker:
    """Gate-aware wrapper around a `TrackBackend`; see the module docstring."""

    def __init__(
        self,
        backend: TrackBackend,
        lines: LineFile | None = None,
        reset_after_s: float = RESET_AFTER_S,
    ):
        self.backend = backend
        self.mask = RoiMask(lines)
        self.reset_after_s = reset_after_s
        self.resets = 0
        self._idle_since: float | None = None
        self._is_reset = True  # nothing to drop before the first track
        self._offset = 0  # added to the backend's ids, raised at every reset
        self._max_id = 0

    def update(self, image: np.ndarray, ts: float, active: bool) -> list[Detection]:
        """`ts` in seconds (the frame's timestamp); `active` = the motion gate's verdict."""
        if active:
            self._idle_since = None
            self._is_reset = False
            out = []
            for d in self.backend.track(self.mask(image)):
                tid = self._offset + int(d.track_id or 0)
                self._max_id = max(self._max_id, tid)
                out.append(Detection(d.cls, d.conf, d.box, d.mask, tid))
            return out
        if self._idle_since is None:
            self._idle_since = ts
        if self._is_reset:
            return []
        if ts - self._idle_since >= self.reset_after_s:
            self.reset()
        else:
            self.backend.idle(image)
        return []

    def reset(self) -> None:
        """Drop all tracks now (also after a reconnect); later ids stay above the old ones."""
        self.backend.reset()
        self._offset = self._max_id
        self._is_reset = True
        self.resets += 1


def anchor(box: Sequence[float]) -> tuple[float, float]:
    """Bottom-centre of a box (the point the two-line counter follows, vision.md §7.3)."""
    x1, _, x2, y2 = box
    return ((x1 + x2) / 2, y2)


@dataclass
class TrackSpan:
    track_id: int
    cls: str
    first_ts: float
    last_ts: float
    frames: int = 0
    first_anchor: tuple[float, float] = (0.0, 0.0)
    last_anchor: tuple[float, float] = (0.0, 0.0)

    @property
    def seconds(self) -> float:
        return self.last_ts - self.first_ts

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "cls": self.cls,
            "first_ts": round(self.first_ts, 3),
            "seconds": round(self.seconds, 2),
            "frames": self.frames,
            "first_anchor": [round(v, 1) for v in self.first_anchor],
            "last_anchor": [round(v, 1) for v in self.last_anchor],
        }


@dataclass
class TrackLog:
    """Where each track id was first and last seen, for checking one id per passing car."""

    spans: dict[int, TrackSpan] = field(default_factory=dict)

    def add(self, tracks: Iterable[Detection], ts: float) -> None:
        for t in tracks:
            if t.track_id is None:
                continue
            p = anchor(t.box)
            span = self.spans.get(t.track_id)
            if span is None:
                span = self.spans[t.track_id] = TrackSpan(t.track_id, t.cls, ts, ts, 0, p, p)
            span.frames += 1
            span.last_ts, span.last_anchor = ts, p


@dataclass(frozen=True)
class TrackStats:
    frames: int
    active_frames: int
    seconds: float  # first to last frame timestamp
    track_ms_avg: float  # mean backend time per active frame (mask + detect + track)
    resets: int
    spans: list[TrackSpan]

    def to_dict(self) -> dict:
        return {
            "frames": self.frames,
            "active_frames": self.active_frames,
            "seconds": round(self.seconds, 2),
            "track_ms_avg": round(self.track_ms_avg, 1),
            "resets": self.resets,
            "tracks": [s.to_dict() for s in self.spans],
        }


def draw_tracks(
    image: np.ndarray,
    tracks: Sequence[Detection],
    lines: LineFile | None = None,
    banner: str | None = None,
) -> np.ndarray:
    """Copy of `image` with the ROI/lines, each track's box, id and anchor, and a banner."""
    out = draw_lines(image, lines) if lines is not None else image.copy()
    w = out.shape[1]
    scale = text_scale(w) * 0.8
    th = _thickness(scale, 2.5)
    for t in tracks:
        colour = PALETTE[(t.track_id or 0) % len(PALETTE)]
        x1, y1, x2, y2 = (round(v) for v in t.box)
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, th, cv2.LINE_AA)
        ax, ay = (round(v) for v in anchor(t.box))
        cv2.circle(out, (ax, ay), th + 2, colour, -1, cv2.LINE_AA)
        label = f"#{t.track_id} {t.cls} {t.conf:.2f}"
        _put_text(out, label, (x1, max(int(18 * scale), y1 - 6)), scale, colour)
    if banner:
        bh = int(28 * scale) + 8
        strip = out[:bh].copy()
        cv2.rectangle(strip, (0, 0), (w, bh), BLACK, -1)
        out[:bh] = cv2.addWeighted(strip, BANNER_ALPHA, out[:bh], 1 - BANNER_ALPHA, 0)
        _put_text(out, banner, (8, bh - 10), scale, YELLOW)
    return out


def run_tracking(
    src: FrameSource,
    gate: MotionGate,
    tracker: VehicleTracker,
    seconds: float,
    start_timeout: float = 20.0,
    poll_s: float = 0.005,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    on_frame: Callable[[np.ndarray, float, bool, list[Detection]], None] | None = None,
) -> TrackStats:
    """Gate + track the new frames of `src` for `seconds` of frame time.

    Stops early when the source is exhausted or no frame came within `start_timeout` s.
    `on_frame(image, elapsed_s, active, tracks)` is called per frame (e.g. a video writer).
    """
    log = TrackLog()
    frames = active_frames = 0
    busy = 0.0
    first = last = None
    last_seq: int | None = None
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
        active = gate.update(frame.image, ts).active
        t1 = clock()
        tracks = tracker.update(frame.image, ts, active)
        if active:
            busy += clock() - t1
            active_frames += 1
        frames += 1
        log.add(tracks, ts)
        if on_frame is not None:
            on_frame(frame.image, ts - first, active, tracks)
    span = last - first if first is not None and last is not None else 0.0
    ms = 1000 * busy / active_frames if active_frames else 0.0
    spans = sorted(log.spans.values(), key=lambda s: (s.first_ts, s.track_id))
    return TrackStats(frames, active_frames, span, ms, tracker.resets, spans)
