"""P7.2: camera health list and snapshots (api.md §4): the latest health per camera, and the
JPEG proxied from a real worker control server (api.md §5.2) with its errors."""

import contextlib
import threading
from datetime import UTC, datetime

import httpx
import pytest

from parking.api.app import create_app
from parking.api.routes import admin
from parking.config import Settings
from parking.core.clock import FakeClock
from parking.workers.control import ControlServer

ADMIN = "static-admin-token-0123456789abcdef"
WORKER = "worker-token-0123456789abcdef"
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"

LOT_YAML = """
version: 1
lot: {{id: main, name: Parking, location: {{lat: 1.5, lon: 2.5}}, timezone: UTC}}
zones:
  - {{id: ground, name: {{en: Ground}}, method: count, capacity: 20}}
  - {{id: underground, name: {{en: Underground}}, method: flow, capacity: 60}}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: "file:x.jpg"
    slots_file: config/slots/cam-ground.json
    control_url: {control_url}
    detector: {{model: models/x}}
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "file:y.jpg"
    lines_file: config/lines/cam-ramp.json
    detector: {{model: models/x}}
"""


class Handler:
    def __init__(self):
        self.jpeg: bytes | None = JPEG
        self.calls: list[bool] = []
        self.release = threading.Event()
        self.hang = False

    def snapshot(self, annotated):
        self.calls.append(annotated)
        if self.hang:
            self.release.wait(5)
        return self.jpeg

    def reload(self):
        return {}

    def save_reference(self):
        return {}


@pytest.fixture
def worker():
    handler = Handler()
    server = ControlServer(handler, WORKER, "127.0.0.1", 0).start()
    yield handler, f"http://127.0.0.1:{server.port}"
    handler.release.set()
    server.stop()


@pytest.fixture
def clock():
    return FakeClock(T0)


def make_app(tmp_path, clock, control_url, **settings):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML.format(control_url=control_url))
    values = {"admin_token": ADMIN, "worker_token": WORKER} | settings
    return create_app(
        tmp_path / "config" / "lot.yaml",
        Settings(_env_file=None, **values),
        clock=clock,
        db_url=f"sqlite:///{tmp_path}/parking.sqlite",
        tick=False,
    )


@contextlib.asynccontextmanager
async def client_for(app):
    transport = httpx.ASGITransport(app=app, client=("1.2.3.4", 123))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        yield c


ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}
WORKER_H = {"Authorization": f"Bearer {WORKER}"}


async def test_camera_list_needs_admin(tmp_path, clock, worker):
    async with client_for(make_app(tmp_path, clock, worker[1])) as c:
        r = await c.get("/api/admin/cameras")
        assert r.status_code == 401
        r = await c.get("/api/admin/cameras/cam-ground/snapshot")
        assert r.status_code == 401
    assert worker[0].calls == []


async def test_camera_list_shows_latest_health_and_ages(tmp_path, clock, worker):
    async with client_for(make_app(tmp_path, clock, worker[1])) as c:
        r = await c.get("/api/admin/cameras", headers=ADMIN_H)
        assert r.status_code == 200
        ground, ramp = r.json()
        assert ground == {
            "id": "cam-ground",
            "role": "occupancy",
            "zones": ["ground"],
            "state": "unknown",
            "issue": None,
            "fps": None,
            "last_frame_age_s": None,
            "inference_ms_avg": None,
            "unhealthy_ratio": None,
            "last_health_age_s": None,
            "snapshot": True,
        }
        assert ramp["snapshot"] is False

        health = {
            "camera_id": "cam-ground",
            "ts": "2026-10-09T12:00:00Z",
            "state": "degraded",
            "issue": "blurry",
            "fps": 0.2,
            "last_frame_age_s": 3.1,
            "inference_ms_avg": 151.26,
            "unhealthy_ratio": 0.4,
        }
        assert (await c.post("/internal/health", json=health, headers=WORKER_H)).status_code == 204
        clock.advance(4)
        ground = (await c.get("/api/admin/cameras", headers=ADMIN_H)).json()[0]
        assert ground["state"] == "degraded"
        assert ground["issue"] == "blurry"
        assert ground["fps"] == 0.2
        assert ground["last_frame_age_s"] == 7.1  # 3.1 when sent + 4 s since
        assert ground["inference_ms_avg"] == 151.3
        assert ground["unhealthy_ratio"] == 0.4
        assert ground["last_health_age_s"] == 4.0


async def test_snapshot_is_the_workers_jpeg_never_cached(tmp_path, clock, worker):
    handler, url = worker
    async with client_for(make_app(tmp_path, clock, url)) as c:
        r = await c.get("/api/admin/cameras/cam-ground/snapshot", headers=ADMIN_H)
        assert r.status_code == 200
        assert r.content == JPEG
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"] == "no-store"
        r = await c.get("/api/admin/cameras/cam-ground/snapshot?annotated=false", headers=ADMIN_H)
        assert r.status_code == 200
    assert handler.calls == [True, False]


async def test_snapshot_errors(tmp_path, clock, worker):
    handler, url = worker
    async with client_for(make_app(tmp_path, clock, url)) as c:
        r = await c.get("/api/admin/cameras/nope/snapshot", headers=ADMIN_H)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

        r = await c.get("/api/admin/cameras/cam-ramp/snapshot", headers=ADMIN_H)
        assert r.status_code == 503
        assert "control_url" in r.json()["error"]["message"]

        handler.jpeg = None  # the worker has no frame yet -> its 503
        r = await c.get("/api/admin/cameras/cam-ground/snapshot", headers=ADMIN_H)
        assert r.status_code == 503
        assert r.json()["error"]["message"] == "the worker has no frame yet"

        handler.jpeg = b"not a jpeg"
        r = await c.get("/api/admin/cameras/cam-ground/snapshot", headers=ADMIN_H)
        assert r.status_code == 503


async def test_snapshot_wrong_worker_token_or_none(tmp_path, clock, worker):
    _, url = worker
    async with client_for(make_app(tmp_path, clock, url, worker_token="other-token")) as c:
        r = await c.get("/api/admin/cameras/cam-ground/snapshot", headers=ADMIN_H)
        assert r.status_code == 503
        assert "HTTP 401" in r.json()["error"]["message"]
    async with client_for(make_app(tmp_path, clock, url, worker_token=None)) as c:
        r = await c.get("/api/admin/cameras/cam-ground/snapshot", headers=ADMIN_H)
        assert r.status_code == 503
        assert "WORKER_TOKEN" in r.json()["error"]["message"]


async def test_snapshot_worker_down_or_too_slow(tmp_path, clock, worker, monkeypatch):
    handler, url = worker
    monkeypatch.setattr(admin, "WORKER_TIMEOUT_S", 0.3)
    handler.hang = True
    async with client_for(make_app(tmp_path, clock, url)) as c:
        r = await c.get("/api/admin/cameras/cam-ground/snapshot", headers=ADMIN_H)
        assert r.status_code == 503
        assert "didn't answer" in r.json()["error"]["message"]
    handler.release.set()
    # nothing listens on port 9 (discard) on the test machine -> connect error
    async with client_for(make_app(tmp_path, clock, "http://127.0.0.1:9")) as c:
        r = await c.get("/api/admin/cameras/cam-ground/snapshot", headers=ADMIN_H)
        assert r.status_code == 503
