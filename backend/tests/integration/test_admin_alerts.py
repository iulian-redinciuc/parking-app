"""P7.8: admin alerts inside the API (notifications.md §5.1). Camera health, observations and
flow events go in through `/internal/*`, the checks are run by hand (`AdminAlertMonitor.check()`)
with a `FakeClock`; `webpush` is faked."""

import base64
import contextlib
import os
from datetime import UTC, datetime

import httpx
import pytest

from parking.api.app import create_app
from parking.config import Settings
from parking.core.clock import FakeClock
from parking.messages import format_ts
from parking.push.sender import generate_vapid_keys
from tests.integration.test_on_my_way import (
    AUTH,
    PRIVATE,
    PUBLIC,
    TOKEN,
    FakeWebpush,
    Feed,
    write_lot,
)

ADMIN_TOKEN = "admin-test-token"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
ADMIN_ENDPOINT = "https://push.example.test/send/admin"
USER_ENDPOINT = "https://push.example.test/send/user"
MINE = {"endpoint": ADMIN_ENDPOINT}
# Friday 9 Oct 2026, 14:05 UTC = 17:05 in Bucharest (EEST, +3)
T0 = datetime(2026, 10, 9, 14, 5, tzinfo=UTC)


@pytest.fixture
def lot(tmp_path):
    return write_lot(tmp_path)


@contextlib.asynccontextmanager
async def running(lot, webpush, clock, vapid=True):
    keys = {"vapid_public_key": PUBLIC, "vapid_private_key": PRIVATE} if vapid else {}
    settings = Settings(
        _env_file=None,
        worker_token=TOKEN,
        admin_token=ADMIN_TOKEN,
        vapid_subject="mailto:test@example.test",
        **keys,
    )
    app = create_app(
        lot / "config" / "lot.yaml",
        settings,
        clock=clock,
        db_url=f"sqlite:///{lot}/parking.sqlite",
        tick=False,
        webpush=webpush,
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client, app.state.runtime


async def subscribe(client, endpoint, admin=False):
    p256dh = generate_vapid_keys()[0]
    auth = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
    body = {
        "subscription": {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
        "prefs": {},
        "tz": "Europe/Bucharest",
        "lang": "en",
    }
    r = await client.post("/api/push/subscriptions", json=body)
    assert r.status_code == 201, r.text
    if admin:
        r = await client.put(
            "/api/admin/alerts", json={"endpoint": endpoint, "enabled": True}, headers=ADMIN
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"enabled": True}


async def health(client, clock, state="ok", issue=None, camera="cam-ground"):
    body = {"v": 1, "camera_id": camera, "ts": format_ts(clock.now()), "state": state}
    body["issue"] = issue
    r = await client.post("/internal/health", json=body, headers=AUTH)
    assert r.status_code == 204, r.text


async def flow(client, clock, *ids, direction="out"):
    events = [
        {
            "v": 1,
            "event_id": i,
            "camera_id": "cam-ramp",
            "ts": format_ts(clock.now()),
            "direction": direction,
            "track_id": 1,
            "cls": "car",
            "confidence": 0.8,
        }
        for i in ids
    ]
    r = await client.post("/internal/flow-events", json={"events": events}, headers=AUTH)
    assert r.status_code == 200, r.text


async def checks(rt, clock, seconds, every=10):
    """The monitor's 10 s checks over `seconds`."""
    for _ in range(seconds // every):
        clock.advance(seconds=every)
        await rt.alerts.check()


def admin_pushes(webpush):
    return [
        (c["subscription_info"]["endpoint"], p)
        for c, p in zip(webpush.calls, webpush.payloads(), strict=True)
        if p["kind"] == "admin_alert"
    ]


async def test_camera_down_alerts_after_two_minutes_then_resolves(lot):
    """The Done-when: a camera unplugged -> one alert (~2 min after its `down`), plugged back
    in -> a resolved push; only the admin subscription gets them."""
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        await subscribe(client, ADMIN_ENDPOINT, admin=True)
        await subscribe(client, USER_ENDPOINT)
        await health(client, clock)
        await health(client, clock, "down", "connect_failed")
        await checks(rt, clock, 110)
        assert admin_pushes(webpush) == []
        await checks(rt, clock, 20)  # 2 min since the first check that saw it
        [(endpoint, alert)] = admin_pushes(webpush)
        assert endpoint == ADMIN_ENDPOINT
        assert alert["title"] == "Admin: Camera cam-ground is down"
        assert alert["body"] == "Since 17:05 (connect failed)"
        assert alert["tag"] == "admin-camera_down:cam-ground"
        assert alert["url"] == "#/admin/cameras/cam-ground"
        assert alert["resolved"] is False
        assert webpush.calls[-1]["headers"]["Urgency"] == "high"
        # GET lists the open issue
        r = await client.get(
            "/api/admin/alerts", params={"endpoint": ADMIN_ENDPOINT}, headers=ADMIN
        )
        body = r.json()
        assert body["enabled"] is True and body["available"] is True
        [issue] = body["issues"]
        assert issue["key"] == "camera_down:cam-ground"
        assert issue["active"] is True and issue["detail"] == "connect_failed"
        # still down: nothing more within the hour, one repeat after it
        await checks(rt, clock, 3000, every=60)
        assert len(admin_pushes(webpush)) == 1
        await checks(rt, clock, 660, every=60)
        assert len(admin_pushes(webpush)) == 2
        await health(client, clock)
        await rt.alerts.check()
        *_, (_, resolved) = admin_pushes(webpush)
        assert resolved["title"] == "Admin: Camera cam-ground is back up"
        assert resolved["body"] == "Resolved: 17:05–18:08"
        assert resolved["tag"] == alert["tag"] and resolved["resolved"] is True
        assert webpush.calls[-1]["headers"]["Urgency"] == "normal"
        await checks(rt, clock, 300)
        assert len(admin_pushes(webpush)) == 3


async def test_short_outage_sends_nothing(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        await subscribe(client, ADMIN_ENDPOINT, admin=True)
        await health(client, clock, "down", "connect_failed")
        await checks(rt, clock, 90)
        await health(client, clock)
        await checks(rt, clock, 300)
        assert admin_pushes(webpush) == []


async def test_shift_alerts_at_once_and_flapping_waits_an_hour(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        await subscribe(client, ADMIN_ENDPOINT, admin=True)
        await health(client, clock, "degraded", "shifted")
        await rt.alerts.check()
        [(_, alert)] = admin_pushes(webpush)
        assert alert["title"] == "Admin: Camera cam-ground has shifted"
        await health(client, clock)
        await rt.alerts.check()  # resolved
        await health(client, clock, "degraded", "shifted")
        await checks(rt, clock, 600)  # back within the hour: held back
        assert [p["resolved"] for _, p in admin_pushes(webpush)] == [False, True]
        await health(client, clock)
        await rt.alerts.check()  # no alert went out for this episode: no resolved either
        assert len(admin_pushes(webpush)) == 2
        await health(client, clock, "degraded", "shifted")
        await checks(rt, clock, 3000, every=60)  # an hour after the first alert
        assert [p["resolved"] for _, p in admin_pushes(webpush)] == [False, True, False]


async def test_stale_data_alerts_after_five_minutes(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        await subscribe(client, ADMIN_ENDPOINT, admin=True)
        await Feed(client, rt, clock).free(12)
        clock.advance(seconds=61)  # stale_after_s 60
        await rt.ingestor.tick()
        await checks(rt, clock, 300)  # first seen stale at the first of these checks
        assert admin_pushes(webpush) == []
        await checks(rt, clock, 10)
        [(_, alert)] = admin_pushes(webpush)
        assert alert["title"] == "Admin: Ground: no fresh data"
        assert alert["url"] == "#/admin"
        await Feed(client, rt, clock).free(11)
        await rt.alerts.check()
        assert admin_pushes(webpush)[-1][1]["title"] == "Admin: Ground: live data again"


async def test_stale_zone_of_a_down_camera_is_only_the_camera_alert(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        await subscribe(client, ADMIN_ENDPOINT, admin=True)
        await Feed(client, rt, clock).free(12)
        await health(client, clock, "down", "connect_failed")
        await checks(rt, clock, 600, every=30)
        keys = [p["issue"] for _, p in admin_pushes(webpush)]
        assert keys == ["camera_down:cam-ground"]


async def test_clamps_alert_on_the_fourth_and_resolve_after_a_correction(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        await subscribe(client, ADMIN_ENDPOINT, admin=True)
        r = await client.post(
            "/api/admin/zones/underground/correct", json={"occupied": 0}, headers=ADMIN
        )
        assert r.status_code == 200
        clock.advance(seconds=1)
        await flow(client, clock, "o1", "o2", "o3")  # OUT at 0: clamped
        await rt.alerts.check()
        assert admin_pushes(webpush) == []
        await flow(client, clock, "o4")
        await rt.alerts.check()
        [(_, alert)] = admin_pushes(webpush)
        assert alert["title"] == "Admin: Underground: entry/exit count is off"
        assert alert["body"] == "4 clamped events today. Correct the count"
        clock.advance(seconds=1)
        r = await client.post(
            "/api/admin/zones/underground/correct", json={"occupied": 5}, headers=ADMIN
        )
        await rt.alerts.check()
        assert admin_pushes(webpush)[-1][1]["title"] == (
            "Admin: Underground: entry/exit count fixed"
        )


async def test_alerts_routes(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        body = {"endpoint": ADMIN_ENDPOINT, "enabled": True}
        assert (await client.put("/api/admin/alerts", json=body)).status_code == 401
        assert (await client.get("/api/admin/alerts")).status_code == 401
        r = await client.put("/api/admin/alerts", json=body, headers=ADMIN)
        assert r.status_code == 404
        r = await client.put("/api/admin/alerts", json={**body, "x": 1}, headers=ADMIN)
        assert r.status_code == 422
        r = await client.get("/api/admin/alerts", headers=ADMIN)
        assert r.json() == {"enabled": False, "available": True, "issues": []}
        await subscribe(client, ADMIN_ENDPOINT, admin=True)
        # re-subscribing the same browser (new keys) keeps the flag
        await subscribe(client, ADMIN_ENDPOINT)
        r = await client.get(
            "/api/admin/alerts", params={"endpoint": ADMIN_ENDPOINT}, headers=ADMIN
        )
        assert r.json()["enabled"] is True
        r = await client.put("/api/admin/alerts", json={**body, "enabled": False}, headers=ADMIN)
        assert r.json() == {"enabled": False}
        await health(client, clock, "degraded", "shifted")
        await rt.alerts.check()
        assert admin_pushes(webpush) == []  # nobody opted in


async def test_without_push_the_routes_still_work(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock, vapid=False) as (client, rt):
        assert rt.alerts is None
        r = await client.get("/api/admin/alerts", headers=ADMIN)
        assert r.json() == {"enabled": False, "available": False, "issues": []}
