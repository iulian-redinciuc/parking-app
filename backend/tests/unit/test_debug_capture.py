"""Debug frame capture (workers/debug_capture.py)."""

import json
import os
import threading
import time
from datetime import UTC, datetime

import numpy as np

from parking.config import Settings
from parking.messages import Observation, SlotScore
from parking.workers.debug_capture import DebugCapture, capture_from_settings

IMAGE = np.full((40, 60, 3), 128, np.uint8)


def obs(ts: datetime, *taken: bool) -> Observation:
    return Observation(
        camera_id="cam-ground",
        ts=ts,
        frame_size=[60, 40],
        inference_ms=5,
        detections=0,
        slots=[SlotScore(id=f"G{i + 1:02d}", score=float(t), taken=t) for i, t in enumerate(taken)],
        zone_counts={},
    )


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, clock, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("every_s", 600)
    kw.setdefault("retention_s", 3600)
    return DebugCapture(tmp_path, "cam-ground", "Europe/Bucharest", clock=clock, **kw)


TS = datetime(2026, 10, 9, 21, 30, 5, 250000, tzinfo=UTC)  # 00:30 next day in Bucharest


def test_periodic_and_flip_captures(tmp_path):
    clock = Clock()
    cap = make(tmp_path, clock)
    first = cap.maybe_save(IMAGE, obs(TS, True, False))
    assert first is not None
    # lot-local date folder and time in the name
    assert first == tmp_path / "data/debug/cam-ground/2026-10-10/003005_250-periodic.jpg"
    meta = json.loads(first.with_suffix(".json").read_text())
    assert meta["reasons"] == ["periodic"] and meta["flipped"] == []
    assert meta["frame"] == first.name
    assert meta["observation"]["slots"][0] == {"id": "G01", "score": 1.0, "taken": True}

    clock.t += 5  # nothing changed, not due
    assert cap.maybe_save(IMAGE, obs(TS.replace(second=10), True, False)) is None
    clock.t += 5  # G02 flips
    flip = cap.maybe_save(IMAGE, obs(TS.replace(second=15), True, True))
    assert flip is not None and flip.name == "003015_250-flip.jpg"
    assert json.loads(flip.with_suffix(".json").read_text())["flipped"] == ["G02"]
    clock.t += 5  # a flip doesn't restart the periodic timer
    assert cap.maybe_save(IMAGE, obs(TS.replace(second=20), True, True)) is None
    clock.t = 1000.0 + 600  # due, and G01 flips at the same time
    both = cap.maybe_save(IMAGE, obs(TS.replace(minute=40), False, True))
    assert both is not None and both.name.endswith("-periodic+flip.jpg")
    assert cap.saved == 3
    assert len(list((tmp_path / "data/debug/cam-ground/2026-10-10").iterdir())) == 6


def test_new_slots_dont_count_as_flips(tmp_path):
    clock = Clock()
    cap = make(tmp_path, clock)
    cap.maybe_save(IMAGE, obs(TS, True))
    clock.t += 1
    assert cap.maybe_save(IMAGE, obs(TS, True, True)) is None  # G02 is new after a reload


def test_off_saves_nothing(tmp_path):
    cap = make(tmp_path, Clock(), enabled=False)
    assert cap.maybe_save(IMAGE, obs(TS, True)) is None
    assert not (tmp_path / "data").exists()
    assert cap.describe() == "debug capture off"


def test_write_errors_are_logged_not_raised(tmp_path, caplog):
    (tmp_path / "data").write_text("not a folder")
    cap = make(tmp_path, Clock())
    assert cap.maybe_save(IMAGE, obs(TS, True)) is None
    assert "debug capture failed" in caplog.text


def test_prune_deletes_old_files_and_empty_days(tmp_path):
    now = time.time()
    cap = make(tmp_path, Clock(), enabled=False, wall=lambda: now)  # prunes even when off
    assert cap.prune() == 0  # no folder yet
    old_day = tmp_path / "data/debug/cam-ground/2026-10-08"
    new_day = tmp_path / "data/debug/cam-ground/2026-10-09"
    for day in (old_day, new_day):
        day.mkdir(parents=True)
    for name, age in [("a.jpg", 7200), ("a.json", 7200), ("b.jpg", 1800), ("b.json", 1800)]:
        path = (old_day if name.startswith("a") else new_day) / name
        path.write_text("x")
        os.utime(path, (now - age, now - age))
    assert cap.prune() == 2
    assert not old_day.exists()
    assert sorted(p.name for p in new_day.iterdir()) == ["b.jpg", "b.json"]


def test_pruning_thread_runs_until_stopped(tmp_path):
    cap = make(tmp_path, Clock())
    path = cap.maybe_save(IMAGE, obs(TS, True))
    assert path is not None
    os.utime(path, (0, 0))
    stop = threading.Event()
    cap.start(stop)
    deadline = time.monotonic() + 5
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not path.exists() and path.with_suffix(".json").exists()
    stop.set()
    cap._thread.join(2)
    assert not cap._thread.is_alive()


def test_from_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG_CAPTURE", "true")
    monkeypatch.setenv("DEBUG_CAPTURE_EVERY_MIN", "2")
    monkeypatch.setenv("DEBUG_RETENTION_HOURS", "1")
    cap = capture_from_settings(Settings(_env_file=None), tmp_path, "cam-ground", "UTC")
    assert (cap.enabled, cap.every_s, cap.retention_s) == (True, 120, 3600)
    assert cap.describe() == (
        "debug capture on: every 2 min + slot flips → data/debug/cam-ground/, kept 1 h"
    )
