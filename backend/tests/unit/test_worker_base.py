"""Worker scheduling and the control server (workers/base.py, workers/control.py)."""

import json

import httpx
import pytest

from parking.workers.base import Schedule
from parking.workers.control import ControlServer


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds
        return False  # not stopping


def test_schedule_first_run_is_immediate_then_on_a_grid():
    clock = FakeClock()
    s = Schedule(5, clock.sleep, clock)
    assert s.wait()
    clock.now += 1.5  # the frame took 1.5 s
    assert s.wait()
    assert clock.sleeps == [3.5]  # next start = previous start + interval, no drift
    assert clock.now == 105


def test_schedule_slow_frame_runs_late_without_waiting():
    clock = FakeClock()
    s = Schedule(5, clock.sleep, clock)
    s.wait()
    clock.now += 7  # 2 s late: run at once, the grid stays at 100, 105, 110
    assert s.wait()
    assert clock.sleeps == []
    clock.now += 1  # now 108
    s.wait()
    assert clock.sleeps == [2.0]  # waits for 110
    assert s.skipped == 0


def test_schedule_skips_runs_more_than_one_interval_late():
    clock = FakeClock()
    s = Schedule(5, clock.sleep, clock)
    s.wait()
    clock.now += 17  # now 117: 105 and 110 are > 5 s late, 115 is 2 s late
    assert s.wait()
    assert s.skipped == 2
    assert clock.sleeps == []
    s.wait()
    assert clock.sleeps == [3.0]  # 120


def test_schedule_stops_when_sleep_reports_stop():
    clock = FakeClock()
    s = Schedule(5, lambda _s: True, clock)
    assert s.wait()  # first run doesn't sleep
    assert not s.wait()


def test_schedule_rejects_bad_interval():
    with pytest.raises(ValueError):
        Schedule(0, lambda _s: False)


class Handler:
    def __init__(self):
        self.jpeg = None
        self.annotated = []

    def snapshot(self, annotated):
        self.annotated.append(annotated)
        return self.jpeg

    def reload(self):
        return {"slots": 3}

    def save_reference(self):
        raise ValueError("no frame read yet")


@pytest.fixture
def control():
    handler = Handler()
    server = ControlServer(handler, "secret", "127.0.0.1", 0).start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{server.port}", headers={"Authorization": "Bearer secret"}
    )
    yield handler, client
    client.close()
    server.stop()


def test_control_needs_the_token(control):
    _, client = control
    for headers in ({"Authorization": "Bearer wrong"}, {"Authorization": ""}):
        r = client.get("/control/snapshot", headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"


def test_control_snapshot(control):
    handler, client = control
    r = client.get("/control/snapshot")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "unavailable"
    handler.jpeg = b"\xff\xd8jpeg"
    r = client.get("/control/snapshot?annotated=true")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8jpeg"
    assert handler.annotated == [False, True]


def test_control_reload_and_save_reference(control):
    _, client = control
    r = client.post("/control/reload")
    assert (r.status_code, r.json()) == (200, {"slots": 3})
    r = client.post("/control/save-reference")
    assert r.status_code == 409
    assert json.loads(r.text)["error"] == {"code": "conflict", "message": "no frame read yet"}


def test_control_unknown_route_and_method(control):
    _, client = control
    assert client.get("/control/nope").status_code == 404
    assert client.get("/control/reload").status_code == 404  # reload is POST


def test_control_handler_crash_is_500(control):
    handler, client = control
    handler.snapshot = lambda annotated: 1 / 0
    r = client.get("/control/snapshot")
    assert r.status_code == 500
    assert "ZeroDivision" not in r.text


def test_control_refuses_to_start_without_token():
    with pytest.raises(ValueError):
        ControlServer(Handler(), "", "127.0.0.1", 0)
