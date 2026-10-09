"""Motion gate (vision/motion.py) on generated scenes: 640×360 at 10 fps."""

import json
from datetime import UTC, datetime, timedelta

import cv2
import numpy as np
from typer.testing import CliRunner

from parking.cli import app
from parking.config import LineFile
from parking.vision.motion import HOLD_S, MotionGate, measure_motion
from parking.vision.sources import Frame

W, H, FPS = 640, 360, 10.0
MIN_AREA = 1500  # lot.yaml default, frame pixels
CAR = (90, 50)  # a car-sized blob at sub-stream resolution
runner = CliRunner()


def background(w: int = W, h: int = H) -> np.ndarray:
    rng = np.random.default_rng(0)
    img = np.tile(np.linspace(40, 160, w, dtype=np.uint8), (h, 1))
    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    for _ in range(60):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        cv2.rectangle(img, (x, y), (x + 20, y + 12), color, -1)
    return img


BG = background()


NOISE = [np.random.default_rng(s).normal(0, 2.5, (H, W, 3)).astype(np.float32) for s in range(7)]


def noisy(img: np.ndarray, i: int) -> np.ndarray:
    """Sensor noise, changing every frame."""
    if img.shape != NOISE[0].shape:
        return img
    return np.clip(img + NOISE[i % len(NOISE)], 0, 255).astype(np.uint8)


def with_car(img: np.ndarray, x: int, y: int = 220, size=CAR) -> np.ndarray:
    out = img.copy()
    cv2.rectangle(out, (x, y), (x + size[0], y + size[1]), (30, 30, 200), -1)
    return out


def run(gate: MotionGate, frames, start: int = 0):
    return [gate.update(f, (start + i) / FPS) for i, f in enumerate(frames)]


def warm(gate: MotionGate, n: int = 30, bg: np.ndarray = BG) -> None:
    run(gate, (noisy(bg, i) for i in range(n)))


def lines(**changes) -> LineFile:
    return LineFile.model_validate(
        {
            "version": 1,
            "camera_id": "cam-ramp",
            "image_size": [640, 360],
            "roi": [[60, 120], [600, 120], [600, 360], [60, 360]],
            "line_a": [[100, 220], [560, 220]],
            "line_b": [[100, 270], [560, 270]],
            **changes,
        }
    )


def test_static_scene_is_inactive():
    gate = MotionGate(MIN_AREA, lines())
    results = run(gate, (noisy(BG, i) for i in range(200)))
    assert not any(r.active for r in results)
    assert max(r.largest_area_px for r in results[1:]) < MIN_AREA


def test_moving_rectangle_is_active():
    gate = MotionGate(MIN_AREA, lines())
    warm(gate)
    results = run(gate, (noisy(with_car(BG, 80 + 15 * i), i) for i in range(30)), start=30)
    assert all(r.active and r.moving for r in results)
    assert max(r.largest_area_px for r in results) >= 0.8 * CAR[0] * CAR[1]


def test_hold_over_keeps_it_active_for_2_s():
    gate = MotionGate(MIN_AREA, lines())
    warm(gate)
    run(gate, (noisy(with_car(BG, 80 + 15 * i), i) for i in range(20)), start=30)
    last_motion = 49 / FPS
    # the car leaves (the ramp is empty again): no motion, but the gate holds for HOLD_S
    after = run(gate, (noisy(BG, 100 + i) for i in range(50)), start=50)
    assert not any(r.moving for r in after[1:])
    for i, r in enumerate(after):
        assert r.active == ((50 + i) / FPS - last_motion <= HOLD_S + 1e-9), i
    assert sum(r.active for r in after) == 20  # 2 s at 10 fps


def test_small_motion_and_motion_outside_the_roi_are_ignored():
    gate = MotionGate(MIN_AREA, lines())
    warm(gate)
    small = run(gate, (noisy(with_car(BG, 100 + 5 * i, size=(30, 30)), i) for i in range(20)), 30)
    assert not any(r.active for r in small)
    # a full car moving above the ROI (y < 120)
    above = run(gate, (noisy(with_car(BG, 80 + 15 * i, y=20), i) for i in range(20)), 50)
    assert not any(r.active for r in above)
    # without an ROI the same car counts
    gate = MotionGate(MIN_AREA)
    warm(gate)
    moving = run(gate, (with_car(BG, 80 + 15 * i, y=20) for i in range(20)), 30)
    assert all(r.active for r in moving)


def test_min_area_and_roi_scale_with_the_frame():
    big = cv2.resize(BG, (2 * W, 2 * H), interpolation=cv2.INTER_NEAREST)
    gate = MotionGate(MIN_AREA, lines())
    warm(gate, bg=big)
    # the threshold is in line-file pixels (640×360): 60×60 at 1280 px = 900 px there
    small = run(gate, (with_car(big, 200 + 10 * i, 460, (60, 60)) for i in range(20)), 30)
    assert not any(r.active for r in small)
    assert max(r.largest_area_px for r in small) < MIN_AREA
    car = run(gate, (with_car(big, 160 + 30 * i, 440, (180, 100)) for i in range(20)), 50)
    assert all(r.active for r in car)
    # a new frame size starts over (no false alarm from the switch)
    assert not gate.update(noisy(BG, 0), 10.0).active


class FakeSource:
    def __init__(self, frames):
        self.frames = list(frames)
        self.exhausted = False
        self.t0 = datetime(2026, 10, 10, 3, tzinfo=UTC)

    def read(self):
        if not self.frames:
            self.exhausted = True
            return None
        i, img = self.frames.pop(0)
        return Frame(img, self.t0 + timedelta(seconds=i / FPS), seq=i)

    def close(self):
        pass


def clip(n: int = 600, cars=((100, 130),)):
    """`n` frames of an empty ramp with a car crossing during each (start, end) frame range."""
    for i in range(n):
        img = BG
        for a, b in cars:
            if a <= i < b:
                img = with_car(BG, 20 + (600 * (i - a)) // (b - a))
        yield i, noisy(img, i)


def test_measure_motion_counts_active_frames_and_bursts():
    frames = list(clip(cars=((100, 130), (400, 430))))
    frames.insert(50, frames[49])  # a repeated frame (same seq) is skipped
    stats = measure_motion(FakeSource(frames), MotionGate(MIN_AREA, lines()), 3600)
    assert stats.frames == 600 and stats.bursts == 2
    # each car: ~3 s in the ROI + 2 s hold-over
    assert 80 <= stats.active_frames <= 110
    assert abs(stats.seconds - 59.9) < 1e-6
    assert stats.to_dict()["ratio"] == round(stats.active_frames / 600, 4)

    short = measure_motion(FakeSource(clip()), MotionGate(MIN_AREA, lines()), 20)
    assert short.frames == 200


def _repo(tmp_path, with_lines: bool = True):
    (tmp_path / "config" / "lines").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(
        """
version: 1
lot: {id: main, name: P, location: {lat: 0, lon: 0}, timezone: UTC}
zones:
  - {id: underground, name: {en: U}, method: flow, capacity: 10}
cameras:
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: video:ramp.avi?realtime=false
    lines_file: config/lines/cam-ramp.json
    detector: {model: models/none}
"""
    )
    if with_lines:
        (tmp_path / "config/lines/cam-ramp.json").write_text(lines().model_dump_json())
    w = cv2.VideoWriter(str(tmp_path / "ramp.avi"), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    assert w.isOpened()
    for _, img in clip(300, cars=((100, 130),)):
        w.write(img)
    w.release()
    return ["motion-check", "--camera", "cam-ramp", "--config", str(tmp_path / "config/lot.yaml")]


def test_cli_motion_check(tmp_path):
    args = _repo(tmp_path)
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output.strip().splitlines()[-2])
    assert out["frames"] == 300 and out["bursts"] == 1 and out["ok"]
    assert 0.1 < out["ratio"] < 0.2
    assert "only 30 of 3600 s" in result.output

    result = runner.invoke(app, [*args, "--max-ratio", "0.1"])
    assert result.exit_code == 1 and "too often" in result.output


def test_cli_motion_check_without_line_file(tmp_path):
    args = _repo(tmp_path, with_lines=False)
    result = runner.invoke(app, [*args, "--seconds", "30"])
    assert result.exit_code == 0, result.output
    assert "whole frame" in result.output and "1 burst(s)" in result.output
