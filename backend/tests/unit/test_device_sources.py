"""Cameras plugged into the vision host (P4.12): `device:` and `picamera:` with fake captures
and a fake `rpicam-vid` (a shell script); no camera hardware needed."""

import logging
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from parking.vision import sources
from parking.vision.sources import (
    Backoff,
    CaptureError,
    DeviceSettings,
    DeviceSource,
    FrameSource,
    PicameraSettings,
    PicameraSource,
    RpicamCapture,
    make_source,
)


def _jpeg(value: int, size=(36, 64)) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((*size, 3), value, np.uint8))
    assert ok
    return buf.tobytes()


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def fast_backoff():
    return Backoff(first=0.01, cap=0.04)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeDevice:
    """A V4L2 capture playing a script: each item is a frame value, or None for a failed grab.
    Counts what was decoded (`retrieve`) apart from what was only taken off the queue."""

    def __init__(self, script, opened=True, block: threading.Event | None = None, on_grab=None):
        self.script = list(script)
        self.opened = opened
        self.block = block
        self.on_grab = on_grab
        self.released = False
        self.props: dict[int, float] = {}
        self.set_order: list[int] = []
        self.grabs = 0
        self.decoded: list[int] = []
        self._current = None

    def isOpened(self):  # noqa: N802 (OpenCV's name)
        return self.opened

    def set(self, prop, value):
        self.props[prop] = value
        self.set_order.append(prop)
        return True

    def get(self, prop):
        return self.props.get(prop, 0)

    def grab(self):
        if not self.script:
            if self.block is not None:
                self.block.wait(5)
            return False
        self._current = self.script.pop(0)
        self.grabs += 1
        if self.on_grab:
            self.on_grab()
        time.sleep(0.002)
        return self._current is not None

    def retrieve(self):
        self.decoded.append(self._current)
        return True, np.full((4, 6, 3), self._current, np.uint8)

    def release(self):
        self.released = True


class Opener:
    def __init__(self, captures):
        self.captures = list(captures)
        self.opened = []
        self.targets = []

    def __call__(self, target):
        self.targets.append(target)
        cap = self.captures.pop(0) if self.captures else CaptureError("gone")
        self.opened.append(cap)
        if isinstance(cap, Exception):
            raise cap
        return cap


# --- URIs ---


def test_device_uri(monkeypatch):
    opened = []
    monkeypatch.setattr(sources, "_open_device", lambda s: opened.append(s))
    src = make_source("device:/dev/video0?width=3840&height=2160&fourcc=MJPG&fps=1")
    assert isinstance(src, DeviceSource) and isinstance(src, FrameSource) and not src.replay
    assert src.settings == DeviceSettings("/dev/video0", 3840, 2160, "MJPG", 1.0)
    assert opened == []  # nothing is opened before the first read
    assert make_source("device:2").settings == DeviceSettings(2)
    by_id = "device:/dev/v4l/by-id/usb-Cam-video-index0"
    assert make_source(by_id).settings.device == "/dev/v4l/by-id/usb-Cam-video-index0"


def test_picamera_uri():
    src = make_source("picamera:0?width=4608&height=2592")
    assert isinstance(src, PicameraSource) and isinstance(src, FrameSource) and not src.replay
    assert src.settings == PicameraSettings(0, 4608, 2592, fps=2.0)
    s = make_source("picamera:1?fps=0.5&focus=0&rotation=180").settings
    assert (s.camera, s.fps, s.focus, s.rotation) == (1, 0.5, 0.0, 180)


@pytest.mark.parametrize(
    ("uri", "message"),
    [
        ("device:video0", "/dev/"),
        ("device:/dev/video0?width=big", "width must be a number"),
        ("device:/dev/video0?height=0", "height must be"),
        ("device:/dev/video0?fourcc=MJPEG", "fourcc"),
        ("device:/dev/video0?fps=0", "fps must be"),
        ("device:/dev/video0?focus=0", "unknown option"),
        ("picamera:/dev/video0", "camera index"),
        ("picamera:0?rotation=90", "rotation"),
        ("picamera:0?focus=-1", "focus must be"),
        ("picamera:0?fourcc=MJPG", "unknown option"),
        ("picamera:", "expected"),
    ],
)
def test_bad_uris(uri, message):
    with pytest.raises(ValueError, match=message):
        make_source(uri)


def test_unknown_scheme_lists_the_new_ones():
    with pytest.raises(ValueError, match="device, picamera"):
        make_source("webcam:0")


# --- device: ---


def test_device_keeps_only_the_latest_frame():
    hold = threading.Event()
    opener = Opener([FakeDevice([1, 2, 3, 4], block=hold)])
    settings = DeviceSettings("/dev/video0")
    src = DeviceSource(settings, open_capture=opener, backoff=fast_backoff())
    assert src.read() is None  # starts the reader; nothing decoded yet
    assert wait_for(lambda: src.frames == 4)
    f = src.read()
    assert f.image[0, 0, 0] == 4 and f.seq == 4 and f.ts.tzinfo is not None
    assert opener.targets == [settings] and src.dropped == 3 and src.connected
    assert src._thread.name == "device-reader"
    hold.set()
    src.close()
    assert opener.opened[0].released and not src.connected


def test_device_fps_decodes_only_that_many_frames():
    clock = FakeClock()
    hold = threading.Event()

    def tick():  # the camera delivers 10 frames a second
        clock.t += 0.1

    cap = FakeDevice(range(1, 31), block=hold, on_grab=tick)
    src = DeviceSource(
        DeviceSettings("/dev/video0", fps=1),
        open_capture=Opener([cap]),
        backoff=Backoff(clock=clock),
        clock=clock,
    )
    src.read()
    assert wait_for(lambda: cap.grabs == 30)
    assert cap.decoded == [1, 9, 17, 25]  # 30 frames in 3 s taken off the queue, 4 decoded
    assert src.frames == 4 and src.read().image[0, 0, 0] == 25
    assert src.max_age == 5.0
    hold.set()
    src.close()


def test_device_slow_fps_stretches_the_max_age():
    src = DeviceSource(DeviceSettings(0, fps=0.2), open_capture=Opener([]))
    assert src.max_age == 15.0  # a frame every 5 s must not count as stale in between


def test_device_reconnects_and_says_why(caplog):
    hold = threading.Event()
    good = FakeDevice([7, 8], block=hold)
    opener = Opener(
        [
            CaptureError("/dev/video0 not found: is the camera plugged in?"),
            FakeDevice([], opened=False),
            FakeDevice([5, None]),  # one frame, then the cable is pulled
            CaptureError("can't open /dev/video0: busy"),
            good,
        ]
    )
    src = DeviceSource(DeviceSettings("/dev/video0"), open_capture=opener, backoff=fast_backoff())
    errors = []
    with caplog.at_level(logging.INFO, logger="parking.vision.sources"):
        src.read()
        assert wait_for(lambda: src.last_error is not None)
        errors.append(src.last_error)
        assert wait_for(lambda: src.frames == 3)
    assert errors == ["/dev/video0 not found: is the camera plugged in?"]
    assert src.read().image[0, 0, 0] == 8 and src.connected
    assert src.last_error is None and src.backoff.failures == 0 and src.reconnects == 4
    assert opener.opened[1].released and opener.opened[2].released
    text = caplog.text
    assert "device: attempt 1 failed (/dev/video0 not found" in text
    assert "device: attempt 2 failed, next try in 0.02 s" in text
    assert "device: stream read failed, reconnecting" in text
    assert "failed (can't open /dev/video0: busy), next try" in text
    assert "device: camera back after" in text
    hold.set()
    src.close()
    assert good.released


def test_open_device_missing(tmp_path):
    with pytest.raises(CaptureError, match="not found: is the camera plugged in"):
        sources._open_device(DeviceSettings(str(tmp_path / "video9")))


@pytest.mark.skipif(os.geteuid() == 0, reason="root may open anything")
def test_open_device_no_permission(tmp_path):
    node = tmp_path / "video0"
    node.write_bytes(b"")
    node.chmod(0)
    with pytest.raises(CaptureError, match="'video' group"):
        sources._open_device(DeviceSettings(str(node)))


def test_open_device_busy_or_not_a_camera(tmp_path, monkeypatch):
    node = tmp_path / "video0"
    node.write_bytes(b"")
    cap = FakeDevice([], opened=False)
    monkeypatch.setattr(cv2, "VideoCapture", lambda *a: cap)
    with pytest.raises(CaptureError, match="busy .* or not a capture device"):
        sources._open_device(DeviceSettings(str(node)))
    assert cap.released


def test_open_device_applies_the_settings(tmp_path, monkeypatch, caplog):
    node = tmp_path / "video0"
    node.write_bytes(b"")
    calls = []
    cap = FakeDevice([])

    def capture(*args):
        calls.append(args)
        return cap

    monkeypatch.setattr(cv2, "VideoCapture", capture)
    settings = DeviceSettings(str(node), 3840, 2160, "MJPG", 5)
    assert sources._open_device(settings) is cap
    assert calls == [(str(node), cv2.CAP_V4L2)]
    assert cap.props == {
        cv2.CAP_PROP_FOURCC: cv2.VideoWriter_fourcc(*"MJPG"),
        cv2.CAP_PROP_FRAME_WIDTH: 3840,
        cv2.CAP_PROP_FRAME_HEIGHT: 2160,
        cv2.CAP_PROP_FPS: 5,
        cv2.CAP_PROP_BUFFERSIZE: 1,
    }
    assert cap.set_order[0] == cv2.CAP_PROP_FOURCC  # the sizes on offer depend on the format
    assert "asked for" not in caplog.text

    # a camera that picks another mode: said once, in the log
    class Stubborn(FakeDevice):
        def get(self, prop):
            return {cv2.CAP_PROP_FRAME_WIDTH: 1920, cv2.CAP_PROP_FRAME_HEIGHT: 1080}.get(prop, 0)

    cap = Stubborn([])
    with caplog.at_level(logging.WARNING, logger="parking.vision.sources"):
        sources._open_device(settings)
    assert "asked for 3840x2160, the camera gives 1920x1080" in caplog.text

    cap = FakeDevice([])  # an index, no settings: only the queue length is set
    sources._open_device(DeviceSettings(0))
    assert calls[-1] == (0, cv2.CAP_V4L2) and list(cap.props) == [cv2.CAP_PROP_BUFFERSIZE]


# --- picamera: ---


def test_picamera_command():
    cmd = PicameraSettings(0, 4608, 2592).command("rpicam-vid")
    assert cmd[:6] == ["rpicam-vid", "--camera", "0", "--timeout", "0", "--nopreview"]
    assert " ".join(cmd).endswith("--framerate 2 --flush --output - --width 4608 --height 2592")
    assert "--codec mjpeg --quality 90" in " ".join(cmd)
    assert "--lens-position" not in cmd and "--rotation" not in cmd
    cmd = PicameraSettings(1, fps=0.5, focus=0, rotation=180).command("libcamera-vid")
    text = " ".join(cmd)
    assert cmd[0] == "libcamera-vid" and "--camera 1" in text and "--framerate 0.5" in text
    assert "--autofocus-mode manual --lens-position 0" in text and "--rotation 180" in text
    assert "--width" not in cmd


def _script(tmp_path: Path, name: str, body: str) -> str:
    """An executable Python script standing in for rpicam-vid."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\nimport sys, time\nout = sys.stdout.buffer\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _frames_file(tmp_path: Path, *values: int) -> Path:
    path = tmp_path / "frames.bin"
    path.write_bytes(b"".join(_jpeg(v) for v in values))
    return path


def test_rpicam_capture_splits_the_mjpeg_stream(tmp_path):
    data = _frames_file(tmp_path, 10, 200, 90)
    # written in small pieces, so frames and their end markers straddle the reads
    body = f"""
data = open({str(data)!r}, 'rb').read()
for i in range(0, len(data), 97):
    out.write(data[i:i + 97]); out.flush(); time.sleep(0.001)
"""
    cap = RpicamCapture([_script(tmp_path, "rpicam-vid", body)], timeout=5)
    seen = []
    for _ in range(3):
        ok, img = cap.read()
        assert ok and img.shape == (36, 64, 3)
        seen.append(int(img[0, 0, 0]))
    assert [round(v, -1) for v in seen] == [10, 200, 90]
    assert cap.read() == (False, None)  # the process ended
    assert cap.retrieve() == (False, None)
    cap.release()


def test_rpicam_capture_skips_a_frame_joined_half_way(tmp_path):
    data = _frames_file(tmp_path, 50, 150)
    body = f"data = open({str(data)!r}, 'rb').read()\nout.write(data[40:]); out.flush()"
    cap = RpicamCapture([_script(tmp_path, "rpicam-vid", body)], timeout=5)
    ok, img = cap.read()
    assert ok and round(int(img[0, 0, 0]), -1) == 150
    cap.release()


def test_rpicam_capture_times_out_and_stops_the_process(tmp_path):
    cap = RpicamCapture([_script(tmp_path, "rpicam-vid", "time.sleep(60)")], timeout=0.2)
    assert cap.isOpened()
    start = time.monotonic()
    assert not cap.grab()
    assert time.monotonic() - start < 2
    cap.release()
    assert not cap.isOpened() and cap._proc.returncode is not None
    cap.release()  # twice is fine


def test_open_picamera_without_rpicam(monkeypatch):
    monkeypatch.setattr(sources.shutil, "which", lambda name: None)
    with pytest.raises(CaptureError, match="rpicam-vid not found: install rpicam-apps"):
        sources._open_picamera(PicameraSettings())


def test_open_picamera_reports_what_rpicam_said(tmp_path, monkeypatch):
    # what rpicam-vid 1.11 prints with no camera attached (colour codes included); it exits 0
    body = r"""
sys.stderr.write("[0:01:02.3] [42] \x1b[1;32m INFO \x1b[1;37mCamera \x1b[0mlibcamera v0.6.0\n")
sys.stderr.write("ERROR: *** no cameras available ***\n")
"""
    fake = _script(tmp_path, "rpicam-vid", body)
    monkeypatch.setattr(
        sources.shutil, "which", lambda name: fake if name == "rpicam-vid" else None
    )
    with pytest.raises(CaptureError) as e:
        sources._open_picamera(PicameraSettings())
    assert str(e.value) == "rpicam-vid: no cameras available"


def test_open_picamera_falls_back_to_the_old_name(tmp_path, monkeypatch):
    args = tmp_path / "args.txt"
    data = _frames_file(tmp_path, 120)
    body = f"""
open({str(args)!r}, 'w').write(' '.join(sys.argv[1:]))
out.write(open({str(data)!r}, 'rb').read()); out.flush(); time.sleep(60)
"""
    fake = _script(tmp_path, "libcamera-vid", body)
    monkeypatch.setattr(
        sources.shutil, "which", lambda name: fake if name == "libcamera-vid" else None
    )
    cap = sources._open_picamera(PicameraSettings(0, 4608, 2592))
    try:
        assert cap.isOpened()
        assert "--codec mjpeg" in args.read_text() and "--width 4608" in args.read_text()
    finally:
        cap.release()
    assert not cap.isOpened()


def test_picamera_source_restarts_a_dead_process(tmp_path, caplog):
    data = _frames_file(tmp_path, 30, 60)
    dying = _script(
        tmp_path,
        "dying",
        f"out.write(open({str(data)!r}, 'rb').read()); out.flush()\n"
        "sys.stderr.write('ERROR: *** failed to dequeue buffer ***\\n')",
    )
    data2 = tmp_path / "frames2.bin"
    data2.write_bytes(_jpeg(220))
    alive = _script(
        tmp_path,
        "alive",
        f"out.write(open({str(data2)!r}, 'rb').read()); out.flush()\ntime.sleep(60)",
    )
    commands = [[dying], [alive]]
    caps = []

    def opener(settings):
        cap = RpicamCapture(commands.pop(0), timeout=5)
        caps.append(cap)
        return cap

    src = PicameraSource(PicameraSettings(), open_capture=opener, backoff=fast_backoff())
    assert src.max_age == 5.0
    with caplog.at_level(logging.INFO, logger="parking.vision.sources"):
        src.read()
        assert wait_for(lambda: src.frames == 3, timeout=10)
    assert round(int(src.read().image[0, 0, 0]), -1) == 220
    assert src.reconnects == 1 and src.last_error is None
    assert "picamera: attempt 1 failed (failed to dequeue buffer)" in caplog.text
    src.close()
    assert all(c._proc.poll() is not None for c in caps)  # no rpicam-vid left behind


def test_picamera_source_slow_fps_stretches_the_max_age():
    assert PicameraSource(PicameraSettings(fps=0.2), open_capture=Opener([])).max_age == 15.0


def test_rpicam_release_ends_the_process(tmp_path):
    cap = RpicamCapture([_script(tmp_path, "rpicam-vid", "time.sleep(60)")], timeout=0.1)
    pid = cap._proc.pid
    cap.release()
    assert subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0
