"""P7.3: the admin slot/line editor endpoints (api.md §4): read the files, validate and write
them with a `.bak`, reload the worker over its control server (api.md §5.2), update the API's
slot ids and capacities, and save the shift-detection reference frame."""

import contextlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from parking.api.app import create_app
from parking.api.routes.admin import format_config_json
from parking.config import Settings, load_lines, load_slots
from parking.core.clock import FakeClock
from parking.workers.control import ControlServer

ADMIN = "static-admin-token-0123456789abcdef"
WORKER = "worker-token-0123456789abcdef"
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

LOT_YAML = """
version: 1
lot: {{id: main, name: Parking, location: {{lat: 1.5, lon: 2.5}}, timezone: UTC}}
zones:
  - {{id: ground, name: {{en: Ground}}, method: slots, capacity: null}}
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
    control_url: {control_url}
    detector: {{model: models/x}}
"""


def square(x: int, y: int = 0, size: int = 10) -> list[list[int]]:
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


def slot_file(*ids: str, dx: int = 0) -> dict:
    return {
        "version": 1,
        "camera_id": "cam-ground",
        "image_size": [640, 360],
        "reference_image": "data/reference/cam-ground.jpg",
        "slots": [
            {"id": i, "zone": "ground", "polygon": square(20 * n + dx), "type": "standard"}
            for n, i in enumerate(ids)
        ],
        "count_zones": [],
    }


LINES = {
    "version": 1,
    "camera_id": "cam-ramp",
    "image_size": [640, 360],
    "line_a": [[100, 220], [560, 220]],
    "line_b": [[100, 270], [560, 270]],
    "in_direction": "a_to_b",
}


class Handler:
    def __init__(self):
        self.reloads = 0
        self.reload_error: str | None = None
        self.has_frame = True

    def snapshot(self, annotated):
        return None

    def reload(self):
        self.reloads += 1
        if self.reload_error:
            raise ValueError(self.reload_error)
        return {"camera_id": "cam-ground", "slots": 2, "interval_s": 5}

    def save_reference(self):
        if not self.has_frame:
            raise ValueError("no frame read yet")
        return {"saved": "data/reference/cam-ground.jpg", "ts": "2026-10-09T12:00:00Z"}


@pytest.fixture
def worker():
    handler = Handler()
    server = ControlServer(handler, WORKER, "127.0.0.1", 0).start()
    yield handler, f"http://127.0.0.1:{server.port}"
    server.stop()


def make_app(tmp_path, control_url, slots: dict | None = None):
    (tmp_path / "config" / "slots").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML.format(control_url=control_url))
    if slots is not None:
        (tmp_path / "config" / "slots" / "cam-ground.json").write_text(format_config_json(slots))
    return create_app(
        tmp_path / "config" / "lot.yaml",
        Settings(_env_file=None, admin_token=ADMIN, worker_token=WORKER),
        clock=FakeClock(T0),
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


def ground(app) -> dict:
    status = app.state.runtime.store.status()
    return next(z for z in status.zones if z.id == "ground").model_dump()


async def observe(c, **taken: bool) -> None:
    body = {
        "camera_id": "cam-ground",
        "ts": "2026-10-09T12:00:00Z",
        "frame_size": [640, 360],
        "inference_ms": 10,
        "detections": 0,
        "slots": [{"id": i, "score": 0.9 if t else 0.0, "taken": t} for i, t in taken.items()],
    }
    r = await c.post("/internal/observations", json=body, headers=WORKER_H)
    assert r.status_code == 204


async def test_editor_routes_need_admin(tmp_path, worker):
    async with client_for(make_app(tmp_path, worker[1], slot_file("G01"))) as c:
        assert (await c.get("/api/admin/cameras/cam-ground/slots")).status_code == 401
        r = await c.put("/api/admin/cameras/cam-ground/slots", json=slot_file("G01"))
        assert r.status_code == 401
        assert (await c.put("/api/admin/cameras/cam-ramp/lines", json=LINES)).status_code == 401
        r = await c.post("/api/admin/cameras/cam-ground/reference-frame")
        assert r.status_code == 401
    assert worker[0].reloads == 0


async def test_get_slots_returns_the_file(tmp_path, worker):
    async with client_for(make_app(tmp_path, worker[1], slot_file("G01", "G02"))) as c:
        r = await c.get("/api/admin/cameras/cam-ground/slots", headers=ADMIN_H)
        assert r.status_code == 200
        assert r.json() == slot_file("G01", "G02")
        r = await c.get("/api/admin/cameras/cam-nope/slots", headers=ADMIN_H)
        assert r.status_code == 404
        r = await c.get("/api/admin/cameras/cam-ramp/slots", headers=ADMIN_H)
        assert r.status_code == 404
        assert "flow camera" in r.json()["error"]["message"]
        r = await c.get("/api/admin/cameras/cam-ramp/lines", headers=ADMIN_H)
        assert r.status_code == 404
        assert "no lines file yet" in r.json()["error"]["message"]


async def test_put_slots_writes_backs_up_reloads_and_updates_capacity(tmp_path, worker):
    handler, url = worker
    path = tmp_path / "config" / "slots" / "cam-ground.json"
    app = make_app(tmp_path, url, slot_file("G01", "G02"))
    async with client_for(app) as c:
        await observe(c, G01=True, G02=True)
        assert ground(app)["capacity"] == 2
        assert ground(app)["occupied"] == 2

        # a nudged camera: the same spaces 7 px to the right, G02 removed, G03 added
        new = slot_file("G01", "G03", dx=7)
        r = await c.put("/api/admin/cameras/cam-ground/slots", json=new, headers=ADMIN_H)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "saved": "config/slots/cam-ground.json",
            "backup": True,
            "reloaded": True,
            "message": None,
            "slots": 2,
        }
        assert handler.reloads == 1
        assert path.read_text() == format_config_json(new)
        assert load_slots(path).ids() == ["G01", "G03"]
        assert json.loads(path.with_name("cam-ground.json.bak").read_text()) == slot_file(
            "G01", "G02"
        )
        # G02 is gone (no longer occupied), G03 is new: counted once the worker reports it
        assert ground(app)["capacity"] == 2
        assert ground(app)["occupied"] == 1
        await observe(c, G01=True, G03=True)
        assert ground(app)["occupied"] == 2
        assert ground(app)["slots"] == {"G01": True, "G03": True}

        # marking a space as accessible shows up in the status at once (P9.2)
        assert ground(app)["by_type"] == {}
        new["slots"][1]["type"] = "accessible"
        r = await c.put("/api/admin/cameras/cam-ground/slots", json=new, headers=ADMIN_H)
        assert r.status_code == 200, r.text
        assert ground(app)["by_type"] == {"accessible": {"capacity": 1, "free": 0}}

        r = await c.get("/api/admin/cameras/cam-ground/slots", headers=ADMIN_H)
        assert r.json() == new


async def test_put_slots_first_file_has_no_backup(tmp_path, worker):
    app = make_app(tmp_path, worker[1])
    async with client_for(app) as c:
        r = await c.put(
            "/api/admin/cameras/cam-ground/slots", json=slot_file("G01"), headers=ADMIN_H
        )
        assert r.status_code == 200
        assert r.json()["backup"] is False
        assert ground(app)["capacity"] == 1
    assert not (tmp_path / "config" / "slots" / "cam-ground.json.bak").exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"camera_id": "cam-other"}, "expected 'cam-ground'"),
        ({"slots": [{"id": "G01", "zone": "underground", "polygon": square(0)}]}, "zones"),
        ({"slots": [{"id": "G01", "zone": "ground", "polygon": [[0, 0], [5, 5]]}]}, "3 points"),
        (
            {
                "slots": [
                    {"id": "G01", "zone": "ground", "polygon": [[0, 0], [9, 9], [9, 0], [0, 9]]}
                ]
            },
            "self-intersecting",
        ),
        (
            {"slots": slot_file("G01")["slots"] * 2},
            "duplicate slot",
        ),
        ({"extra": 1}, "extra"),
        ({"version": 2}, "version"),
    ],
)
async def test_put_slots_rejects_invalid_files(tmp_path, worker, change, message):
    path = tmp_path / "config" / "slots" / "cam-ground.json"
    async with client_for(make_app(tmp_path, worker[1], slot_file("G01"))) as c:
        before = path.read_text()
        r = await c.put(
            "/api/admin/cameras/cam-ground/slots",
            json=slot_file("G01") | change,
            headers=ADMIN_H,
        )
        assert r.status_code == 422
        body = r.json()["error"]
        assert body["code"] == "bad_request"
        assert message in body["message"] + json.dumps(body.get("details"))
    assert path.read_text() == before
    assert not path.with_name("cam-ground.json.bak").exists()
    assert worker[0].reloads == 0


async def test_put_slots_flow_camera_is_404(tmp_path, worker):
    async with client_for(make_app(tmp_path, worker[1], slot_file("G01"))) as c:
        r = await c.put("/api/admin/cameras/cam-ramp/slots", json=slot_file("G01"), headers=ADMIN_H)
        assert r.status_code == 404


async def test_put_slots_saves_even_if_the_worker_cannot_reload(tmp_path, worker):
    handler, url = worker
    handler.reload_error = "slot file: bad zone"
    path = tmp_path / "config" / "slots" / "cam-ground.json"
    async with client_for(make_app(tmp_path, url, slot_file("G01"))) as c:
        r = await c.put(
            "/api/admin/cameras/cam-ground/slots", json=slot_file("G01", "G02"), headers=ADMIN_H
        )
        assert r.status_code == 200
        assert r.json()["reloaded"] is False
        assert r.json()["message"] == "slot file: bad zone"
    assert load_slots(path).ids() == ["G01", "G02"]


async def test_put_slots_worker_unreachable(tmp_path):
    app = make_app(tmp_path, "http://127.0.0.1:9", slot_file("G01"))
    async with client_for(app) as c:
        r = await c.put(
            "/api/admin/cameras/cam-ground/slots", json=slot_file("G01", "G02"), headers=ADMIN_H
        )
        assert r.status_code == 200
        assert r.json()["reloaded"] is False
        assert "didn't answer" in r.json()["message"]
        assert ground(app)["capacity"] == 2


async def test_put_and_get_lines(tmp_path, worker):
    handler, url = worker
    path = tmp_path / "config" / "lines" / "cam-ramp.json"
    async with client_for(make_app(tmp_path, url, slot_file("G01"))) as c:
        r = await c.put("/api/admin/cameras/cam-ramp/lines", json=LINES, headers=ADMIN_H)
        assert r.status_code == 200, r.text
        assert r.json() | {"reloaded": None} == {
            "saved": "config/lines/cam-ramp.json",
            "backup": False,
            "reloaded": None,
            "message": None,
        }
        assert load_lines(path).in_direction == "a_to_b"
        r = await c.get("/api/admin/cameras/cam-ramp/lines", headers=ADMIN_H)
        assert r.json() == LINES

        bad = LINES | {"line_a": [[100, 220]]}
        r = await c.put("/api/admin/cameras/cam-ramp/lines", json=bad, headers=ADMIN_H)
        assert r.status_code == 422
        r = await c.put("/api/admin/cameras/cam-ground/lines", json=LINES, headers=ADMIN_H)
        assert r.status_code == 404
    assert handler.reloads == 1


async def test_reference_frame(tmp_path, worker):
    handler, url = worker
    async with client_for(make_app(tmp_path, url, slot_file("G01"))) as c:
        r = await c.post("/api/admin/cameras/cam-ground/reference-frame", headers=ADMIN_H)
        assert r.status_code == 200
        assert r.json()["saved"] == "data/reference/cam-ground.jpg"

        handler.has_frame = False
        r = await c.post("/api/admin/cameras/cam-ground/reference-frame", headers=ADMIN_H)
        assert r.status_code == 409
        assert r.json()["error"]["message"] == "no frame read yet"

        r = await c.post("/api/admin/cameras/cam-nope/reference-frame", headers=ADMIN_H)
        assert r.status_code == 404


async def test_reference_frame_worker_unreachable(tmp_path):
    async with client_for(make_app(tmp_path, "http://127.0.0.1:9", slot_file("G01"))) as c:
        r = await c.post("/api/admin/cameras/cam-ground/reference-frame", headers=ADMIN_H)
        assert r.status_code == 503
