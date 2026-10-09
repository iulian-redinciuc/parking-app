"""P6.2: the Web Push routes (api.md §2): VAPID key, subscription upsert/patch/delete, Prefs
validation, "on my way", the test push and the push rate limits. `webpush` is faked."""

# ruff: noqa: E501 (the validation table reads best one case per line)

import base64
import contextlib
import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, select

from parking.api.app import create_app
from parking.config import Settings
from parking.core.clock import FakeClock
from parking.db.engine import make_engine
from parking.db.models import NotificationLog, PushSubscription
from parking.push.payload import status_payload
from parking.push.sender import generate_vapid_keys


def browser_keys() -> tuple[str, str]:
    """A subscription's `p256dh` (a P-256 public point) and `auth` (16 random bytes)."""
    return generate_vapid_keys()[0], base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()


TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLOTS = ["G01", "G02", "G03"]
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]

LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 1.5, lon: 2.5}, timezone: Europe/Bucharest}
zones:
  - {id: ground, name: {en: Ground, ro: Parter}, method: slots}
  - {id: underground, name: {en: Underground, ro: Subsol}, method: flow, capacity: 60}
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
api: {sse_ping_s: 1}
"""


def obs(taken):
    return {
        "v": 1,
        "camera_id": "cam-ground",
        "ts": "2026-10-09T08:00:00.000Z",
        "frame_size": [10, 10],
        "inference_ms": 5,
        "detections": 0,
        "slots": [{"id": s, "score": 0.5, "taken": s in taken} for s in SLOTS],
    }


@contextlib.asynccontextmanager
async def client_for(app, **kwargs):
    transport = httpx.ASGITransport(app=app, **kwargs)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


T0 = datetime(2026, 10, 9, 14, 5, tzinfo=UTC)
ENDPOINT = "https://push.example.test/send/abc123"
KEYS = dict(zip(("p256dh", "auth"), browser_keys(), strict=True))  # fresh per run, like a browser
PUBLIC, PRIVATE = generate_vapid_keys()


@pytest.fixture
def lot(tmp_path):
    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    slots = [{"id": s, "zone": "ground", "polygon": SQUARE} for s in SLOTS]
    slot_file = {"version": 1, "camera_id": "cam-ground", "image_size": [10, 10], "slots": slots}
    (tmp_path / "config" / "slots" / "cam-ground.json").write_text(json.dumps(slot_file))
    return tmp_path


class FakeWebpush:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)


def make_app(lot, webpush=None, vapid=True):
    keys = {"vapid_public_key": PUBLIC, "vapid_private_key": PRIVATE} if vapid else {}
    settings = Settings(
        _env_file=None,
        worker_token=TOKEN,
        vapid_subject="mailto:test@example.test",
        public_app_url="https://app.example.test/parking-app/",
        **keys,
    )
    return create_app(
        lot / "config" / "lot.yaml",
        settings,
        clock=FakeClock(T0),
        db_url=f"sqlite:///{lot}/parking.sqlite",
        tick=False,
        webpush=webpush or FakeWebpush(),
    )


def body(**over):
    return {
        "subscription": {"endpoint": ENDPOINT, "expirationTime": None, "keys": KEYS},
        "prefs": {
            "proximity": True,
            "radius_m": 500,
            "zones": ["ground", "underground"],
            "schedules": [{"days": [5, 1, 2, 3, 4], "time": "08:30"}],
            "quiet_hours": {"from": "22:00", "to": "07:00"},
            "alert_when_almost_full": True,
        },
        "tz": "Europe/Bucharest",
        "lang": "en",
    } | over


def rows(lot) -> list[PushSubscription]:
    engine = make_engine(f"sqlite:///{lot}/parking.sqlite")
    with Session(engine) as session:
        result = list(session.exec(select(PushSubscription)))
    engine.dispose()
    return result


def log_rows(lot) -> list[NotificationLog]:
    engine = make_engine(f"sqlite:///{lot}/parking.sqlite")
    with Session(engine) as session:
        result = list(session.exec(select(NotificationLog)))
    engine.dispose()
    return result


async def delete(client, endpoint):
    return await client.request("DELETE", "/api/push/subscriptions", json={"endpoint": endpoint})


async def test_vapid_public_key(lot):
    async with client_for(make_app(lot)) as client:
        r = await client.get("/api/push/vapid-public-key")
        assert r.status_code == 200 and r.json() == {"key": PUBLIC}
    async with client_for(make_app(lot, vapid=False)) as client:
        r = await client.get("/api/push/vapid-public-key")
        assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"


async def test_subscribe_upserts_by_endpoint(lot):
    async with client_for(make_app(lot)) as client:
        r = await client.post("/api/push/subscriptions", json=body())
        assert r.status_code == 201
        sub_id = r.json()["id"]
        [row] = rows(lot)
        assert (row.id, row.endpoint, row.p256dh, row.auth) == (
            sub_id, ENDPOINT, KEYS["p256dh"], KEYS["auth"]
        )  # fmt: skip
        assert row.prefs["schedules"] == [{"days": [1, 2, 3, 4, 5], "time": "08:30"}]
        assert row.prefs["quiet_hours"] == {"from": "22:00", "to": "07:00"}
        assert (row.tz, row.lang, row.failures) == ("Europe/Bucharest", "en", 0)

        # same endpoint again (e.g. new keys, other prefs): same row, same id
        again = body(prefs={"zones": ["ground"]}, tz="UTC", lang="ro")
        again["subscription"]["keys"] = {"p256dh": "AAAA", "auth": "BBBB"}
        r = await client.post("/api/push/subscriptions", json=again)
        assert r.status_code == 201 and r.json()["id"] == sub_id
        [row] = rows(lot)
        assert (row.p256dh, row.tz, row.lang) == ("AAAA", "UTC", "ro")
        assert row.prefs == {
            "proximity": False,
            "radius_m": None,
            "zones": ["ground"],
            "schedules": [],
            "quiet_hours": None,
            "alert_when_almost_full": False,
        }

        other = body()
        other["subscription"]["endpoint"] = ENDPOINT + "-other"
        r = await client.post("/api/push/subscriptions", json=other)
        assert r.status_code == 201 and r.json()["id"] != sub_id
        assert len(rows(lot)) == 2


@pytest.mark.parametrize(
    ("over", "where"),
    [
        ({"prefs": {"radius_m": 99}}, "body.prefs.radius_m"),
        ({"prefs": {"radius_m": 5001}}, "body.prefs.radius_m"),
        ({"prefs": {"schedules": [{"days": [1], "time": "24:00"}]}}, "body.prefs.schedules.0.time"),
        ({"prefs": {"schedules": [{"days": [1], "time": "8:30"}]}}, "body.prefs.schedules.0.time"),
        ({"prefs": {"schedules": [{"days": [0], "time": "08:30"}]}}, "body.prefs.schedules.0.days.0"),
        ({"prefs": {"schedules": [{"days": [], "time": "08:30"}]}}, "body.prefs.schedules.0.days"),
        ({"prefs": {"quiet_hours": {"from": "22:00", "to": "7"}}}, "body.prefs.quiet_hours.to"),
        ({"prefs": {"zones": ["roof"]}}, "body.prefs.zones"),
        ({"prefs": {"radius": 500}}, "body.prefs.radius"),  # typo: unknown field
        ({"tz": "Mars/Olympus"}, "body.tz"),
        ({"tz": "../etc/passwd"}, "body.tz"),
        ({"lang": "e"}, "body.lang"),
        ({"subscription": {"endpoint": "http://insecure.test/x", "keys": KEYS}}, "body.subscription.endpoint"),
        ({"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": "x y", "auth": "a"}}}, "body.subscription.keys.p256dh"),
        ({"subscription": {"endpoint": ENDPOINT}}, "body.subscription.keys"),
    ],
)  # fmt: skip
async def test_subscribe_validation_errors(lot, over, where):
    async with client_for(make_app(lot)) as client:
        r = await client.post("/api/push/subscriptions", json=body(**over))
        assert r.status_code == 422, r.text
        error = r.json()["error"]
        assert error["code"] == "bad_request"
        assert where in {".".join(map(str, d["loc"])) for d in error["details"]}
        assert rows(lot) == []


async def test_patch_replaces_prefs_and_validates(lot):
    async with client_for(make_app(lot)) as client:
        sub_id = (await client.post("/api/push/subscriptions", json=body())).json()["id"]
        prefs = {"zones": ["underground"], "radius_m": 1000}
        r = await client.patch(
            "/api/push/subscriptions", json={"endpoint": ENDPOINT, "prefs": prefs}
        )
        assert r.status_code == 200
        assert r.json()["id"] == sub_id and r.json()["prefs"]["zones"] == ["underground"]
        [row] = rows(lot)
        assert (row.prefs["radius_m"], row.prefs["proximity"], row.prefs["schedules"]) == (
            1000, False, []
        )  # fmt: skip

        bad = {"endpoint": ENDPOINT, "prefs": {"radius_m": 50}}
        r = await client.patch("/api/push/subscriptions", json=bad)
        assert r.status_code == 422
        unknown = {"endpoint": ENDPOINT + "-nope", "prefs": {}}
        r = await client.patch("/api/push/subscriptions", json=unknown)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_delete_is_idempotent(lot):
    async with client_for(make_app(lot)) as client:
        await client.post("/api/push/subscriptions", json=body())
        r = await delete(client, ENDPOINT)
        assert r.status_code == 204 and r.content == b""
        assert rows(lot) == []
        assert (await delete(client, ENDPOINT)).status_code == 204
        assert (await delete(client, "not a url")).status_code == 422


async def test_test_push_sends_status_and_is_limited_per_endpoint(lot):
    webpush = FakeWebpush()
    async with client_for(make_app(lot, webpush)) as client:
        await client.post("/internal/observations", json=obs({"G01"}), headers=AUTH)
        await client.post("/api/push/subscriptions", json=body())
        for _ in range(3):
            r = await client.post("/api/push/test", json={"endpoint": ENDPOINT})
            assert r.status_code == 200 and r.json() == {"sent": True, "deleted": False}
        r = await client.post("/api/push/test", json={"endpoint": ENDPOINT})
        assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
        assert 1 <= int(r.headers["retry-after"]) <= 3600

        # another endpoint (from the same IP) has its own 3/hour
        other = body()
        other["subscription"]["endpoint"] = ENDPOINT + "-2"
        await client.post("/api/push/subscriptions", json=other)
        r = await client.post("/api/push/test", json={"endpoint": ENDPOINT + "-2"})
        assert r.status_code == 200

        r = await client.post("/api/push/test", json={"endpoint": ENDPOINT + "-nope"})
        assert r.status_code == 404

    assert len(webpush.calls) == 4
    call = webpush.calls[0]
    assert call["subscription_info"] == {"endpoint": ENDPOINT, "keys": KEYS}
    assert call["headers"] == {"Urgency": "normal"} and call["ttl"] == 600
    logs = log_rows(lot)
    assert [(log.kind, log.status) for log in logs] == [("test", "sent")] * 4
    assert logs[0].payload == {
        "title": "Parking: 62 free",
        "body": "Ground 2 · Underground ≈60 · 17:05",  # Europe/Bucharest = UTC+3 in October
        "tag": "parking-status",
        "url": "https://app.example.test/parking-app/#/",
        "level": "plenty",
        "kind": "test",
    }


async def test_push_posts_share_20_per_hour_per_ip(lot):
    async with client_for(make_app(lot)) as client:
        for i in range(20):
            other = body()
            other["subscription"]["endpoint"] = f"{ENDPOINT}-{i}"
            assert (await client.post("/api/push/subscriptions", json=other)).status_code == 201
        r = await client.post("/api/push/on-my-way", json={"endpoint": ENDPOINT, "minutes": 30})
        assert r.status_code == 429
        # updating preferences doesn't use the push budget
        patch = {"endpoint": f"{ENDPOINT}-0", "prefs": {}}
        assert (await client.patch("/api/push/subscriptions", json=patch)).status_code == 200


async def test_on_my_way_sets_window_and_pushes_now(lot):
    webpush = FakeWebpush()
    async with client_for(make_app(lot, webpush)) as client:
        await client.post("/api/push/subscriptions", json=body(prefs={"zones": ["ground"]}))
        r = await client.post("/api/push/on-my-way", json={"endpoint": ENDPOINT, "minutes": 30})
        assert r.status_code == 202
        assert r.json() == {"until": "2026-10-09T14:35:00.000Z", "sent": True}
        [row] = rows(lot)
        assert row.on_my_way_until.replace(tzinfo=UTC) == T0 + timedelta(minutes=30)
        assert webpush.calls[0]["headers"] == {"Urgency": "high"}
        # no data yet since start-up
        assert log_rows(lot)[0].payload["body"] == "No data yet"

        r = await client.post("/api/push/on-my-way", json={"endpoint": ENDPOINT, "minutes": 0})
        assert r.status_code == 202 and r.json() == {"until": None, "sent": False}
        assert rows(lot)[0].on_my_way_until is None
        assert len(webpush.calls) == 1

        for minutes in (4, 121, -5):
            r = await client.post(
                "/api/push/on-my-way", json={"endpoint": ENDPOINT, "minutes": minutes}
            )
            assert r.status_code == 422
        r = await client.post(
            "/api/push/on-my-way", json={"endpoint": ENDPOINT + "x", "minutes": 5}
        )
        assert r.status_code == 404


async def test_sending_routes_503_without_vapid(lot):
    async with client_for(make_app(lot, vapid=False)) as client:
        assert (await client.post("/api/push/subscriptions", json=body())).status_code == 201
        for path, extra in (("/api/push/test", {}), ("/api/push/on-my-way", {"minutes": 15})):
            r = await client.post(path, json={"endpoint": ENDPOINT, **extra})
            assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"
        assert rows(lot)[0].on_my_way_until is None


def test_payload_without_zone_filter_and_unknown_tz():
    from parking.messages import LotStatus

    status = LotStatus.model_validate(
        {
            "lot": "main",
            "updated_at": "2026-10-09T14:05:00Z",
            "total": {"capacity": 10, "occupied": 10, "free": 0, "level": "full",
                      "confidence": 1, "stale": False},
            "zones": [{"id": "ground", "name": "Ground", "method": "slots", "capacity": 10,
                       "occupied": 10, "free": 0, "level": "full", "confidence": 1,
                       "stale": False, "trend": "steady"}],
        }
    )  # fmt: skip
    p = status_payload(status, "almost_full", "#/", tz="Nowhere/Gone", zones=["roof"])
    assert (p["title"], p["body"], p["level"]) == ("Parking: 0 free", "Ground 0 · 14:05", "full")
