"""Flow worker (vision.md §7, api.md §5): frames in, entry/exit events out.

For every **new** frame of the camera's source (the newest one; `rtsp:` drops the backlog):
read → health check (at most once per `HEALTH_CHECK_EVERY_S`, no `frozen` check: a quiet ramp
is meant to look the same) → `MotionGate` → `VehicleTracker` (detector + ByteTrack while the
gate is active, empty updates while idle) → `TwoLineCounter` → `ApiClient.queue_flow_events`
(the outbox delivers them, also across restarts). Every event gets a fresh `uuid4` `event_id`.

- No new frame for `NO_FRAME_S` → the health check records `connect_failed` (once a second).
- A gap of more than `RECONNECT_GAP_S` between two frames (reconnect, or a looping video
  starting over) resets the gate and the tracker; the counter forgets old tracks by itself.
- Health every 10 s (base `Worker`): `fps` over the last `STATS_WINDOW_S`, `inference_ms_avg`
  (gate + tracker on the frames the gate let through) and `gate_active_ratio`.
- `debug_video`: every processed frame annotated (ROI, lines, boxes + track ids, a banner with
  the time, gate state and the running IN/OUT) to an MP4, for checking.

One INFO line per event, an INFO summary every `SUMMARY_EVERY_S`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from parking.config import Camera, ConfigError, LineFile, load_lines
from parking.messages import CameraHealthMsg, FlowEventMsg
from parking.vision.detector import Detection
from parking.vision.flow import FlowEvent, TwoLineCounter
from parking.vision.health import FrameHealth
from parking.vision.motion import MotionGate
from parking.vision.sources import Frame, FrameSource, make_source
from parking.vision.tracking import RoiMask, TrackBackend, VehicleTracker, draw_tracks
from parking.workers.base import Worker, WorkerError, load_camera

log = logging.getLogger(__name__)

STATS_WINDOW_S = 10.0  # fps, inference_ms_avg and gate_active_ratio cover this many seconds
SUMMARY_EVERY_S = 60.0
HEALTH_CHECK_EVERY_S = 1.0
NO_FRAME_S = 5.0  # no new frame for this long = connect_failed
RECONNECT_GAP_S = 5.0  # frame-time gap that resets the gate and the tracker
POLL_S = 0.01  # wait between reads when there's no new frame
JPEG_QUALITY = 85


def _resolve(path: str | Path, root: Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else root / path


def make_backend(cam: Camera, root: Path) -> TrackBackend:
    """ByteTrack on the camera's exported detector (`detector` settings in lot.yaml)."""
    det = cam.detector
    model = _resolve(det.model, root)
    if not model.exists():
        raise WorkerError(f"model {model} not found; run `parking models export` first")
    from parking.vision.tracking import UltralyticsTracker

    return UltralyticsTracker(str(model), det.imgsz, det.conf, det.classes)


def load_line_file(cam: Camera, root: Path) -> LineFile:
    assert cam.lines_file is not None  # flow cameras always have one (config check)
    try:
        lines = load_lines(_resolve(cam.lines_file, root))
    except (ConfigError, ValueError, OSError) as e:
        raise WorkerError(f"line file for '{cam.id}': {e}") from None
    if lines.camera_id != cam.id:
        raise WorkerError(f"line file is for '{lines.camera_id}', not '{cam.id}'")
    return lines


def to_event_msg(camera_id: str, ev: FlowEvent) -> FlowEventMsg:
    """The api.md flow payload for one counted crossing (a new `event_id` every call)."""
    return FlowEventMsg(
        event_id=str(uuid.uuid4()),
        camera_id=camera_id,
        ts=datetime.fromtimestamp(ev.ts, UTC),
        direction=ev.direction,
        track_id=ev.track_id,
        cls=ev.cls,
        confidence=round(min(max(ev.conf, 0.0), 1.0), 3),
    )


class _Setup:
    """Everything that `reload` replaces at once."""

    def __init__(self, worker: FlowWorker, cam: Camera, old: _Setup | None = None):
        root = worker.root
        self.cam = cam
        self.lines = load_line_file(cam, root)
        if old is not None and old.cam.detector == cam.detector:
            backend = old.tracker.backend
        elif worker.backend_factory is not None:
            backend = worker.backend_factory(cam)
        else:
            backend = make_backend(cam, root)
        if old is not None and old.cam.source == cam.source:
            self.source = old.source
        else:
            try:
                self.source: FrameSource = make_source(cam.source, root)
            except (ValueError, NotImplementedError) as e:
                raise WorkerError(f"camera '{cam.id}' source: {e}") from None
        # a fresh gate (ROI) and counter; the tracker object is kept so ids keep rising
        self.gate = MotionGate(cam.flow.motion_min_area_px, self.lines)
        if old is None:
            self.tracker = VehicleTracker(backend, self.lines)
        else:
            self.tracker = old.tracker
            self.tracker.reset()
            self.tracker.backend = backend
            self.tracker.mask = RoiMask(self.lines)
        self.counter = TwoLineCounter(self.lines, cam.flow.min_track_frames)


class FlowWorker(Worker):
    role = "flow"

    def __init__(
        self,
        config_path: Path,
        camera_id: str,
        *,
        debug_video: Path | None = None,
        backend_factory=None,
        **kw,
    ):
        """`backend_factory(camera)` builds the `TrackBackend` (tests); default: the camera's
        exported YOLO model through ultralytics' ByteTrack."""
        self.backend_factory = backend_factory
        super().__init__(config_path, camera_id, **kw)
        self._lock = threading.Lock()
        try:
            self._setup = _Setup(self, self.camera)
        except WorkerError:
            self.api.close()
            raise
        self.health = FrameHealth(self.camera.health, check_frozen=False)
        self.debug_video = debug_video
        self._writer: Any = None
        self.frames = 0  # new frames processed
        self.counts = {"in": 0, "out": 0}
        self.errors = 0
        self._last_seq: int | None = None
        self._last_ts: float | None = None  # frame time of the last processed frame
        self._last_frame_at: float | None = None  # monotonic
        self._last_check_at: float | None = None  # monotonic, last health check
        self._started_at = time.monotonic()
        # (monotonic time, active, gate + tracker ms) per processed frame, last STATS_WINDOW_S
        self._recent: deque[tuple[float, bool, float]] = deque()
        self._latest: tuple[Frame, list[Detection], bool] | None = None
        self._summary = {"frames": 0, "active": 0, "in": 0, "out": 0}

    # --- the loop ---

    def loop(self, max_frames: int | None = None) -> None:
        s = self._setup
        log.info(
            "%s: flow worker, in = %s, min_track_frames %d, motion_min_area_px %d",
            self.camera_id,
            s.lines.in_direction.replace("_", " "),
            s.cam.flow.min_track_frames,
            s.cam.flow.motion_min_area_px,
        )
        next_summary = time.monotonic() + SUMMARY_EVERY_S
        try:
            while not self.stop_event.is_set():
                if max_frames is not None and self.frames >= max_frames:
                    break
                if not self.step():
                    if getattr(self._setup.source, "exhausted", False):
                        log.info("%s: source has no more frames", self.camera_id)
                        break
                    self.stop_event.wait(POLL_S)
                self.alive()
                if time.monotonic() >= next_summary:
                    self._log_summary()
                    next_summary += SUMMARY_EVERY_S
        finally:
            if self._writer is not None:
                self._writer.release()
                self._writer = None
                log.info("%s: debug video -> %s", self.camera_id, self.debug_video)

    def step(self) -> bool:
        """Process the source's newest frame. False if there was no new frame."""
        with self._lock:
            setup = self._setup
        now = time.monotonic()
        try:
            frame = setup.source.read()
        except Exception:
            log.exception("%s: reading a frame failed", self.camera_id)
            frame = None
        if frame is None or (frame.seq is not None and frame.seq == self._last_seq):
            since = self._last_frame_at if self._last_frame_at is not None else self._started_at
            if now - since >= NO_FRAME_S and self._check_due(now):
                self.health.check(None)
            return False
        self._last_seq = frame.seq
        self._last_frame_at = now
        if self._check_due(now):
            issue = self.health.check(frame)
            if issue:
                log.debug("%s: frame health: %s", self.camera_id, issue)
        ts = frame.ts.timestamp()
        if self._last_ts is not None and abs(ts - self._last_ts) > RECONNECT_GAP_S:
            log.info(
                "%s: %.0f s gap in the frames, gate and tracker reset",
                self.camera_id,
                ts - self._last_ts,
            )
            setup.gate.reset()
            setup.tracker.reset()
        self._last_ts = ts
        self.frames += 1
        t0 = time.perf_counter()
        try:
            active = setup.gate.update(frame.image, ts).active
            tracks = setup.tracker.update(frame.image, ts, active)
            h, w = frame.image.shape[:2]
            events = setup.counter.update(tracks, ts, (w, h))
        except Exception:
            self.errors += 1
            log.exception("%s: tracking failed", self.camera_id)
            return True
        ms = 1000 * (time.perf_counter() - t0)
        self._recent.append((now, active, ms))
        self._latest = (frame, tracks, active)
        self._summary["frames"] += 1
        self._summary["active"] += active
        if events:
            self._publish(events)
        if self.debug_video is not None:
            self._write_debug(frame, tracks, active, setup.lines)
        return True

    def _check_due(self, now: float) -> bool:
        if self._last_check_at is not None and now - self._last_check_at < HEALTH_CHECK_EVERY_S:
            return False
        self._last_check_at = now
        return True

    def _publish(self, events: list[FlowEvent]) -> None:
        msgs = [to_event_msg(self.camera_id, ev) for ev in events]
        for m in msgs:
            self.counts[m.direction] += 1
            self._summary[m.direction] += 1
            log.info(
                "%s: %s (track #%d %s %.2f), running in %d / out %d",
                self.camera_id,
                m.direction.upper(),
                m.track_id,
                m.cls,
                m.confidence,
                self.counts["in"],
                self.counts["out"],
            )
        self.api.queue_flow_events(msgs)

    def _write_debug(
        self, frame: Frame, tracks: list[Detection], active: bool, lines: LineFile
    ) -> None:
        image = frame.image
        if self._writer is None:
            assert self.debug_video is not None
            self.debug_video.parent.mkdir(parents=True, exist_ok=True)
            fps = getattr(self._setup.source, "native_fps", None) or self.camera.fps
            size = (image.shape[1], image.shape[0])
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(str(self.debug_video), fourcc, fps, size)
            if not self._writer.isOpened():
                self._writer = None
                self.debug_video = None
                log.error("%s: can't write the debug video, turned off", self.camera_id)
                return
        self._writer.write(draw_tracks(image, tracks, lines, self._banner(frame, active)))

    def _banner(self, frame: Frame, active: bool) -> str:
        return (
            f"{frame.ts:%H:%M:%S}  {'ACTIVE' if active else 'idle'}  "
            f"IN {self.counts['in']}  OUT {self.counts['out']}"
        )

    def _log_summary(self) -> None:
        s, self._summary = self._summary, {"frames": 0, "active": 0, "in": 0, "out": 0}
        log.info(
            "%s: last %.0f s: %d frames (%d gated in), in %d, out %d, %d pending in the outbox; "
            "running in %d / out %d",
            self.camera_id,
            SUMMARY_EVERY_S,
            s["frames"],
            s["active"],
            s["in"],
            s["out"],
            self.api.pending,
            self.counts["in"],
            self.counts["out"],
        )

    # --- health ---

    def window_stats(self, now: float | None = None) -> dict[str, float | None]:
        """fps, inference_ms_avg and gate_active_ratio over the last `STATS_WINDOW_S`."""
        now = time.monotonic() if now is None else now
        recent = self._recent
        while recent and now - recent[0][0] > STATS_WINDOW_S:
            recent.popleft()
        items = list(recent)
        if not items:
            return {"fps": None, "inference_ms_avg": None, "gate_active_ratio": None}
        span = min(STATS_WINDOW_S, now - self._started_at)
        active_ms = [ms for _, a, ms in items if a]
        return {
            "fps": round(len(items) / span, 2) if span > 0 else None,
            "inference_ms_avg": (round(sum(active_ms) / len(active_ms), 1) if active_ms else None),
            "gate_active_ratio": round(len(active_ms) / len(items), 3),
        }

    def health_message(self, final: bool = False) -> CameraHealthMsg:
        now = time.monotonic()
        if final or self.health.is_down(self.lot.api.stale_after_s):
            state = "down"
        elif self.health.degraded:
            state = "degraded"
        else:
            state = "ok"
        return CameraHealthMsg(
            camera_id=self.camera_id,
            ts=datetime.now(UTC),
            state=state,
            issue=self.health.last_issue,
            last_frame_age_s=(
                round(now - self._last_frame_at, 1) if self._last_frame_at is not None else None
            ),
            unhealthy_ratio=round(self.health.unhealthy_ratio, 3),
            started_at=self.started_at,
            **self.window_stats(now),
        )

    # --- control (api.md §5.2) ---

    def snapshot(self, annotated: bool) -> bytes | None:
        latest = self._latest
        if latest is None:
            return None
        frame, tracks, active = latest
        image = frame.image
        if annotated:
            image = draw_tracks(image, tracks, self._setup.lines, self._banner(frame, active))
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return buf.tobytes() if ok else None

    def reload(self) -> dict[str, Any]:
        """Re-read lot.yaml (this camera's section) and the line file; keep the old ones if
        anything is wrong. Tracks being counted right now start over."""
        try:
            lot, cam = load_camera(self.config_path, self.camera_id, self.role)
            with self._lock:
                old = self._setup
            setup = _Setup(self, cam, old)
        except WorkerError as e:
            raise ValueError(str(e)) from None
        with self._lock:
            self.lot = lot
            if setup.source is not old.source:
                old.source.close()
            self._setup = setup
            self.camera = cam
            self.health.cfg = cam.health
        log.info("%s: reloaded the line file", self.camera_id)
        return {"camera_id": self.camera_id, "in_direction": setup.lines.in_direction}

    def save_reference(self) -> dict[str, Any]:
        """Save the current frame as `data/reference/<camera>.jpg` (the frame the lines are
        drawn on in the editor)."""
        latest = self._latest
        if latest is None:
            raise ValueError("no frame read yet")
        path = self.root / "data" / "reference" / f"{self.camera_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.jpg")
        image: np.ndarray = latest[0].image
        if not cv2.imwrite(str(tmp), image):
            raise ValueError(f"could not write {path.name}")
        tmp.replace(path)
        return {"saved": f"data/reference/{path.name}", "ts": latest[0].ts.isoformat()}

    def shutdown(self) -> None:
        super().shutdown()
        self._setup.source.close()
