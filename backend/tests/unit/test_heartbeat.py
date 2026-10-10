"""Loop heartbeat file + its check, the workers' Docker healthcheck (workers/heartbeat.py, P8.5)."""

import os
import signal
import subprocess
import sys
import time

import pytest

from parking.config import Settings
from parking.workers import base, heartbeat
from parking.workers.heartbeat import LoopHeartbeat, check, max_age_s


def test_max_age_is_three_intervals_with_a_floor():
    assert max_age_s(0) == max_age_s(5) == 20
    assert max_age_s(60) == 180


def test_beat_writes_the_limit_and_check_reads_the_age(tmp_path):
    path = tmp_path / "heartbeat"
    assert check(path) == (False, f"{path}: no heartbeat yet")
    LoopHeartbeat(path).beat(30)
    assert path.read_text() == "90\n"
    assert not (tmp_path / "heartbeat.tmp").exists()
    beat_at = path.stat().st_mtime
    assert check(path, now=beat_at + 89)[0]
    alive, reason = check(path, now=beat_at + 91)
    assert not alive and reason == "loop stuck: last heartbeat 91 s ago (limit 90 s)"


def test_beat_writes_at_most_once_a_second(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat"
    now = [100.0]
    monkeypatch.setattr(heartbeat.time, "monotonic", lambda: now[0])
    hb = LoopHeartbeat(path)
    hb.beat(5)
    path.unlink()
    now[0] += 0.5
    hb.beat(5)
    assert not path.exists()
    now[0] += 0.6
    hb.beat(5)
    assert path.read_text() == "20\n"


def test_off_without_a_path_and_a_broken_file_is_not_alive(tmp_path):
    LoopHeartbeat(None).beat(5)  # CLI runs: nothing written
    path = tmp_path / "heartbeat"
    path.write_text("soon")
    assert not check(path)[0]


def test_healthcheck_command_exit_codes(tmp_path):
    path = tmp_path / "heartbeat"
    cmd = [sys.executable, "-m", "parking.workers.heartbeat", str(path)]
    assert subprocess.run(cmd, capture_output=True).returncode == 1
    LoopHeartbeat(path).beat()
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0 and "s ago (limit 20 s)" in r.stdout
    old = time.time() - 21
    os.utime(path, (old, old))
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 1 and r.stdout.startswith("loop stuck")


class _Worker(base.Worker):
    """The lifecycle without a camera."""

    def __init__(self, settings):
        self.settings = settings
        self.loop_heartbeat = LoopHeartbeat(settings.heartbeat_file)

    camera_id = "cam-x"


def test_worker_alive_touches_the_settings_file(tmp_path, caplog):
    path = tmp_path / "heartbeat"
    w = _Worker(Settings(_env_file=None, heartbeat_file=path))
    w.alive(5)
    assert check(path)[0]
    assert Settings(_env_file=None).heartbeat_file is None

    gone = _Worker(Settings(_env_file=None, heartbeat_file=tmp_path / "missing" / "heartbeat"))
    gone.alive()  # logged, never raised into the loop
    assert "heartbeat file not written" in caplog.text


def test_sigusr1_freezes_the_loop_thread(monkeypatch, caplog):
    w = _Worker(Settings(_env_file=None))
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(base.time, "sleep", nap)
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1)}
    try:
        w.install_signal_handlers()
        with caplog.at_level("WARNING"), pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGUSR1)
            for _ in range(100):  # the handler runs on this (the main) thread and never returns
                time.sleep(0)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    assert naps == [3600, 3600, 3600]
    assert "cam-x: SIGUSR1: loop frozen" in caplog.text
