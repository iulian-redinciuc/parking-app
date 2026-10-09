"""Camera sources (P4.3): `snapshot:` against a local HTTP server, `rtsp:` with a mocked capture."""

import hashlib
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from parking.vision import sources
from parking.vision.sources import (
    Backoff,
    FrameSource,
    RtspSource,
    SnapshotSource,
    make_source,
    split_credentials,
)

USER, PASSWORD = "admin", "p@ss:w/rd"
PASSWORD_Q = "p%40ss%3Aw%2Frd"  # percent-encoded in the URL
REALM, NONCE = "cam", "f00dcafe"


def _jpeg(value: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((36, 64, 3), value, np.uint8))
    assert ok
    return buf.tobytes()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class Camera:
    """A tiny camera: serves `body` at /snap.jpg with auth `none` | `basic` | `digest`."""

    def __init__(self, auth: str = "none"):
        self.auth = auth
        self.body = _jpeg(120)
        self.status = 200
        self.requests: list[str | None] = []  # the Authorization header of each request
        cam = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                header = self.headers.get("Authorization")
                cam.requests.append(header)
                if not cam.authorized(header, self.path):
                    self.send_response(401)
                    challenge = (
                        f'Basic realm="{REALM}"'
                        if cam.auth == "basic"
                        else f'Digest realm="{REALM}", nonce="{NONCE}", qop="auth", algorithm=MD5'
                    )
                    self.send_header("WWW-Authenticate", challenge)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(cam.status)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(cam.body)))
                self.end_headers()
                self.wfile.write(cam.body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, creds: str = f"{USER}:{PASSWORD_Q}@") -> str:
        return f"http://{creds}127.0.0.1:{self.port}/snap.jpg"

    def authorized(self, header: str | None, path: str) -> bool:
        if self.auth == "none":
            return True
        if header is None:
            return False
        if self.auth == "basic":
            import base64

            return header == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if not header.startswith("Digest "):
            return False
        f = dict(re.findall(r'(\w+)="?([^",]+)"?', header))
        if not {"username", "nc", "cnonce", "response"} <= f.keys():
            return False
        ha1 = _md5(f"{USER}:{REALM}:{PASSWORD}")
        ha2 = _md5(f"GET:{path}")
        expected = _md5(f"{ha1}:{NONCE}:{f['nc']}:{f['cnonce']}:auth:{ha2}")
        return f.get("username") == USER and f.get("response") == expected

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def camera(request):
    cam = Camera(getattr(request, "param", "none"))
    yield cam
    cam.stop()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --- helpers ---


def test_split_credentials_decodes_and_strips():
    bare, creds = split_credentials(f"http://{USER}:{PASSWORD_Q}@10.0.20.11:8080/cgi?x=1")
    assert bare == "http://10.0.20.11:8080/cgi?x=1" and creds == (USER, PASSWORD)
    assert split_credentials("http://10.0.20.11/s.jpg") == ("http://10.0.20.11/s.jpg", None)
    assert split_credentials("http://u@[fe80::1]/s")[0] == "http://[fe80::1]/s"


def test_backoff_doubles_to_60_and_resets():
    clock = FakeClock()
    b = Backoff(clock=clock)
    assert b.ready() and b.delay == 0
    assert [b.failed() for _ in range(9)] == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert not b.ready()
    clock.t += 60
    assert b.ready()
    b.succeeded()
    assert b.failures == 0 and b.ready() and b.failed() == 1


# --- snapshot ---


def test_snapshot_without_auth(camera):
    src = make_source(f"snapshot:{camera.url('')}")
    assert isinstance(src, SnapshotSource) and isinstance(src, FrameSource) and not src.replay
    f = src.read()
    assert f.image.shape == (36, 64, 3) and abs(int(f.image[0, 0, 0]) - 120) <= 2
    assert f.ts.tzinfo is not None and f.path is None
    src.close()


@pytest.mark.parametrize("camera", ["basic", "digest"], indirect=True)
def test_snapshot_auth_from_url(camera):
    src = make_source(f"snapshot:{camera.url()}")
    assert src.url == camera.url("")  # credentials not in the request URL
    for _ in range(3):
        assert src.read() is not None
    assert src.auth.scheme == camera.auth
    # one 401 to learn the scheme; basic is then sent up front
    assert camera.requests[0] is None
    if camera.auth == "basic":
        assert len(camera.requests) == 4
    src.close()


@pytest.mark.parametrize("camera", ["digest"], indirect=True)
def test_snapshot_wrong_password_fails_without_leaking(camera, caplog):
    src = SnapshotSource(camera.url("admin:Wr0ngSecret@"))
    with caplog.at_level(logging.WARNING):
        assert src.read() is None
    assert src.last_error == "HTTP 401" and "Wr0ngSecret" not in caplog.text
    src.close()


def test_snapshot_not_an_image(camera):
    camera.body = b"<html>login</html>"
    src = SnapshotSource(camera.url(""))
    assert src.read() is None and src.last_error.startswith("not an image")
    src.close()


def test_snapshot_backoff_then_recovery(camera, caplog):
    clock = FakeClock()
    src = SnapshotSource(camera.url(), backoff=Backoff(clock=clock))
    camera.status = 503
    with caplog.at_level(logging.INFO):
        assert src.read() is None  # attempt 1 → wait 1 s
        assert src.read() is None  # too early: no request
        assert len(camera.requests) == 1
        clock.t += 1
        assert src.read() is None  # attempt 2 → wait 2 s
        clock.t += 1
        assert src.read() is None and len(camera.requests) == 2
        camera.status = 200
        clock.t += 1
        assert src.read() is not None
    assert src.backoff.failures == 0 and src.last_error is None
    assert "attempt 2, next try in 2 s" in caplog.text and "back after 2" in caplog.text
    assert PASSWORD_Q not in caplog.text and PASSWORD not in caplog.text
    src.close()


def test_snapshot_unreachable_times_out_quickly():
    camera = Camera()
    url = camera.url("")
    camera.stop()  # nothing listens on the port any more
    src = SnapshotSource(url, timeout=1)
    t0 = time.monotonic()
    assert src.read() is None
    assert time.monotonic() - t0 < 2 and src.last_error == "ConnectError"
    src.close()


# --- rtsp ---


class FakeCapture:
    """Plays a script: each item is a frame value, or None for a failed read."""

    def __init__(self, script, opened=True, block: threading.Event | None = None):
        self.script = list(script)
        self.opened = opened
        self.block = block
        self.released = False
        self.reads = 0

    def isOpened(self):  # noqa: N802 (OpenCV's name)
        return self.opened

    def read(self):
        self.reads += 1
        if not self.script:
            if self.block is not None:
                self.block.wait(5)
            return False, None
        v = self.script.pop(0)
        time.sleep(0.002)
        return (False, None) if v is None else (True, np.full((4, 6, 3), v, np.uint8))

    def release(self):
        self.released = True


class Opener:
    def __init__(self, captures):
        self.captures = list(captures)
        self.opened: list[FakeCapture | None] = []

    def __call__(self, url):
        cap = self.captures.pop(0) if self.captures else FakeCapture([], opened=False)
        if isinstance(cap, Exception):
            self.opened.append(None)
            raise cap
        self.opened.append(cap)
        return cap


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def fast_backoff():
    return Backoff(first=0.01, cap=0.04)


def test_rtsp_make_source_is_lazy(monkeypatch):
    opened = []
    monkeypatch.setattr(sources, "_open_capture", lambda url: opened.append(url))
    src = make_source("rtsp:rtsp://u:p@10.0.20.11:554/sub")
    assert isinstance(src, RtspSource) and isinstance(src, FrameSource) and not src.replay
    assert src.url == "rtsp://u:p@10.0.20.11:554/sub" and opened == [] and src._thread is None
    src.close()


def test_rtsp_open_capture_uses_tcp_and_timeouts(monkeypatch):
    calls = []
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    monkeypatch.setattr(sources.cv2, "VideoCapture", lambda *a: calls.append(a) or "cap")
    assert sources._open_capture("rtsp://cam/sub") == "cap"
    assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "rtsp_transport;tcp|timeout;5000000"
    url, api, params = calls[0]
    assert (url, api) == ("rtsp://cam/sub", cv2.CAP_FFMPEG)
    assert params == [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000]
    monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;udp")
    sources._open_capture("rtsp://cam/sub")
    assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "rtsp_transport;udp"


def test_rtsp_keeps_only_the_latest_frame():
    hold = threading.Event()
    opener = Opener([FakeCapture([1, 2, 3, 4], block=hold)])
    src = RtspSource("rtsp://cam/sub", open_capture=opener, backoff=fast_backoff())
    assert src.read() is None  # starts the reader; nothing decoded yet
    assert wait_for(lambda: src.frames == 4)
    f = src.read()
    assert f.image[0, 0, 0] == 4 and f.ts.tzinfo is not None
    f.image[:] = 0  # the caller's copy
    assert src.read().image[0, 0, 0] == 4
    hold.set()
    src.close()


def test_rtsp_reconnects_with_backoff(caplog):
    hold = threading.Event()
    good = FakeCapture([7, 8], block=hold)
    opener = Opener(
        [
            FakeCapture([], opened=False),
            OSError("boom"),
            FakeCapture([5, None]),  # one frame, then the stream drops
            FakeCapture([], opened=False),
            good,
        ]
    )
    src = RtspSource("rtsp://u:Secret9@cam/sub", open_capture=opener, backoff=fast_backoff())
    with caplog.at_level(logging.INFO, logger="parking.vision.sources"):
        src.read()
        assert wait_for(lambda: src.frames == 3)
    assert src.read().image[0, 0, 0] == 8 and src.connected
    assert src.backoff.failures == 0
    assert opener.opened[0].released and opener.opened[2].released and opener.opened[3].released
    text = caplog.text
    assert "attempt 1 failed, next try in 0.01 s" in text
    assert "attempt 2 failed, next try in 0.02 s" in text
    assert "open raised OSError" in text and "stream read failed" in text
    assert "back after" in text and "Secret9" not in text
    hold.set()
    src.close()
    assert good.released and not src.connected


def test_rtsp_frame_older_than_max_age_is_none():
    clock = FakeClock()
    hold = threading.Event()
    opener = Opener([FakeCapture([3], block=hold)])
    src = RtspSource(
        "rtsp://cam/sub", open_capture=opener, backoff=Backoff(clock=clock), clock=clock
    )
    src.read()
    assert wait_for(lambda: src.frames == 1)
    clock.t += 5
    assert src.read() is not None  # exactly 5 s old still counts
    clock.t += 0.1
    assert src.read() is None
    hold.set()
    src.close()


def test_rtsp_close_stops_the_thread_and_never_restarts():
    hold = threading.Event()
    cap = FakeCapture([1], block=hold)
    src = RtspSource("rtsp://cam/sub", open_capture=Opener([cap]), backoff=fast_backoff())
    src.read()
    assert wait_for(lambda: src.frames == 1)
    thread = src._thread
    hold.set()
    src.close()
    assert not thread.is_alive() and cap.released
    assert src.read() is None and src._thread is None


def test_rtsp_restarts_a_crashed_reader():
    class Crashing(FakeCapture):
        def read(self):
            raise RuntimeError("decoder blew up")

    hold = threading.Event()
    opener = Opener([Crashing([]), FakeCapture([9], block=hold)])
    src = RtspSource("rtsp://cam/sub", open_capture=opener, backoff=fast_backoff())
    src.read()
    assert wait_for(lambda: not src._thread.is_alive())
    assert opener.opened[0].released
    src.read()  # a new reader
    assert wait_for(lambda: src.frames == 1)
    assert src.read().image[0, 0, 0] == 9
    hold.set()
    src.close()
