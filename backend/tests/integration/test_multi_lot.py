"""P9.5: several lots in one API (multi-lot.md): `lots:` in lot.yaml, `/api/lots/{lot}/...`,
worker payloads routed by camera, per-lot rollup totals and per-lot push subscriptions."""

import contextlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, select

from parking.api.app import create_app
from parking.config import ConfigError, Settings, load_config, load_lots, total_key
from parking.core.clock import FakeClock
from parking.db.engine import make_engine
from parking.db.models import PushSubscription, ZoneMinute
from parking.push.sender import generate_vapid_keys
from parking.workers.base import load_camera

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ADMIN = {"Authorization": "Bearer admin-token"}
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
T0 = datetime(2026, 10, 9, 14, 5, tzinfo=UTC)
PUBLIC, PRIVATE = generate_vapid_keys()
KEYS = {"p256dh": "BPk3", "auth": "c2Vj"}

LOTS_YAML = """
version: 1
lots:
  - id: main
    name: Main street
    location: {lat: 1.5, lon: 2.5}
    timezone: Europe/Bucharest
    zones:
      - {id: ground, name: {en: Ground, ro: Parter}, method: slots}
      - {id: underground, name: {en: Underground}, method: flow, capacity: 60}
    cameras:
      - id: cam-ground
        role: occupancy
        zones: [ground]
        source: "file:x.jpg"
        slots_file: config/slots/cam-ground.json
        detector: {model: models/x}
        smoothing: {consistent_readings: 1}
      - id: cam-ramp
        role: flow
        zones: [underground]
        source: "file:y.jpg"
        lines_file: config/lines/cam-ramp.json
        detector: {model: models/x}
  - id: north
    name: North gate
    location: {lat: 3.5, lon: 4.5}
    notify_radius_m: 300
    timezone: Europe/London
    zones:
      - {id: north-yard, name: {en: Yard}, method: slots}
    cameras:
      - id: cam-north
        role: occupancy
        zones: [north-yard]
        source: "file:z.jpg"
        slots_file: config/slots/cam-north.json
        detector: {model: models/x}
        smoothing: {consistent_readings: 1}
api: {sse_ping_s: 1}
"""

SLOTS = {"cam-ground": ("ground", ["G01", "G02", "G03"]), "cam-north": ("north-yard", ["N1", "N2"])}


def obs(camera, taken, **over):
    return {
        "v": 1,
        "camera_id": camera,
        "ts": "2026-10-09T14:05:00.000Z",
        "frame_size": [10, 10],
        "inference_ms": 5,
        "detections": 0,
        "slots": [{"id": s, "score": 0.5, "taken": s in taken} for s in SLOTS[camera][1]],
    } | over


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOTS_YAML)
    for camera, (zone, ids) in SLOTS.items():
        slots = [{"id": s, "zone": zone, "polygon": SQUARE} for s in ids]
        body = {"version": 1, "camera_id": camera, "image_size": [10, 10], "slots": slots}
        (tmp_path / "config" / "slots" / f"{camera}.json").write_text(json.dumps(body))
    return tmp_path


class FakeWebpush:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)

    def sent(self) -> list[tuple[str, dict]]:
        return [(c["subscription_info"]["endpoint"], json.loads(c["data"])) for c in self.calls]


def make_app(root, clock=None, webpush=None, **settings):
    settings = Settings(
        _env_file=None,
        worker_token=TOKEN,
        admin_token="admin-token",
        vapid_public_key=PUBLIC,
        vapid_private_key=PRIVATE,
        vapid_subject="mailto:test@example.test",
        **settings,
    )
    return create_app(
        root / "config" / "lot.yaml",
        settings,
        clock=clock or FakeClock(T0),
        db_url=f"sqlite:///{root}/parking.sqlite",
        tick=False,
        webpush=webpush or FakeWebpush(),
    )


@contextlib.asynccontextmanager
async def client_for(app):
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


def db_rows(root, model):
    engine = make_engine(f"sqlite:///{root}/parking.sqlite")
    with Session(engine) as session:
        result = list(session.exec(select(model)))
    engine.dispose()
    return result


# --- lot.yaml ---


def test_lots_file_loads_one_config_per_lot_with_the_shared_api(root):
    main, north = load_lots(root / "config" / "lot.yaml")
    assert (main.lot.id, north.lot.id) == ("main", "north")
    assert [z.id for z in north.zones] == ["north-yard"]
    assert [c.id for c in main.cameras] == ["cam-ground", "cam-ramp"]
    assert main.api.sse_ping_s == north.api.sse_ping_s == 1
    assert (north.lot.timezone, north.lot.notify_radius_m) == ("Europe/London", 300)
    # the default lot, unless a camera says which
    assert load_config(root / "config" / "lot.yaml").lot.id == "main"
    assert load_config(root / "config" / "lot.yaml", camera="cam-north").lot.id == "north"
    assert total_key([main, north], "main") == "total"
    assert total_key([main, north], "north") == "total@north"


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("id: north\n", "id: main\n", "duplicate lot id"),
        ("north-yard", "ground", "duplicate zone id"),
        ("id: cam-north\n", "id: cam-ramp\n", "duplicate camera id"),
        ("api: {sse_ping_s: 1}", "zones: []", "can't be combined with top-level zones"),
        ("notify_radius_m: 300", "notify_radius_m: 0", r"lots\[1\] \(north\)"),
        ("zones: [north-yard]", "zones: [ground]", r"(?s)lots\[1\] \(north\).*unknown zone"),
    ],
)
def test_lots_file_errors(root, old, new, match):
    path = root / "config" / "lot.yaml"
    assert old in LOTS_YAML
    path.write_text(LOTS_YAML.replace(old, new))
    with pytest.raises((ConfigError, ValueError), match=match):
        load_lots(path)


def test_worker_loads_its_own_lot(root):
    lot, camera = load_camera(root / "config" / "lot.yaml", "cam-north", "occupancy")
    assert (lot.lot.id, camera.id, [z.id for z in lot.zones]) == (
        "north",
        "cam-north",
        ["north-yard"],
    )


# --- public paths ---


async def test_each_lot_has_its_own_status_and_the_old_paths_are_the_default_lot(root):
    async with client_for(make_app(root)) as client:
        r = await client.get("/api/lots")
        assert r.json() == {
            "v": 1,
            "default": "main",
            "lots": [{"id": "main", "name": "Main street"}, {"id": "north", "name": "North gate"}],
        }
        assert (await client.get("/api/lots/north/status")).status_code == 503

        r = await client.post("/internal/observations", json=obs("cam-north", {"N1"}), headers=AUTH)
        assert r.status_code == 204
        north = (await client.get("/api/lots/north/status")).json()
        assert north["lot"] == "north" and [z["id"] for z in north["zones"]] == ["north-yard"]
        assert (north["total"]["capacity"], north["total"]["free"]) == (2, 1)
        # the other lot hasn't heard from its cameras
        assert (await client.get("/api/lots/main/status")).status_code == 503
        assert (await client.get("/api/status")).status_code == 503

        await client.post("/internal/observations", json=obs("cam-ground", set()), headers=AUTH)
        main = (await client.get("/api/status")).json()
        assert main == (await client.get("/api/lots/main/status")).json()
        assert main["lot"] == "main" and main["total"]["capacity"] == 63
        assert (await client.get("/api/lots/north/status")).json()["total"]["free"] == 1

        r = await client.get("/api/lots/nowhere/status")
        assert r.status_code == 404 and r.json()["error"]["message"] == "unknown lot 'nowhere'"
        # a zone belongs to its lot
        assert (await client.get("/api/lots/north/history?zone=ground")).status_code == 404
        assert (await client.get("/api/lots/main/history?zone=ground")).status_code == 200
        assert (await client.get("/api/lots/north/maps/ground")).status_code == 404


async def test_lot_info_per_lot(root):
    async with client_for(make_app(root, lot_lat=9.0, lot_lon=8.0)) as client:
        north = (await client.get("/api/lots/north")).json()
        assert (north["id"], north["name"], north["timezone"]) == (
            "north",
            "North gate",
            "Europe/London",
        )
        assert north["location"] == {"lat": 3.5, "lon": 4.5} and north["notify_radius_m"] == 300
        assert north["zones"] == [
            {"id": "north-yard", "name": "Yard", "method": "slots", "capacity": 2}
        ]
        # LOT_LAT / LOT_LON are the default lot's
        main = (await client.get("/api/lot?lang=ro")).json()
        assert main["id"] == "main" and main["location"] == {"lat": 9.0, "lon": 8.0}
        assert main["zones"][0]["name"] == "Parter"
        assert main == (await client.get("/api/lots/main?lang=ro")).json()


# --- workers ---


async def test_payload_naming_another_lot_is_rejected(root):
    async with client_for(make_app(root)) as client:
        ok = obs("cam-north", {"N1"}, lot="north")
        assert (
            await client.post("/internal/observations", json=ok, headers=AUTH)
        ).status_code == 204
        wrong = obs("cam-north", {"N1"}, lot="main")
        r = await client.post("/internal/observations", json=wrong, headers=AUTH)
        assert r.status_code == 422
        assert (
            r.json()["error"]["message"] == "camera 'cam-north' belongs to lot 'north', not 'main'"
        )
        health = {"v": 1, "lot": "north", "camera_id": "cam-ramp", "ts": "2026-10-09T14:05:00Z"}
        r = await client.post("/internal/health", json=health | {"state": "ok"}, headers=AUTH)
        assert r.status_code == 422
        event = {
            "v": 1,
            "event_id": "e1",
            "lot": "main",
            "camera_id": "cam-ramp",
            "ts": "2026-10-09T14:05:00Z",
            "direction": "in",
            "track_id": 1,
            "cls": "car",
            "confidence": 0.9,
        }
        r = await client.post("/internal/flow-events", json={"events": [event]}, headers=AUTH)
        assert r.json() == {"accepted": 1, "duplicates": 0}
        unknown = obs("cam-ground", set()) | {"camera_id": "cam-nowhere"}
        r = await client.post("/internal/observations", json=unknown, headers=AUTH)
        assert r.status_code == 422

        healthz = (await client.get("/healthz")).json()
        assert list(healthz["cameras"]) == ["cam-ground", "cam-ramp", "cam-north"]
        assert healthz["ingest"] == {
            "observations": 1,
            "flow_events": 1,
            "health": 0,
            "rejected": 3,
            "db_errors": 0,
        }


# --- admin: cameras and zones are found by id, whatever their lot ---


async def test_admin_routes_reach_every_lot(root):
    async with client_for(make_app(root)) as client:
        cameras = (await client.get("/api/admin/cameras", headers=ADMIN)).json()
        assert [(c["id"], c["lot"]) for c in cameras] == [
            ("cam-ground", "main"),
            ("cam-ramp", "main"),
            ("cam-north", "north"),
        ]
        r = await client.get("/api/admin/cameras/cam-north/slots", headers=ADMIN)
        assert r.status_code == 200 and [s["id"] for s in r.json()["slots"]] == ["N1", "N2"]
        # a slot file saved for the second lot's camera changes that lot's capacity
        slots = [{"id": s, "zone": "north-yard", "polygon": SQUARE} for s in ("N1", "N2", "N3")]
        body = {"version": 1, "camera_id": "cam-north", "image_size": [10, 10], "slots": slots}
        r = await client.put("/api/admin/cameras/cam-north/slots", json=body, headers=ADMIN)
        assert r.status_code == 200
        assert (await client.get("/api/lots/north")).json()["zones"][0]["capacity"] == 3
        r = await client.post(
            "/api/admin/zones/underground/correct", json={"occupied": 7}, headers=ADMIN
        )
        assert r.status_code == 200 and r.json()["occupied"] == 7
        r = await client.post(
            "/api/admin/zones/north-yard/correct", json={"occupied": 1}, headers=ADMIN
        )
        assert r.status_code == 409  # found in its lot: a measured zone can't be corrected


# --- rollups: one total per lot ---


async def test_rollups_keep_a_total_per_lot(root):
    clock = FakeClock(T0)
    app = make_app(root, clock)
    async with client_for(app) as client:
        await client.post("/internal/observations", json=obs("cam-ground", {"G01"}), headers=AUTH)
        await client.post("/internal/observations", json=obs("cam-north", {"N1"}), headers=AUTH)
        clock.advance(minutes=2)
        rt = app.state.runtime
        assert rt.lots["north"].jobs is rt.jobs
        rt.jobs.run_minutes(clock.now())
        free = {(r.zone_id, r.free_min) for r in db_rows(root, ZoneMinute)}
        assert free == {("ground", 2), ("total", 2), ("north-yard", 1), ("total@north", 1)}

        end = (T0 + timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
        query = f"bucket=minute&to={end}"
        north = (await client.get(f"/api/lots/north/history?{query}")).json()
        assert north["zone"] == "total" and {p["free_min"] for p in north["points"]} == {1}
        main = (await client.get(f"/api/history?{query}")).json()
        assert {p["free_min"] for p in main["points"]} == {2}


# --- push: a subscription follows one lot ---


def sub_body(endpoint, **over):
    return {"subscription": {"endpoint": endpoint, "keys": KEYS}, "tz": "UTC"} | over


async def test_subscriptions_follow_one_lot(root):
    webpush = FakeWebpush()
    app = make_app(root, webpush=webpush)
    a, b = "https://push.example.test/a", "https://push.example.test/b"
    async with client_for(app) as client:
        post = "/api/push/subscriptions"
        assert (await client.post(post, json=sub_body(a))).status_code == 201
        r = await client.post(post, json=sub_body(b, lot="north", prefs={"zones": ["north-yard"]}))
        assert r.status_code == 201
        assert {s.endpoint: s.lot_id for s in db_rows(root, PushSubscription)} == {
            a: "main",
            b: "north",
        }
        # zones are checked against the subscription's lot
        r = await client.post(post, json=sub_body(b, lot="north", prefs={"zones": ["ground"]}))
        assert r.status_code == 422 and "unknown zone(s): ground" in r.json()["error"]["message"]
        r = await client.post(post, json=sub_body(b, lot="nowhere"))
        assert r.status_code == 422 and r.json()["error"]["message"] == "lot: unknown lot 'nowhere'"
        r = await client.patch(post, json={"endpoint": b, "prefs": {"zones": ["ground"]}})
        assert r.status_code == 422

        await client.post("/internal/observations", json=obs("cam-ground", set()), headers=AUTH)
        await client.post("/internal/observations", json=obs("cam-north", {"N1"}), headers=AUTH)

        # the test push shows the subscription's own lot
        assert (await client.post("/api/push/test", json={"endpoint": b})).json()["sent"]
        assert (await client.post("/api/push/test", json={"endpoint": a})).json()["sent"]
        (to_b, north), (to_a, main) = webpush.sent()
        assert (to_b, to_a) == (b, a)
        assert "Yard" in north["body"] and "Ground" not in north["body"]
        assert "Ground" in main["body"] and "Yard" not in main["body"]

        # on the way to each: only the lot that changes pushes
        for endpoint in (a, b):
            r = await client.post("/api/push/on-my-way", json={"endpoint": endpoint, "minutes": 15})
            assert r.status_code == 202 and r.json()["sent"]
        webpush.calls.clear()
        full = obs("cam-north", {"N1", "N2"})
        await client.post("/internal/observations", json=full, headers=AUTH)
        rt = app.state.runtime
        for lot in rt.all:
            await lot.on_my_way.drain()
        assert [endpoint for endpoint, _ in webpush.sent()] == [b]

        # moving the subscription to the other lot ends its window and takes that lot's zones
        r = await client.patch(post, json={"endpoint": b, "lot": "main", "prefs": {}})
        assert r.status_code == 200 and r.json()["lot"] == "main"
        [moved] = [s for s in db_rows(root, PushSubscription) if s.endpoint == b]
        assert (moved.lot_id, moved.on_my_way_until) == ("main", None)
        r = await client.patch(post, json={"endpoint": b, "prefs": {"zones": ["ground"]}})
        assert r.status_code == 200 and r.json()["lot"] == "main"
