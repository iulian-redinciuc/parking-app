"""Frame sources (config.md "Source URI formats").

`make_source(uri)` turns a camera's `source` string into a `FrameSource`:

- `file:<path>`: the same image every time.
- `folder:<dir>?interval=5&loop=true`: images in name order, one per `read()`. The folder is
  re-scanned at the start of every pass, so new files dropped in are picked up. `interval` is
  only parsed here; the **worker loop** does the waiting. With `loop=false`, `read()` returns
  `None` at the end and `exhausted` becomes true.
- `snapshot:<http url>`: one HTTP GET per `read()` (JPEG), digest or basic auth from the URL's
  `user:pass@`. Preferred for occupancy: no decoding between samples, full resolution.
- `rtsp:<rtsp url>`: a reader thread decodes the stream and keeps only the latest frame;
  `read()` returns it, or `None` when it's older than 5 s. The fallback for occupancy.
- `video:` comes with the flow camera (P5.2).

`read()` returns `None` when no frame could be read (missing or unreadable file, camera
unreachable); the caller reports that as `connect_failed` (vision.md §5). The camera sources
reconnect with exponential backoff (1, 2, 4 … 60 s) and never log the URL (it holds the
camera's credentials).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, Protocol, runtime_checkable
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import cv2
import httpx
import numpy as np

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
LATER = {"video": "Phase 5 (P5.2)"}
SNAPSHOT_TIMEOUT_S = 5.0
RTSP_MAX_AGE_S = 5.0  # an RTSP frame older than this counts as no frame
RTSP_TIMEOUT_MS = 5000  # open and read timeouts of the FFmpeg capture
# FFmpeg ≥ 5 (OpenCV's wheels bundle 8.x) renamed RTSP's `stimeout` to `timeout` (µs)
RTSP_CAPTURE_OPTIONS = "rtsp_transport;tcp|timeout;5000000"


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


class Backoff:
    """Exponential reconnect delays: 1, 2, 4 … `cap` s, back to the start after a success."""

    def __init__(
        self, first: float = 1.0, cap: float = 60.0, clock: Callable[[], float] = time.monotonic
    ):
        self.first = first
        self.cap = cap
        self.clock = clock
        self.failures = 0
        self.next_at = 0.0  # clock time of the next allowed attempt

    @property
    def delay(self) -> float:
        """The wait after the latest failure (0 when the last attempt worked)."""
        if self.failures == 0:
            return 0.0
        return min(self.cap, self.first * 2 ** (self.failures - 1))

    def ready(self) -> bool:
        return self.clock() >= self.next_at

    def failed(self) -> float:
        self.failures += 1
        self.next_at = self.clock() + self.delay
        return self.delay

    def succeeded(self) -> None:
        self.failures = 0
        self.next_at = 0.0


def split_credentials(url: str) -> tuple[str, tuple[str, str] | None]:
    """`scheme://user:pass@host/…` → (`scheme://host/…`, (user, pass)); percent-decoded."""
    parts = urlsplit(url)
    if parts.username is None:
        return url, None
    host = parts.hostname or ""
    if ":" in host:  # IPv6
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    bare = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return bare, (unquote(parts.username), unquote(parts.password or ""))


class CameraAuth(httpx.Auth):
    """Digest or basic, whichever the camera's 401 asks for; remembered for later requests
    (basic is then sent up front, digest is re-negotiated by `httpx.DigestAuth`)."""

    def __init__(self, user: str, password: str):
        self._basic = httpx.BasicAuth(user, password)
        self._digest = httpx.DigestAuth(user, password)
        self.scheme: str | None = None  # "basic" | "digest" once known

    def auth_flow(self, request: httpx.Request):
        if self.scheme == "digest":
            yield from self._digest.auth_flow(request)
            return
        if self.scheme == "basic":
            yield from self._basic.auth_flow(request)
            return
        response = yield request
        if response.status_code != 401:
            return
        challenge = response.headers.get("www-authenticate", "").lower()
        if challenge.startswith("digest"):
            self.scheme = "digest"
            flow = self._digest.auth_flow(request)
            next(flow)  # DigestAuth's first, unauthenticated request: we already have its 401
            try:
                response = yield flow.send(response)
                flow.send(response)
            except StopIteration:
                pass
        elif challenge.startswith("basic"):
            self.scheme = "basic"
            yield from self._basic.auth_flow(request)


class SnapshotSource:
    """`snapshot:` — one HTTP GET per read, decoded with `cv2.imdecode`."""

    replay = False

    def __init__(
        self, url: str, timeout: float = SNAPSHOT_TIMEOUT_S, backoff: Backoff | None = None
    ):
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("snapshot source: expected an http:// or https:// URL")
        # httpx logs each request's URL at INFO: one line per sample, and a snapshot URL may
        # hold credentials in its query (Reolink: `?cmd=Snap&user=…&password=…`)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.url, creds = split_credentials(url)
        self.auth = CameraAuth(*creds) if creds else None
        self.backoff = backoff or Backoff()
        self.last_error: str | None = None
        self._client = httpx.Client(timeout=timeout, auth=self.auth, follow_redirects=True)

    def read(self) -> Frame | None:
        if not self.backoff.ready():
            return None  # still waiting before the next attempt
        img, error = self._fetch()
        if img is None:
            self.last_error = error
            delay = self.backoff.failed()
            log.warning(
                "snapshot failed (%s), attempt %d, next try in %g s",
                error,
                self.backoff.failures,
                delay,
            )
            return None
        if self.backoff.failures:
            log.info("snapshot: camera back after %d failed attempt(s)", self.backoff.failures)
        self.backoff.succeeded()
        self.last_error = None
        return Frame(img, _now())

    def _fetch(self) -> tuple[np.ndarray | None, str]:
        try:
            resp = self._client.get(self.url)
        except httpx.HTTPError as e:  # its text holds the URL: log the type only
            return None, type(e).__name__
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        data = np.frombuffer(resp.content, np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        if img is None:
            return None, f"not an image ({resp.headers.get('content-type', '?')})"
        return img, ""

    def close(self) -> None:
        self._client.close()


def _open_capture(url: str) -> Any:
    # OpenCV reads the options at every open; an operator's own value wins
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", RTSP_CAPTURE_OPTIONS)
    params = [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
        RTSP_TIMEOUT_MS,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
        RTSP_TIMEOUT_MS,
    ]
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)


class RtspSource:
    """`rtsp:` — a reader thread keeps only the latest decoded frame (a lock + one slot).

    The thread starts on the first `read()`. When opening or reading fails, the capture is
    released and reopened after 1, 2, 4 … 60 s; each attempt is logged.
    """

    replay = False

    def __init__(
        self,
        url: str,
        max_age: float = RTSP_MAX_AGE_S,
        open_capture: Callable[[str], Any] = _open_capture,
        backoff: Backoff | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if urlsplit(url).scheme not in ("rtsp", "rtsps"):
            raise ValueError("rtsp source: expected an rtsp:// URL")
        self.url = url
        self.max_age = max_age
        self.clock = clock
        self.backoff = backoff or Backoff(clock=clock)
        self.connected = False
        self.reconnects = 0  # captures reopened after a failure
        self.frames = 0  # frames decoded by the reader thread
        self._open = open_capture
        self._lock = threading.Lock()
        self._latest: tuple[np.ndarray, datetime, float] | None = None  # image, ts, clock time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def read(self) -> Frame | None:
        alive = self._thread is not None and self._thread.is_alive()
        if not alive and not self._stop.is_set():  # first read, or the thread crashed
            self._thread = threading.Thread(target=self._run, name="rtsp-reader", daemon=True)
            self._thread.start()
        with self._lock:
            latest = self._latest
        if latest is None or self.clock() - latest[2] > self.max_age:
            return None
        return Frame(latest[0].copy(), latest[1])

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=RTSP_TIMEOUT_MS / 1000 + 2)
            self._thread = None
        with self._lock:
            self._latest = None

    # --- reader thread ---

    def _run(self) -> None:
        cap = None
        try:
            while not self._stop.is_set():
                if cap is None:
                    cap = self._connect()
                    if cap is None:
                        continue
                ok, img = cap.read()
                if not ok or img is None:
                    log.warning("rtsp: stream read failed, reconnecting")
                    cap.release()
                    cap = None
                    self.connected = False
                    self._fail()
                    continue
                with self._lock:
                    self._latest = (img, _now(), self.clock())
                self.frames += 1
                if self.backoff.failures:
                    log.info("rtsp: camera back after %d failed attempt(s)", self.backoff.failures)
                    self.backoff.succeeded()
        except Exception:
            log.exception("rtsp: reader thread crashed")
        finally:
            if cap is not None:
                cap.release()
            self.connected = False

    def _connect(self) -> Any:
        """One attempt to open the stream (after the backoff wait); None on failure."""
        wait = self.backoff.next_at - self.clock()
        if wait > 0 and self._stop.wait(wait):
            return None
        if self.backoff.failures or self.frames:
            self.reconnects += 1
        log.info("rtsp: connecting (attempt %d)", self.backoff.failures + 1)
        try:
            cap = self._open(self.url)
        except Exception as e:  # don't log the URL: it holds the credentials
            log.warning("rtsp: open raised %s", type(e).__name__)
            cap = None
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            self._fail()
            return None
        self.connected = True
        return cap

    def _fail(self) -> None:
        delay = self.backoff.failed()
        log.warning("rtsp: attempt %d failed, next try in %g s", self.backoff.failures, delay)


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
    if scheme == "snapshot":
        return SnapshotSource(rest)
    if scheme == "rtsp":
        return RtspSource(rest)
    if scheme in LATER:
        raise NotImplementedError(
            f"'{scheme}:' sources arrive in {LATER[scheme]}; use 'file:' or 'folder:' for now"
        )
    # don't echo the URI: it may hold camera credentials
    raise ValueError(f"unknown source scheme {scheme!r} (file, folder, snapshot, rtsp, video)")
