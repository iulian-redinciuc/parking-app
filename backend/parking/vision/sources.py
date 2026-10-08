"""Frame sources (config.md "Source URI formats").

`make_source(uri)` turns a camera's `source` string into a `FrameSource`:

- `file:<path>`: the same image every time.
- `folder:<dir>?interval=5&loop=true`: images in name order, one per `read()`. The folder is
  re-scanned at the start of every pass, so new files dropped in are picked up. `interval` is
  only parsed here; the **worker loop** does the waiting. With `loop=false`, `read()` returns
  `None` at the end and `exhausted` becomes true.
- `snapshot:`, `rtsp:`, `video:` come with the real cameras (Phases 4–5).

`read()` returns `None` when no frame could be read (missing or unreadable file); the caller
reports that as `connect_failed` (vision.md §5).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable
from urllib.parse import parse_qs

import cv2
import numpy as np

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
LATER = {"snapshot": "Phase 4 (P4.3)", "rtsp": "Phase 4 (P4.3)", "video": "Phase 5 (P5.2)"}


class Frame(NamedTuple):
    image: np.ndarray  # BGR, as read by OpenCV
    ts: datetime  # UTC, when the frame was read
    path: Path | None = None  # the image file, for replay sources (fake-detector sidecars)


@runtime_checkable
class FrameSource(Protocol):
    # true for sources that replay stored images (file/folder): repeated frames are expected,
    # so the worker turns the `frozen` health check off for them
    replay: bool

    def read(self) -> Frame | None: ...

    def close(self) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _imread(path: Path) -> np.ndarray | None:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        log.warning("could not read image %s", path)
    return img


class FileSource:
    """`file:` — the same image on every read (decoded once)."""

    replay = True

    def __init__(self, path: Path):
        self.path = path
        self._image: np.ndarray | None = None

    def read(self) -> Frame | None:
        if self._image is None:
            self._image = _imread(self.path)
            if self._image is None:
                return None
        return Frame(self._image.copy(), _now(), self.path)

    def close(self) -> None:
        self._image = None


class FolderReplaySource:
    """`folder:` — images in name order, re-scanned on every pass."""

    replay = True

    def __init__(self, folder: Path, interval: float = 5.0, loop: bool = True):
        self.folder = folder
        self.interval = interval
        self.loop = loop
        self.exhausted = False
        self._files: list[Path] = []
        self._pos = 0
        self._passes = 0

    def scan(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(
            p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )

    def read(self) -> Frame | None:
        if self.exhausted:
            return None
        if self._pos >= len(self._files):
            if self._passes > 0 and not self.loop:
                self.exhausted = True
                return None
            self._files = self.scan()
            self._pos = 0
            if not self._files:
                log.warning("no images in %s", self.folder)
                return None
            self._passes += 1
        path = self._files[self._pos]
        self._pos += 1
        img = _imread(path)
        return None if img is None else Frame(img, _now(), path)

    def close(self) -> None:
        self._files = []
        self._pos = 0


def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _resolve(path: str, root: Path | None) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() or root is None else root / p


def make_source(uri: str, root: Path | None = None) -> FrameSource:
    """Build the source for a `scheme:rest` URI. Relative paths resolve against `root`."""
    scheme, sep, rest = uri.partition(":")
    if not sep or not rest:
        raise ValueError(f"source {uri!r}: expected '<scheme>:<target>'")
    if scheme == "file":
        return FileSource(_resolve(rest, root))
    if scheme == "folder":
        path, _, query = rest.partition("?")
        params = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
        unknown = set(params) - {"interval", "loop"}
        if unknown:
            raise ValueError(f"source {uri!r}: unknown option(s) {', '.join(sorted(unknown))}")
        try:
            interval = float(params.get("interval", 5))
            loop = _parse_bool(params.get("loop", "true"))
        except ValueError as e:
            raise ValueError(f"source {uri!r}: {e}") from None
        if interval <= 0:
            raise ValueError(f"source {uri!r}: interval must be > 0")
        return FolderReplaySource(_resolve(path, root), interval=interval, loop=loop)
    if scheme in LATER:
        raise NotImplementedError(
            f"'{scheme}:' sources arrive in {LATER[scheme]}; use 'file:' or 'folder:' for now"
        )
    # don't echo the URI: it may hold camera credentials
    raise ValueError(f"unknown source scheme {scheme!r} (file, folder, snapshot, rtsp, video)")
