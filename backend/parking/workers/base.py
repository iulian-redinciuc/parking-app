"""What every camera worker shares: config, API client, heartbeat, control server, shutdown.

`Worker.run()`:
1. starts the control server (workers/control.py) if a port and `WORKER_TOKEN` are set,
2. starts a timer thread that sends a health message every `HEALTH_EVERY_S`,
3. runs `loop()` until it returns or SIGTERM/SIGINT sets the stop flag (the current frame
   is finished first),
4. sends a final health `down`, flushes the flow-event outbox for up to `SHUTDOWN_FLUSH_S`
   and closes everything.

`Schedule` is the loop's clock: runs are `interval` apart on a monotonic grid (next run =
previous scheduled start + interval), so slow frames don't make it drift; when it's late by
more than one interval the missed runs are skipped instead of run back to back.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from parking.config import Camera, ConfigError, LotConfig, Settings, cli_env, load_config
from parking.messages import CameraHealthMsg
from parking.workers.api_client import ApiClient
from parking.workers.control import DEFAULT_PORT, ControlServer

log = logging.getLogger(__name__)

HEALTH_EVERY_S = 10.0
SHUTDOWN_FLUSH_S = 5.0


class WorkerError(Exception):
    """A configuration problem that stops the worker from starting (or a reload)."""


def load_camera(config_path: Path, camera_id: str, role: str) -> tuple[LotConfig, Camera]:
    """Load lot.yaml (`${VAR}` from deploy/.env.example < deploy/.env < environment)."""
    root = config_path.resolve().parent.parent
    try:
        lot = load_config(config_path, cli_env(root))
    except (ConfigError, ValueError, OSError) as e:
        raise WorkerError(f"{config_path}: {e}") from None
    cams = {c.id: c for c in lot.cameras}
    if camera_id not in cams:
        raise WorkerError(
            f"camera '{camera_id}' is not in {config_path} (cameras: {', '.join(cams) or 'none'})"
        )
    cam = cams[camera_id]
    if cam.role != role:
        raise WorkerError(f"camera '{camera_id}' is a {cam.role} camera, not {role}")
    return lot, cam


def load_settings(root: Path) -> Settings:
    """`.env` variables: `deploy/.env` under the repo root, overridden by the environment."""
    return Settings(_env_file=root / "deploy" / ".env")


class Schedule:
    """Monotonic, drift-free run times. `wait()` returns False if `sleep` reports a stop."""

    def __init__(
        self,
        interval: float,
        sleep: Callable[[float], bool],
        clock: Callable[[], float] = time.monotonic,
    ):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.interval = interval
        self.skipped = 0  # runs dropped because the loop was too late
        self._sleep = sleep
        self._clock = clock
        self._next: float | None = None

    def wait(self) -> bool:
        """Wait for the next run; the first run is immediate."""
        now = self._clock()
        if self._next is None:
            self._next = now
        while now - self._next > self.interval:
            self._next += self.interval
            self.skipped += 1
        delay = self._next - now
        if delay > 0 and self._sleep(delay):
            return False
        self._next += self.interval
        return True


class Worker:
    """Base class. Subclasses implement `loop()`, `health_message()` and the control calls."""

    role = ""

    def __init__(
        self,
        config_path: Path,
        camera_id: str,
        *,
        print_mode: bool = False,
        settings: Settings | None = None,
        api: ApiClient | None = None,
        control_port: int | None = DEFAULT_PORT,
        control_host: str = "0.0.0.0",
    ):
        self.config_path = config_path
        self.root = config_path.resolve().parent.parent
        self.lot, self.camera = load_camera(config_path, camera_id, self.role)
        self.settings = settings or load_settings(self.root)
        if api is None:
            try:
                api = ApiClient.from_settings(
                    self.settings,
                    camera_id,
                    print_mode=print_mode,
                    outbox_dir=self.root / "data" / "outbox",
                )
            except ValueError as e:
                raise WorkerError(str(e)) from None
        self.api = api
        self.control_port = control_port
        self.control_host = control_host
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self._control: ControlServer | None = None
        self._heartbeat: threading.Thread | None = None

    @property
    def camera_id(self) -> str:
        return self.camera.id

    # --- to implement ---

    def loop(self, max_frames: int | None = None) -> None:
        raise NotImplementedError

    def health_message(self, final: bool = False) -> CameraHealthMsg:
        raise NotImplementedError

    def snapshot(self, annotated: bool) -> bytes | None:
        return None

    def reload(self) -> dict[str, Any]:
        raise ValueError("reload is not supported by this worker")

    def save_reference(self) -> dict[str, Any]:
        raise ValueError("save-reference is not supported by this worker")

    # --- lifecycle ---

    def stop(self, *_: object) -> None:
        """Ask the loop to stop after the current frame (also the signal handler)."""
        already = self.stop_event.is_set()
        self.stop_event.set()
        if not already:
            log.info("%s: stopping", self.camera_id)

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

    def run(self, max_frames: int | None = None, handle_signals: bool = True) -> None:
        if handle_signals:
            self.install_signal_handlers()
        self._start_control()
        self._heartbeat = threading.Thread(target=self._beat, name="heartbeat", daemon=True)
        self._heartbeat.start()
        try:
            self.loop(max_frames)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.stop_event.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=HEALTH_EVERY_S)
        self._send_health(final=True)
        if self.api.pending and not self.api.flush(SHUTDOWN_FLUSH_S):
            log.warning("%s: %d flow event(s) left in the outbox", self.camera_id, self.api.pending)
        self.api.close()
        if self._control is not None:
            self._control.stop()
            self._control = None
        log.info("%s: stopped", self.camera_id)

    def _start_control(self) -> None:
        if self.control_port is None:
            return
        token = self.settings.worker_token
        if token is None:
            log.warning("WORKER_TOKEN is not set: control server (/control/*) not started")
            return
        try:
            self._control = ControlServer(
                self, token.get_secret_value(), self.control_host, self.control_port
            ).start()
        except OSError as e:
            raise WorkerError(f"control server port {self.control_port}: {e}") from None

    @property
    def control(self) -> ControlServer | None:
        return self._control

    def _beat(self) -> None:
        while not self.stop_event.is_set():
            self._send_health()
            if self.stop_event.wait(HEALTH_EVERY_S):
                return

    def _send_health(self, final: bool = False) -> None:
        try:
            self.api.send_health(self.health_message(final))
        except Exception:
            log.exception("%s: health message failed", self.camera_id)
