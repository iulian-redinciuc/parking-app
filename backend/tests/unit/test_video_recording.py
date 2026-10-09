"""P5.2: `video:` source, newest-frame RTSP counters, `parking record` and `stream-check`."""

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.vision import recording
from parking.vision.recording import (
    Recording,
    StreamStats,
    VideoInfo,
    measure_stream,
    record_command,
    redact,
)
from parking.vision.sources import (
    Backoff,
    Frame,
    FrameSource,
    RateMeter,
    RtspSource,
    VideoFileSource,
    make_source,
)

runner = CliRunner()
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def write_video(path: Path, n=20, fps=10.0) -> Path:
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (32, 16))
    assert w.isOpened()
    for i in range(n):
        w.write(np.full((16, 32, 3), i * 10, np.uint8))
    w.release()
    return path


def value(frame):
    return int(round(frame.image[8, 16, 0] / 10))


# --- video: ---


def test_video_fast_plays_every_frame_on_the_video_timeline(tmp_path):
    src = make_source(f"video:{write_video(tmp_path / 'a.avi')}?realtime=false")
    assert isinstance(src, VideoFileSource) and isinstance(src, FrameSource) and src.replay
    frames = [src.read() for _ in range(20)]
    assert [value(f) for f in frames] == list(range(20))
    assert [f.seq for f in frames] == list(range(1, 21))
    assert src.native_fps == pytest.approx(10)
    dt = (frames[-1].ts - frames[0].ts).total_seconds()
    assert dt == pytest.approx(1.9) and frames[0].ts.tzinfo is not None
    assert src.read() is None and src.exhausted and src.read() is None
    assert src.frames == 20 and src.dropped == 0
    src.close()


def test_video_loop_keeps_time_going(tmp_path):
    src = make_source(f"video:{write_video(tmp_path / 'a.avi', n=5)}?realtime=false&loop=true")
    frames = [src.read() for _ in range(12)]
    assert [value(f) for f in frames] == [0, 1, 2, 3, 4] * 2 + [0, 1]
    assert all(b.ts > a.ts for a, b in zip(frames, frames[1:], strict=False))
    assert src.passes == 3 and not src.exhausted
    src.close()


def test_video_realtime_paces_and_skips_when_late(tmp_path):
    clock = FakeClock()
    path = write_video(tmp_path / "a.avi")
    src = VideoFileSource(path, realtime=True, clock=clock, sleep=clock.sleep)
    assert value(src.read()) == 0 and clock.slept == []  # frame 0 is due at once
    assert value(src.read()) == 1 and clock.slept == [pytest.approx(0.1)]
    assert value(src.read()) == 2 and src.fps == pytest.approx(10)
    clock.t += 0.55  # the caller was busy until 0.75 s: 3–6 are skipped, 7 is the newest due
    f = src.read()
    assert value(f) == 7 and f.seq == 8 and src.dropped == 4
    assert len(clock.slept) == 2
    clock.t += 5  # past the end
    assert src.read() is None and src.exhausted
    src.close()


def test_video_missing_file_is_a_failed_read(tmp_path, caplog):
    src = make_source("video:nope.mp4", root=tmp_path)
    assert src.read() is None and not src.exhausted
    assert "not found" in caplog.text
    (tmp_path / "bad.mp4").write_bytes(b"not a video")
    assert make_source("video:bad.mp4", root=tmp_path).read() is None


@pytest.mark.parametrize(
    ("uri", "msg"),
    [
        ("video:a.mp4?speed=2", "unknown option"),
        ("video:a.mp4?realtime=maybe", "not a boolean"),
        ("video:rtsp://u:secret@cam/x", "use 'rtsp:'"),
    ],
)
def test_video_uri_errors(uri, msg):
    with pytest.raises(ValueError, match=msg) as e:
        make_source(uri)
    assert "secret" not in str(e.value)


def test_video_uri_defaults(tmp_path):
    src = make_source("video:data/recordings/x.mp4", root=tmp_path)
    assert src.realtime and not src.loop
    assert src.path == tmp_path / "data" / "recordings" / "x.mp4"


# --- rtsp: newest frame, dropped backlog, fps ---


class ScriptCapture:
    def __init__(self, values, hold):
        self.values = list(values)
        self.hold = hold

    def isOpened(self):  # noqa: N802
        return True

    def read(self):
        if not self.values:
            self.hold.wait(5)
            return False, None
        return True, np.full((2, 2, 3), self.values.pop(0), np.uint8)

    def release(self):
        pass


def test_rtsp_returns_the_newest_frame_and_counts_the_dropped_ones():
    hold = threading.Event()
    src = RtspSource(
        "rtsp://cam/sub",
        open_capture=lambda url: ScriptCapture([1, 2, 3, 4, 5], hold),
        backoff=Backoff(first=0.01),
    )
    src.read()
    deadline = time.monotonic() + 3
    while src.frames < 5 and time.monotonic() < deadline:
        time.sleep(0.005)
    f = src.read()
    assert f.image[0, 0, 0] == 5 and f.seq == 5
    assert src.dropped == 4  # 1–4 were overwritten unread
    assert src.read().seq == 5  # the same frame again: same seq, nothing more dropped
    assert src.dropped == 4 and src.fps > 0
    hold.set()
    src.close()


def test_rtsp_capture_options_ask_for_low_latency():
    from parking.vision.sources import RTSP_CAPTURE_OPTIONS

    assert "rtsp_transport;tcp" in RTSP_CAPTURE_OPTIONS
    assert "fflags;nobuffer" in RTSP_CAPTURE_OPTIONS and "flags;low_delay" in RTSP_CAPTURE_OPTIONS


def test_rate_meter():
    clock = FakeClock()
    m = RateMeter(window=2, clock=clock)
    assert m.rate == 0
    for _ in range(31):
        m.tick()
        clock.t += 0.1
    clock.t -= 0.1
    assert m.rate == pytest.approx(10)
    clock.t += 3  # nothing for longer than the window
    assert m.rate == 0


# --- recording helpers ---


def test_record_command_is_stream_copy_over_tcp(tmp_path):
    cmd = record_command("ffmpeg", "rtsp://u:p@cam/sub", 3600, tmp_path / "o.mp4")
    s = " ".join(cmd)
    assert "-rtsp_transport tcp" in s and "-c copy" in s and "-t 3600" in s
    assert "-map 0:v:0" in s and "frag_keyframe" in s and "-c:v" not in s
    assert cmd[-2:] == ["-n", str(tmp_path / "o.mp4")]
    assert "-rtsp_transport" not in record_command("ffmpeg", "/x.mp4", 1, tmp_path / "o.mp4")


def test_redact_hides_url_and_password():
    url = "rtsp://admin:p%40ss@10.0.20.12:554/sub"
    text = f"{url}: Connection refused; tried p@ss and p%40ss"
    out = redact(text, url)
    assert "p@ss" not in out and "p%40ss" not in out and "10.0.20.12" not in out
    assert "<camera url>" in out
    assert redact("plain", "rtsp://cam/sub") == "plain"


def test_rate_parsing_and_completeness(tmp_path):
    assert recording._rate("30000/1001") == pytest.approx(29.97, abs=0.01)
    assert recording._rate("0/0") is None and recording._rate(None) is None
    info = VideoInfo("h264", 640, 360, 10.0, 600, 59.0)
    assert Recording(tmp_path, 60, 0, "", info).complete
    info.duration_s = 50
    assert not Recording(tmp_path, 60, 0, "", info).complete
    assert not Recording(tmp_path, 60, 1, "").complete


class FakeProc:
    def __init__(self, cmd, returncode=0, err="", hang=False):
        self.cmd = cmd
        self.returncode = returncode
        self.err = err
        self.hang = hang
        self.terminated = False

    def communicate(self, timeout=None):
        if self.hang and not self.terminated:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        Path(self.cmd[-1]).write_bytes(b"x")
        return "", self.err

    def terminate(self):
        self.terminated = True


def test_record_redacts_and_probes(tmp_path, monkeypatch):
    url = "rtsp://u:secretpw@cam/sub"
    procs = []

    def run(cmd, **kw):
        procs.append(FakeProc(cmd, 0, f"[rtsp @ 0x1] {url}: non-monotonic DTS"))
        return procs[-1]

    monkeypatch.setattr(recording, "probe", lambda p, f: VideoInfo("h264", 640, 360, 10, 50, 5))
    rec = recording.record(url, 5, tmp_path / "r" / "o.mp4", "ffmpeg", "ffprobe", run=run)
    assert rec.complete and rec.info.frames == 50
    assert "secretpw" not in rec.errors and "<camera url>" in rec.errors

    def hang(cmd, **kw):
        procs.append(FakeProc(cmd, -15, "", hang=True))
        return procs[-1]

    rec = recording.record(url, 5, tmp_path / "o2.mp4", "ffmpeg", "ffprobe", run=hang)
    assert procs[-1].terminated and "stopped" in rec.errors


def test_probe_reads_ffprobe_json(tmp_path, monkeypatch):
    out = {
        "streams": [
            {
                "codec_name": "h264",
                "width": 640,
                "height": 360,
                "avg_frame_rate": "10/1",
                "nb_read_packets": "36000",
            }
        ],
        "format": {"duration": "3600.1"},
    }

    def run(cmd, **kw):
        assert "-count_packets" in cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    monkeypatch.setattr(recording.subprocess, "run", run)
    info = recording.probe(tmp_path / "x.mp4", "ffprobe")
    assert info == VideoInfo("h264", 640, 360, 10.0, 36000, 3600.1)
    monkeypatch.setattr(
        recording.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "bad"),
    )
    with pytest.raises(recording.RecordError, match="bad"):
        recording.probe(tmp_path / "x.mp4", "ffprobe")


# --- measure_stream ---


class PolledSource:
    """A live source: a new frame every `period` s of the fake clock (gaps optional)."""

    replay = False

    def __init__(self, clock, period=0.1, gap_at=None, gap=0.0):
        self.clock = clock
        self.period = period
        self.gap_at = gap_at
        self.gap = gap
        self.start = clock()

    def read(self):
        t = self.clock() - self.start
        if self.gap_at is not None and t > self.gap_at:
            t = max(self.gap_at, t - self.gap)
        seq = int(t / self.period)
        return Frame(np.zeros((1, 1, 3), np.uint8), None, seq=seq)

    def close(self):
        pass


def test_measure_stream_steady():
    clock = FakeClock()
    stats = measure_stream(
        PolledSource(clock), 30, window=5, poll_s=0.01, clock=clock, sleep=clock.sleep
    )
    assert stats.fps == pytest.approx(10, rel=0.02) and stats.steady
    assert len(stats.windows) == 5 and stats.repeats > 0 and stats.misses == 0
    assert stats.max_gap_s == pytest.approx(0.1, abs=0.02)
    assert stats.to_dict()["steady"] is True


def test_measure_stream_gap_is_not_steady():
    clock = FakeClock()
    src = PolledSource(clock, gap_at=12, gap=3)
    stats = measure_stream(src, 30, window=5, poll_s=0.01, clock=clock, sleep=clock.sleep)
    assert stats.max_gap_s >= 3 and not stats.steady


def test_measure_stream_without_frames_gives_up():
    clock = FakeClock()

    class Dead:
        def read(self):
            return None

    stats = measure_stream(Dead(), 30, start_timeout=2, clock=clock, sleep=clock.sleep)
    assert stats.frames == 0 and stats.misses > 0 and not stats.steady
    assert not StreamStats(1, 1, 0, 0, [10.0]).steady


# --- CLI ---


def write_lot(tmp_path, monkeypatch, source="rtsp:rtsp://u:secretpw@127.0.0.1:9/sub"):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lot.yaml").write_text(
        f"""
version: 1
lot: {{id: main, name: P, location: {{lat: 0, lon: 0}}, timezone: Europe/Bucharest}}
zones:
  - {{id: underground, name: {{en: U}}, method: flow, capacity: 10}}
cameras:
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "{source}"
    lines_file: config/lines/cam-ramp.json
    detector: {{model: models/none}}
"""
    )
    monkeypatch.chdir(tmp_path)


def test_record_cli(tmp_path, monkeypatch):
    write_lot(tmp_path, monkeypatch)
    monkeypatch.setattr(recording, "ffmpeg_tool", lambda name="ffmpeg": name)
    calls = []

    def fake_record(url, seconds, out):
        calls.append((url, seconds, out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 1000)
        info = VideoInfo("h264", 640, 360, 10.0, 1200, 120.0)
        return Recording(out, seconds, 0, "", info)

    monkeypatch.setattr(recording, "record", fake_record)
    res = runner.invoke(app, ["record", "--camera", "cam-ramp", "--minutes", "2"])
    assert res.exit_code == 0, res.output
    url, seconds, out = calls[0]
    assert url == "rtsp://u:secretpw@127.0.0.1:9/sub" and seconds == 120
    assert out.parent == tmp_path / "data" / "recordings" and out.name.startswith("cam-ramp-")
    assert "1200 frames, 10.00 fps" in res.output and "secretpw" not in res.output

    res = runner.invoke(app, ["record", "--camera", "cam-ramp", "--minutes", "3", "--out", "x.mp4"])
    assert res.exit_code == 1 and "cut short: 120 of 180 s" in res.output
    res = runner.invoke(app, ["record", "--camera", "cam-ramp", "--out", "x.mp4"])
    assert res.exit_code == 1 and "exists" in res.output
    res = runner.invoke(app, ["record", "--camera", "cam-ramp", "--source", "video:x.mp4"])
    assert res.exit_code == 1 and "needs an 'rtsp:' source" in res.output
    res = runner.invoke(app, ["record", "--camera", "cam-ramp", "--minutes", "0"])
    assert res.exit_code == 2


def test_record_cli_without_ffmpeg(tmp_path, monkeypatch):
    write_lot(tmp_path, monkeypatch)
    monkeypatch.setattr(recording.shutil, "which", lambda name: None)
    res = runner.invoke(app, ["record", "--camera", "cam-ramp"])
    assert res.exit_code == 1 and "vision container" in res.output


def test_stream_check_cli_on_a_recording(tmp_path, monkeypatch):
    write_lot(tmp_path, monkeypatch)
    write_video(tmp_path / "r.avi", n=20)
    args = ["stream-check", "--source", "video:r.avi", "--seconds", "1.5", "--window", "0.5"]
    res = runner.invoke(app, [*args, "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["steady"] and out["native_fps"] == pytest.approx(10)
    assert out["fps"] == pytest.approx(10, rel=0.15) and out["frames"] >= 14
    res = runner.invoke(app, ["stream-check", "--source", "video:missing.mp4", "--seconds", "1"])
    assert res.exit_code == 1 and "NOT steady" in res.output
    assert runner.invoke(app, ["stream-check"]).exit_code == 2


# --- the real thing: FFmpeg + the fake RTSP camera (skipped without FFmpeg) ---


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs the ffmpeg binary")
def test_record_and_read_the_fake_rtsp_camera(tmp_path):
    port = _free_port()
    script = Path(__file__).parents[2] / "scripts" / "fake_rtsp.py"
    server = subprocess.Popen(
        [sys.executable, str(script), "--port", str(port), "--gop-s", "1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert "serving" in server.stdout.readline()
        url = f"rtsp://u:pw@127.0.0.1:{port}/sub"
        rec = recording.record(url, 4, tmp_path / "rec.mp4")
        assert rec.returncode == 0, rec.errors
        assert rec.complete and rec.info.codec == "h264" and rec.info.width == 640
        assert rec.info.fps == pytest.approx(10, rel=0.1)

        src = make_source(f"video:{rec.path}?realtime=false")
        n = 0
        while src.read() is not None:
            n += 1
        assert n == rec.info.frames and n >= 30

        live = make_source(f"rtsp:{url}")
        stats = measure_stream(live, 4, window=2)
        live.close()
        assert stats.fps == pytest.approx(10, rel=0.2), stats
    finally:
        server.terminate()
        server.wait(10)
