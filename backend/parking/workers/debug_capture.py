"""Debug frame capture (P4.7, vision.md §6.1, security-privacy.md §4).

With `DEBUG_CAPTURE=true` a worker saves an analysed frame (JPEG) + its observation JSON
into `data/debug/<camera>/<lot-local date>/` every `DEBUG_CAPTURE_EVERY_MIN` minutes and
whenever a slot's per-frame `taken` flips. A pruning thread deletes files older than
`DEBUG_RETENTION_HOURS`; it runs even with capture off, so turning capture off still clears
what an earlier run left. Off by default: frames can show people and number plates.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from parking.config import Settings
from parking.messages import Observation

log = logging.getLogger(__name__)

JPEG_QUALITY = 90
PRUNE_EVERY_S = 300.0


def debug_dir(root: Path, camera_id: str) -> Path:
    return root / "data" / "debug" / camera_id


class DebugCapture:
    """`maybe_save()` after every observation; `start()` the pruning thread."""

    def __init__(
        self,
        root: Path,
        camera_id: str,
        tz: str,
        *,
        enabled: bool,
        every_s: float,
        retention_s: float,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.dir = debug_dir(root, camera_id)
        self.camera_id = camera_id
        self.tz = ZoneInfo(tz)
        self.enabled = enabled
        self.every_s = every_s
        self.retention_s = retention_s
        self.saved = 0
        self._clock = clock
        self._wall = wall
        self._last_save: float | None = None  # monotonic
        self._taken: dict[str, bool] | None = None  # previous observation's slots
        self._thread: threading.Thread | None = None

    # --- capture ---

    def maybe_save(self, image: np.ndarray, obs: Observation) -> Path | None:
        """Save if it's time or a slot flipped since the previous observation; the JPEG path."""
        taken = {s.id: s.taken for s in obs.slots}
        prev, self._taken = self._taken, taken
        if not self.enabled:
            return None
        flipped = sorted(i for i, t in taken.items() if prev is not None and prev.get(i, t) != t)
        now = self._clock()
        periodic = self._last_save is None or now - self._last_save >= self.every_s
        if not (periodic or flipped):
            return None
        reasons = (["periodic"] if periodic else []) + (["flip"] if flipped else [])
        try:
            path = self._write(image, obs, reasons, flipped)
        except OSError as e:  # a full disk must not stop the worker
            log.warning("%s: debug capture failed: %s", self.camera_id, e)
            return None
        if periodic:
            self._last_save = now
        self.saved += 1
        log.debug("%s: debug capture %s (%s)", self.camera_id, path.name, "+".join(reasons))
        return path

    def _write(
        self, image: np.ndarray, obs: Observation, reasons: list[str], flipped: list[str]
    ) -> Path:
        local = obs.ts.astimezone(self.tz)
        day = self.dir / local.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        stem = f"{local:%H%M%S}_{local.microsecond // 1000:03d}-{'+'.join(reasons)}"
        jpg = day / f"{stem}.jpg"
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            raise OSError("JPEG encoding failed")
        jpg.write_bytes(buf.tobytes())
        meta = {
            "camera_id": self.camera_id,
            "frame": jpg.name,
            "reasons": reasons,
            "flipped": flipped,
            "observation": obs.model_dump(mode="json"),
        }
        jpg.with_suffix(".json").write_text(json.dumps(meta, indent=1) + "\n")
        return jpg

    # --- pruning ---

    def prune(self) -> int:
        """Delete files older than the retention period (by mtime) and empty date folders."""
        if not self.dir.is_dir():
            return 0
        cutoff = self._wall() - self.retention_s
        removed = 0
        for path in self.dir.rglob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as e:
                log.warning("%s: can't prune %s: %s", self.camera_id, path.name, e)
        for day in self.dir.iterdir():
            if day.is_dir() and not any(day.iterdir()):
                day.rmdir()
        if removed:
            log.info("%s: pruned %d debug capture file(s)", self.camera_id, removed)
        return removed

    def start(self, stop: threading.Event) -> None:
        """Prune now and then every `PRUNE_EVERY_S` (at most ¼ of the retention) until `stop`."""
        every = min(PRUNE_EVERY_S, max(self.retention_s / 4, 1.0))

        def run() -> None:
            while True:
                try:
                    self.prune()
                except Exception:
                    log.exception("%s: debug capture pruning failed", self.camera_id)
                if stop.wait(every):
                    return

        self._thread = threading.Thread(target=run, name="debug-prune", daemon=True)
        self._thread.start()

    def describe(self) -> str:
        if not self.enabled:
            return "debug capture off"
        return (
            f"debug capture on: every {self.every_s / 60:g} min + slot flips → "
            f"data/debug/{self.camera_id}/, kept {self.retention_s / 3600:g} h"
        )


def capture_from_settings(settings: Settings, root: Path, camera_id: str, tz: str) -> DebugCapture:
    return DebugCapture(
        root,
        camera_id,
        tz,
        enabled=settings.debug_capture,
        every_s=settings.debug_capture_every_min * 60,
        retention_s=settings.debug_retention_hours * 3600,
    )
