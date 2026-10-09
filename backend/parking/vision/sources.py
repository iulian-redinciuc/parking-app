"""Frame sources (config.md "Source URI formats").

`make_source(uri)` turns a camera's `source` string into a `FrameSource`:

- `file:<path>`: the same image every time.
- `folder:<dir>?interval=5&loop=true`: images in name order, one per `read()`. The folder is
  re-scanned at the start of every pass, so new files dropped in are picked up. `interval` is
  only parsed here; the **worker loop** does the waiting. With `loop=false`, `read()` returns
  `None` at the end and `exhausted` becomes true.
- `snapshot:<http url>`: one HTTP GET per `read()` (JPEG), digest or basic auth from the URL's
  `user:pass@`. Preferred for occupancy: no decoding between samples, full resolution.
- `rtsp:<rtsp url>`: a reader thread decodes the stream and keeps only the newest frame
  (older ones are overwritten, never queued); `read()` returns it, or `None` when it's older
  than 5 s. The fallback for occupancy, and the flow camera's live source.
- `video:<path>?realtime=true&loop=false`: a recording (P5.2). `realtime=true` plays at the
  file's native fps like a live camera (`read()` waits for the next frame and skips frames the
  caller was too slow for); `realtime=false` returns every frame as fast as it's read, for
  evaluation. Frame timestamps follow the video's timeline either way.

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
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, Protocol, runtime_checkable
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import cv2
import httpx
import numpy as np

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
SNAPSHOT_TIMEOUT_S = 5.0
RTSP_MAX_AGE_S = 5.0  # an RTSP frame older than this counts as no frame
RTSP_TIMEOUT_MS = 5000  # open and read timeouts of the FFmpeg capture
# FFmpeg ≥ 5 (OpenCV's wheels bundle 8.x) renamed RTSP's `stimeout` to `timeout` (µs);
# `nobuffer` + `low_delay` stop FFmpeg buffering frames before handing them out (latency)
RTSP_CAPTURE_OPTIONS = "rtsp_transport;tcp|timeout;5000000|fflags;nobuffer|flags;low_delay"
FPS_WINDOW_S = 5.0  # `fps` = frames per second over this many recent seconds
VIDEO_DEFAULT_FPS = 10.0  # when a file doesn't say (or says something absurd)


class Frame(NamedTuple):
    image: np.ndarray  # BGR, as read by OpenCV
    ts: datetime  # UTC, when the frame was read
    path: Path | None = None  # the image file, for replay sources (fake-detector sidecars)
    seq: int | None = None  # rtsp/video: frame number (equal = the same frame read again)


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


class RateMeter:
    """Events per second over the last `window` seconds (at least two events needed)."""

    def __init__(self, window: float = FPS_WINDOW_S, clock: Callable[[], float] = time.monotonic):
        self.window = window
        self.clock = clock
        self._times: deque[float] = deque()

    def tick(self) -> None:
        now = self.clock()
        self._times.append(now)
        while self._times and now - self._times[0] > self.window:
            self._times.popleft()

    @property
    def rate(self) -> float:
        t = self._times
        if len(t) < 2 or self.clock() - t[-1] > self.window:
            return 0.0
        return (len(t) - 1) / (t[-1] - t[0]) if t[-1] > t[0] else 0.0


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


_OPTIONS_VAR = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
_OPERATOR_OPTIONS = os.environ.get(_OPTIONS_VAR)  # set before start-up: the operator's own
_options_lock = threading.Lock()


def _capture_with_options(options: str | None, *args: Any) -> Any:
    """`cv2.VideoCapture(*args)` with the FFmpeg options env var set (None = unset) only for
    this open: OpenCV reads it at every open, and the RTSP options break opening files."""
    with _options_lock:
        old = os.environ.get(_OPTIONS_VAR)
        if options is None:
            os.environ.pop(_OPTIONS_VAR, None)
        else:
            os.environ[_OPTIONS_VAR] = options
        try:
            return cv2.VideoCapture(*args)
        finally:
            if old is None:
                os.environ.pop(_OPTIONS_VAR, None)
            else:
                os.environ[_OPTIONS_VAR] = old


def _open_capture(url: str) -> Any:
    params = [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
        RTSP_TIMEOUT_MS,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
        RTSP_TIMEOUT_MS,
    ]
    options = _OPERATOR_OPTIONS or RTSP_CAPTURE_OPTIONS  # an operator's own value wins
    return _capture_with_options(options, url, cv2.CAP_FFMPEG, params)


def _open_video(path: str) -> Any:
    return _capture_with_options(None, path, cv2.CAP_FFMPEG)


class RtspSource:
    """`rtsp:` — a reader thread keeps only the newest decoded frame (a lock + one slot).

    The thread reads as fast as the stream delivers, so frames never queue up behind a slow
    caller: a frame the caller didn't read before the next one arrived is overwritten and
    counted in `dropped`. `fps` is the decode rate over the last 5 s. The thread starts on the
    first `read()`. When opening or reading fails, the capture is released and reopened after
    1, 2, 4 … 60 s; each attempt is logged.
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
        self.frames = 0  # frames decoded by the reader thread (= the newest frame's seq)
        self.dropped = 0  # frames overwritten before any read() returned them
        self._meter = RateMeter(clock=clock)
        self._open = open_capture
        self._lock = threading.Lock()
        # image, ts, clock time, seq
        self._latest: tuple[np.ndarray, datetime, float, int] | None = None
        self._returned = 0  # seq of the newest frame read() has returned
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
            self._returned = latest[3]
        return Frame(latest[0].copy(), latest[1], seq=latest[3])

    @property
    def fps(self) -> float:
        """Frames decoded per second over the last 5 s (0 while nothing arrives)."""
        return self._meter.rate

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
                    if self._latest is not None and self._latest[3] > self._returned:
                        self.dropped += 1
                    self.frames += 1
                    self._latest = (img, _now(), self.clock(), self.frames)
                self._meter.tick()
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


class VideoFileSource:
    """`video:` — a recording, at its native fps (`realtime`) or as fast as possible.

    Realtime playback behaves like a live camera: the clock starts at the first `read()`,
    `read()` sleeps until the next frame is due, and when the caller falls behind, the frames
    it missed are skipped (`grab()` without converting them) and counted in `dropped`. A
    frame's `ts` is the first read's wall time + its position / native fps, so tracking and
    counting see the video's own timing even with `realtime=false`. At the end, `loop=true`
    starts over (timestamps keep increasing); otherwise `read()` returns `None` and
    `exhausted` becomes true. A missing or unreadable file is a failed read.
    """

    replay = True

    def __init__(
        self,
        path: Path,
        realtime: bool = True,
        loop: bool = False,
        open_capture: Callable[[str], Any] = _open_video,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.path = path
        self.realtime = realtime
        self.loop = loop
        self.clock = clock
        self.sleep = sleep
        self.exhausted = False
        self.native_fps: float | None = None  # known once the file is open
        self.frames = 0  # frames returned (= the latest frame's seq)
        self.dropped = 0  # frames skipped because the caller was late (realtime only)
        self.passes = 0
        self._open = open_capture
        self._cap: Any = None
        self._pos = 0  # index of the next frame on the timeline (across loops)
        self._start: tuple[float, datetime] | None = None  # clock and wall time of frame 0
        self._meter = RateMeter(clock=clock)

    @property
    def fps(self) -> float:
        """Frames returned per second over the last 5 s."""
        return self._meter.rate

    def _open_file(self) -> bool:
        if not self.path.is_file():
            log.warning("video file %s not found", self.path)
            return False
        cap = self._open(str(self.path))
        if cap is None or not cap.isOpened():
            log.warning("could not open video %s", self.path)
            if cap is not None:
                cap.release()
            return False
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if not 0 < fps <= 240:  # also NaN
            log.warning(
                "video %s: no usable fps (%r), assuming %g", self.path, fps, VIDEO_DEFAULT_FPS
            )
            fps = VIDEO_DEFAULT_FPS
        self.native_fps = fps
        self._cap = cap
        self.passes += 1
        return True

    def _next(self, convert: bool) -> np.ndarray | bool | None:
        """Read (or just grab) the next frame, looping if asked; None at the end."""
        for _ in range(2):
            if convert:
                ok, img = self._cap.read()
                if ok and img is not None:
                    return img
            elif self._cap.grab():
                return True
            if not self.loop:
                return None
            self._cap.release()  # start the next pass from the top
            self._cap = None
            if not self._open_file():
                return None
        return None  # an empty file, even after reopening

    def read(self) -> Frame | None:
        if self.exhausted:
            return None
        if self._cap is None and not self._open_file():
            return None
        fps = self.native_fps or VIDEO_DEFAULT_FPS
        if self._start is None:
            self._start = (self.clock(), _now())
        if self.realtime:
            due = self._start[0] + self._pos / fps
            wait = due - self.clock()
            if wait > 0:
                self.sleep(wait)
            else:  # late: skip what the caller missed, as a live camera would
                behind = int((self.clock() - self._start[0]) * fps) - self._pos
                for _ in range(max(0, behind)):
                    if self._next(convert=False) is None:
                        break
                    self._pos += 1
                    self.dropped += 1
        img = self._next(convert=True)
        if img is None:
            self.exhausted = True
            log.info("video %s: end after %d frame(s)", self.path.name, self.frames)
            return None
        ts = self._start[1] + timedelta(seconds=self._pos / fps)
        self._pos += 1
        self.frames += 1
        self._meter.tick()
        return Frame(img, ts, seq=self._pos)

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


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


def _options(uri: str, query: str, allowed: set[str]) -> dict[str, str]:
    params = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"source {uri!r}: unknown option(s) {', '.join(sorted(unknown))}")
    return params


def make_source(uri: str, root: Path | None = None) -> FrameSource:
    """Build the source for a `scheme:rest` URI. Relative paths resolve against `root`."""
    scheme, sep, rest = uri.partition(":")
    if not sep or not rest:
        raise ValueError(f"source {uri!r}: expected '<scheme>:<target>'")
    if scheme == "file":
        return FileSource(_resolve(rest, root))
    if scheme == "folder":
        path, _, query = rest.partition("?")
        params = _options(uri, query, {"interval", "loop"})
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
    if scheme == "video":
        path, _, query = rest.partition("?")
        params = _options(uri, query, {"realtime", "loop"})
        try:
            realtime = _parse_bool(params.get("realtime", "true"))
            loop = _parse_bool(params.get("loop", "false"))
        except ValueError as e:
            raise ValueError(f"source {uri!r}: {e}") from None
        if "://" in path:  # a URL would be opened by FFmpeg as a stream: use rtsp: for those
            raise ValueError("video source: expected a file path (use 'rtsp:' for streams)")
        return VideoFileSource(_resolve(path, root), realtime=realtime, loop=loop)
    # don't echo the URI: it may hold camera credentials
    raise ValueError(f"unknown source scheme {scheme!r} (file, folder, snapshot, rtsp, video)")
