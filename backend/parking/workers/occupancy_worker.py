"""Occupancy worker (vision.md §2, api.md §5): frames in, observations out.

Every `interval` seconds (the `folder:` source's `interval`, otherwise the camera's
`sample_every_s`): read a frame → health check (unhealthy frames are recorded and skipped)
→ `analyze_frame` (the same pipeline as `parking analyze`) → `ApiClient.send_observation`.

One DEBUG line per observation, an INFO summary every `SUMMARY_EVERY_S`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from parking.config import Camera, ConfigError, LotConfig, SlotFile, load_slots
from parking.messages import CameraHealthMsg, Observation, SlotScore
from parking.vision.detector import Detection, Detector, FakeDetector
from parking.vision.health import FrameHealth
from parking.vision.pipeline import AnalysisResult, analyze_frame
from parking.vision.sources import FolderReplaySource, Frame, FrameSource, make_source
from parking.workers.base import Schedule, Worker, WorkerError, load_camera

log = logging.getLogger(__name__)

SUMMARY_EVERY_S = 60.0
STATS_WINDOW = 20  # frames used for fps and inference_ms_avg
JPEG_QUALITY = 85


class SidecarDetector:
    """`--fake-detector` for a stream of frames: the detections in `<image>.json` next to
    each frame's file (vision.md §1 format); no sidecar or no file = no detections."""

    def __init__(self, conf: float, classes: list[str]):
        self.conf = conf
        self.classes = classes
        self._cache: dict[Path, FakeDetector | None] = {}

    def for_frame(self, frame: Frame) -> Detector:
        if frame.path is None:
            return _NoDetections()
        sidecar = frame.path.with_suffix(".json")
        if sidecar not in self._cache:
            if sidecar.is_file():
                self._cache[sidecar] = FakeDetector(sidecar, self.conf, self.classes)
            else:
                log.warning("fake detector: no sidecar %s, no detections", sidecar.name)
                self._cache[sidecar] = None
        return self._cache[sidecar] or _NoDetections()


class _NoDetections:
    def detect(self, frame: np.ndarray) -> list[Detection]:
        return []


class _Setup:
    """Everything that `reload` replaces at once."""

    def __init__(
        self, worker: OccupancyWorker, lot: LotConfig, cam: Camera, old: _Setup | None = None
    ):
        root = worker.root
        try:
            self.slot_file: SlotFile = load_slots(_resolve(cam.slots_file, root))
        except (ConfigError, ValueError, OSError) as e:
            raise WorkerError(f"slot file for '{cam.id}': {e}") from None
        if self.slot_file.camera_id != cam.id:
            raise WorkerError(f"slot file is for '{self.slot_file.camera_id}', not '{cam.id}'")
        self.cam = cam
        self.capacities = {z: lot.zone_capacity(z, [self.slot_file]) for z in cam.zones}
        self.reference = _load_reference(cam, root)

        # keep the expensive or stateful parts when their config didn't change
        if old is not None and old.cam.detector == cam.detector and old.method == self.method:
            self.detector = old.detector
        else:
            self.detector = _make_detector(cam, root, worker.fake_detector)
        if old is not None and old.cam.source == cam.source:
            self.source = old.source
        else:
            try:
                self.source: FrameSource = make_source(cam.source, root)
            except (ValueError, NotImplementedError) as e:
                raise WorkerError(f"camera '{cam.id}' source: {e}") from None
        self.interval = (
            self.source.interval
            if isinstance(self.source, FolderReplaySource)
            else float(cam.sample_every_s)
        )

    @property
    def method(self) -> str:
        return self.cam.occupancy.method


def _resolve(path: str | Path, root: Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else root / path


def _make_detector(cam: Camera, root: Path, fake: bool) -> Detector | SidecarDetector | None:
    if cam.occupancy.method == "appearance":
        return None  # vision.md §2.1: no model
    det = cam.detector
    if fake:
        return SidecarDetector(det.conf, det.classes)
    model = _resolve(det.model, root)
    if not model.exists():
        raise WorkerError(f"model {model} not found; run `parking models export` first")
    from parking.vision.detector import YoloDetector

    return YoloDetector(str(model), det.imgsz, det.conf, det.classes, det.use_masks)


def _load_reference(cam: Camera, root: Path) -> np.ndarray | None:
    occ = cam.occupancy
    if occ.method != "appearance" or occ.appearance.reference_empty is None:
        return None
    path = _resolve(occ.appearance.reference_empty, root)
    ref = cv2.imread(str(path))
    if ref is None:
        raise WorkerError(f"can't read reference_empty image {path}")
    return ref


class OccupancyWorker(Worker):
    role = "occupancy"

    def __init__(self, config_path: Path, camera_id: str, *, fake_detector: bool = False, **kw):
        self.fake_detector = fake_detector
        super().__init__(config_path, camera_id, **kw)
        self._lock = threading.Lock()
        try:
            self._setup = _Setup(self, self.lot, self.camera)
        except WorkerError:
            self.api.close()
            raise
        self.health = FrameHealth(self.camera.health, check_frozen=not self._setup.source.replay)
        self.frames = 0  # frames read (including unhealthy and failed ones)
        self.observations = 0
        self.errors = 0
        self._latest: tuple[Frame, AnalysisResult | None] | None = None
        self._last_frame_at: float | None = None  # monotonic
        self._analysed_at: deque[float] = deque(maxlen=STATS_WINDOW)
        self._inference_ms: deque[float] = deque(maxlen=STATS_WINDOW)
        self._summary = _Summary()

    @property
    def interval(self) -> float:
        return self._setup.interval

    # --- the loop ---

    def loop(self, max_frames: int | None = None) -> None:
        log.info(
            "%s: occupancy worker, %s, %d slots, every %g s",
            self.camera_id,
            self._setup.method,
            len(self._setup.slot_file.slots),
            self.interval,
        )
        schedule = Schedule(self.interval, self.stop_event.wait)
        next_summary = time.monotonic() + SUMMARY_EVERY_S
        while not self.stop_event.is_set():
            if max_frames is not None and self.frames >= max_frames:
                break
            schedule.interval = self.interval  # a reload may change it
            if not schedule.wait():
                break
            self.step()
            if getattr(self._setup.source, "exhausted", False):
                log.info("%s: source has no more frames", self.camera_id)
                break
            if time.monotonic() >= next_summary:
                self._log_summary(schedule.skipped)
                next_summary += SUMMARY_EVERY_S

    def step(self) -> Observation | None:
        """One frame: read, check, analyse, send. Returns the observation (None if skipped)."""
        with self._lock:
            setup = self._setup
        self.frames += 1
        try:
            frame = setup.source.read()
        except Exception:
            log.exception("%s: reading a frame failed", self.camera_id)
            frame = None
        if frame is not None:
            self._last_frame_at = time.monotonic()
            self._latest = (frame, None)
        issue = self.health.check(frame)
        if issue:
            self._summary.unhealthy += 1
            log.debug("%s: frame skipped: %s", self.camera_id, issue)
            return None
        assert frame is not None
        detector = setup.detector
        if isinstance(detector, SidecarDetector):
            detector = detector.for_frame(frame)
        try:
            result = analyze_frame(
                frame.image, setup.cam, setup.slot_file, detector, setup.capacities, setup.reference
            )
        except Exception:
            self.errors += 1
            self._summary.errors += 1
            log.exception("%s: analysis failed", self.camera_id)
            return None
        self._latest = (frame, result)
        self._analysed_at.append(time.monotonic())
        self._inference_ms.append(result.inference_ms)

        obs = to_observation(result, frame.ts)
        sent = self.api.send_observation(obs)
        self.observations += 1
        self._summary.add(result, sent)
        log.debug(
            "%s: %s in %.0f ms%s",
            self.camera_id,
            _totals_text(result),
            result.timings["total_ms"],
            "" if sent else " (not sent)",
        )
        return obs

    def _log_summary(self, skipped_total: int) -> None:
        s, self._summary = self._summary, _Summary()
        last = self._latest[1] if self._latest else None
        log.info(
            "%s: last %.0f s: %d observations (%d not sent), %d unhealthy, %d errors, "
            "%d late runs skipped in total, avg %.0f ms; %s",
            self.camera_id,
            SUMMARY_EVERY_S,
            s.observations,
            s.unsent,
            s.unhealthy,
            s.errors,
            skipped_total,
            s.avg_ms,
            _totals_text(last) if last else "no result yet",
        )

    # --- health ---

    def health_message(self, final: bool = False) -> CameraHealthMsg:
        now = time.monotonic()
        stale = self.lot.api.stale_after_s
        if final or self.health.is_down(stale):
            state = "down"
        elif self.health.degraded:
            state = "degraded"
        else:
            state = "ok"
        times = list(self._analysed_at)
        span = times[-1] - times[0] if len(times) > 1 else 0.0
        fps = (len(times) - 1) / span if span > 0 else None
        ms = list(self._inference_ms)
        return CameraHealthMsg(
            camera_id=self.camera_id,
            ts=datetime.now(UTC),
            state=state,
            issue=self.health.last_issue,
            fps=round(fps, 3) if fps is not None else None,
            last_frame_age_s=(
                round(now - self._last_frame_at, 1) if self._last_frame_at is not None else None
            ),
            inference_ms_avg=round(sum(ms) / len(ms), 1) if ms else None,
            unhealthy_ratio=round(self.health.unhealthy_ratio, 3),
        )

    # --- control (api.md §5.2) ---

    def snapshot(self, annotated: bool) -> bytes | None:
        latest = self._latest
        if latest is None:
            return None
        frame, result = latest
        image = frame.image
        if annotated:
            from parking.vision.annotate import annotate_occupancy

            with self._lock:
                setup = self._setup
            names = {z.id: z.name.get("en", z.id) for z in self.lot.zones}
            image = annotate_occupancy(
                image,
                setup.slot_file.slots,
                result.slots if result else [],
                result.detections if result else [],
                {names.get(z, z): t for z, t in result.banner_totals().items()} if result else {},
                result.inference_ms if result else None,
                setup.slot_file.image_size,
            )
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return buf.tobytes() if ok else None

    def reload(self) -> dict[str, Any]:
        """Re-read lot.yaml (this camera's section) and the slot file; keep the old ones if
        anything is wrong."""
        try:
            lot, cam = load_camera(self.config_path, self.camera_id, self.role)
            with self._lock:
                old = self._setup
            setup = _Setup(self, lot, cam, old)
        except WorkerError as e:
            raise ValueError(str(e)) from None
        with self._lock:
            self.lot = lot
            if setup.source is not old.source:
                old.source.close()
            self._setup = setup
            self.camera = cam
            self.health.cfg = cam.health
            self.health.check_frozen = not setup.source.replay
        log.info("%s: reloaded, %d slots", self.camera_id, len(setup.slot_file.slots))
        return {
            "camera_id": self.camera_id,
            "slots": len(setup.slot_file.slots),
            "interval_s": setup.interval,
        }

    def save_reference(self) -> dict[str, Any]:
        """Save the current frame as `data/reference/<camera>.jpg` (vision.md §6)."""
        latest = self._latest
        if latest is None:
            raise ValueError("no frame read yet")
        path = self.root / "data" / "reference" / f"{self.camera_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.jpg")
        if not cv2.imwrite(str(tmp), latest[0].image):
            raise ValueError(f"could not write {path.name}")
        tmp.replace(path)
        log.info("%s: reference frame saved", self.camera_id)
        return {"saved": f"data/reference/{path.name}", "ts": latest[0].ts.isoformat()}

    def shutdown(self) -> None:
        super().shutdown()
        self._setup.source.close()


def to_observation(result: AnalysisResult, ts: datetime) -> Observation:
    """The api.md observation for one analysed frame (`ts` = when the frame was read)."""
    return Observation(
        camera_id=result.camera_id,
        ts=ts,
        frame_size=result.frame_size,
        inference_ms=round(result.inference_ms),
        detections=len(result.detections),
        slots=[
            SlotScore(id=s.id, score=round(min(max(s.score, 0.0), 1.0), 3), taken=s.taken)
            for s in result.slots
        ],
        zone_counts=dict(result.zone_counts),
    )


def _totals_text(result: AnalysisResult) -> str:
    return (
        "; ".join(f"{z} {t.free}/{t.capacity} free" for z, t in result.totals.items()) or "no zones"
    )


class _Summary:
    def __init__(self) -> None:
        self.observations = 0
        self.unsent = 0
        self.unhealthy = 0
        self.errors = 0
        self._ms = 0.0

    def add(self, result: AnalysisResult, sent: bool) -> None:
        self.observations += 1
        self.unsent += not sent
        self._ms += result.timings["total_ms"]

    @property
    def avg_ms(self) -> float:
        return self._ms / self.observations if self.observations else 0.0
