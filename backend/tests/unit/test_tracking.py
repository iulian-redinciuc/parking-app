"""Tracker wrapper (vision/tracking.py): gate handling, ROI mask, id offsets, track log."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from parking.config import LineFile
from parking.vision.detector import Detection
from parking.vision.motion import MotionResult
from parking.vision.sources import Frame
from parking.vision.tracking import (
    RESET_AFTER_S,
    RoiMask,
    TrackLog,
    UltralyticsTracker,
    VehicleTracker,
    anchor,
    draw_tracks,
    run_tracking,
)

FPS = 10.0
W, H = 640, 360


def lines() -> LineFile:
    return LineFile.model_validate(
        {
            "version": 1,
            "camera_id": "cam-ramp",
            "image_size": [1280, 720],  # drawn on a bigger frame: the ROI must be scaled
            "roi": [[120, 240], [1200, 240], [1200, 720], [120, 720]],
            "line_a": [[200, 440], [1120, 440]],
            "line_b": [[200, 540], [1120, 540]],
        }
    )


class FakeBackend:
    """Restarts its ids at 1 after a reset, like ByteTrack."""

    def __init__(self):
        self.calls: list[str] = []
        self.seen: list[np.ndarray] = []
        self.next_id = 1

    def track(self, image):
        self.calls.append("track")
        self.seen.append(image)
        tid = self.next_id
        return [Detection("car", 0.9, (10, 20, 110, 80), track_id=tid)]

    def idle(self, image):
        self.calls.append("idle")

    def reset(self):
        self.calls.append("reset")
        self.next_id = 1


def frame(value: int = 200) -> np.ndarray:
    return np.full((H, W, 3), value, np.uint8)


def test_active_frames_are_tracked_inside_the_roi_only():
    be = FakeBackend()
    tr = VehicleTracker(be, lines())
    out = tr.update(frame(), 0.0, active=True)
    assert [d.track_id for d in out] == [1]
    masked = be.seen[0]
    assert masked[0, 0].tolist() == [0, 0, 0]  # above the ROI (y < 120 at 640×360)
    assert masked[10, 300].tolist() == [0, 0, 0]
    assert masked[200, 300].tolist() == [200, 200, 200]  # inside
    assert masked[200, 30].tolist() == [0, 0, 0]  # left of x = 60


def test_idle_feeds_empty_updates_then_resets_after_10_s():
    be = FakeBackend()
    tr = VehicleTracker(be)
    tr.update(frame(), 0.0, active=True)
    t = 0.1
    while t < 0.1 + RESET_AFTER_S - 0.05:
        assert tr.update(frame(), t, active=False) == []
        t += 0.1
    assert "reset" not in be.calls  # a short idle spell keeps the tracks
    assert be.calls.count("idle") == len(be.calls) - 1
    tr.update(frame(), 0.1 + RESET_AFTER_S, active=False)
    assert be.calls[-1] == "reset" and tr.resets == 1
    n = len(be.calls)
    tr.update(frame(), 0.2 + RESET_AFTER_S, active=False)  # already reset: nothing more
    assert len(be.calls) == n


def test_activity_restarts_the_idle_clock():
    be = FakeBackend()
    tr = VehicleTracker(be)
    for i in range(3):  # 3 × (9 s idle, then a car moves again)
        tr.update(frame(), i * 10.0, active=True)
        for k in range(1, 91):
            tr.update(frame(), i * 10.0 + k / FPS, active=False)
    assert "reset" not in be.calls


def test_no_reset_or_idle_before_the_first_track():
    be = FakeBackend()
    tr = VehicleTracker(be)
    for k in range(200):
        tr.update(frame(), k / FPS, active=False)
    assert be.calls == []


def test_ids_stay_unique_across_resets():
    be = FakeBackend()
    tr = VehicleTracker(be)
    first = tr.update(frame(), 0.0, active=True)[0].track_id
    be.next_id = 3
    tr.update(frame(), 0.1, active=True)
    tr.update(frame(), 1.0, active=False)
    tr.update(frame(), 1.0 + RESET_AFTER_S, active=False)  # backend ids restart at 1
    again = tr.update(frame(), 20.0, active=True)[0].track_id
    assert first == 1 and again == 4  # offset by the highest id handed out (3)


def test_roi_mask_without_roi_returns_the_frame():
    img = frame()
    assert RoiMask(None)(img) is img
    lf = lines().model_copy(update={"roi": None})
    assert RoiMask(lf)(img) is img


def test_track_log_spans():
    log = TrackLog()
    log.add([Detection("car", 0.9, (0, 0, 10, 10), track_id=7)], 1.0)
    log.add([Detection("car", 0.8, (100, 0, 120, 20), track_id=7)], 3.5)
    log.add([Detection("car", 0.8, (0, 0, 1, 1))], 4.0)  # untracked: ignored
    span = log.spans[7]
    assert (span.frames, span.seconds) == (2, 2.5)
    assert span.first_anchor == (5.0, 10.0) and span.last_anchor == (110.0, 20.0)
    assert span.to_dict()["track_id"] == 7
    assert anchor((0, 0, 10, 30)) == (5.0, 30.0)


def test_draw_tracks_keeps_the_input_and_draws():
    img = frame(100)
    out = draw_tracks(img, [Detection("car", 0.9, (50, 150, 200, 250), track_id=3)], lines(), "x")
    assert out.shape == img.shape and not np.array_equal(out, img)
    assert (img == 100).all()


class Source:
    def __init__(self, n: int):
        self.i, self.n, self.exhausted = 0, n, False
        self.t0 = datetime(2026, 10, 10, 3, tzinfo=UTC)

    def read(self):
        if self.i >= self.n:
            self.exhausted = True
            return None
        self.i += 1
        return Frame(frame(), self.t0 + timedelta(seconds=(self.i - 1) / FPS), seq=self.i)

    def close(self):
        pass


class ScriptedGate:
    def __init__(self, active):
        self.active = active  # frame index -> bool
        self.i = 0

    def update(self, image, ts):
        a = self.active(self.i)
        self.i += 1
        return MotionResult(a, a, 0.0)


def test_run_tracking_collects_tracks_and_resets():
    be = FakeBackend()
    tr = VehicleTracker(be)
    seen = []
    gate = ScriptedGate(lambda i: i < 20)  # 2 s of motion, then 28 s idle
    stats = run_tracking(
        Source(300), gate, tr, seconds=60, on_frame=lambda img, e, a, t: seen.append(a)
    )
    assert stats.frames == 300 and stats.active_frames == 20
    assert stats.resets == 1 and len(stats.spans) == 1
    assert stats.spans[0].frames == 20
    assert stats.to_dict()["tracks"][0]["track_id"] == 1
    assert len(seen) == 300


def test_ultralytics_idle_and_reset_on_a_real_bytetracker():
    pytest.importorskip("ultralytics")
    from ultralytics.engine.results import Boxes
    from ultralytics.trackers.byte_tracker import BYTETracker
    from ultralytics.utils import YAML, IterableSimpleNamespace
    from ultralytics.utils.checks import check_yaml

    cfg = IterableSimpleNamespace(**YAML.load(check_yaml("bytetrack.yaml")))
    bt = BYTETracker(cfg)
    ut = UltralyticsTracker.__new__(UltralyticsTracker)
    ut.model = SimpleNamespace(predictor=SimpleNamespace(trackers=[bt]))
    img = frame()
    car = Boxes(np.array([[100, 150, 200, 220, 0.9, 2]], np.float32), img.shape[:2])
    for _ in range(3):
        bt.update(car, img)
    assert len(bt.tracked_stracks) == 1
    for _ in range(cfg.track_buffer + 2):  # empty updates age the lost track out
        ut.idle(img)
    assert not bt.tracked_stracks and not bt.lost_stracks
    ut.reset()
    assert bt.frame_id == 0


def test_ultralytics_idle_before_any_track_is_a_no_op():
    ut = UltralyticsTracker.__new__(UltralyticsTracker)
    ut.model = SimpleNamespace(predictor=None)
    ut.idle(frame())
    ut.reset()


def test_cli_track_check(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from parking.cli import app
    from parking.vision import tracking
    from tests.unit.test_motion import _repo

    args = _repo(tmp_path)
    args[0] = "track-check"
    runner = CliRunner()
    result = runner.invoke(app, args)
    assert result.exit_code == 1 and "not found" in result.output

    (tmp_path / "models" / "none").mkdir(parents=True)
    monkeypatch.setattr(tracking, "UltralyticsTracker", lambda *a, **k: FakeBackend())
    video = tmp_path / "out" / "debug.mp4"
    result = runner.invoke(app, [*args, "--debug-video", str(video), "--json"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output.strip().splitlines()[-2])
    assert out["frames"] == 300 and 0 < out["active_frames"] < 300
    assert [t["track_id"] for t in out["tracks"]] == [1]
    assert video.stat().st_size > 0

    result = runner.invoke(app, args)
    assert result.exit_code == 0 and "1 track(s)" in result.output and "#1" in result.output
