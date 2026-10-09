"""P6.6: "I'm on my way" end to end inside the API: start a 15 min window, change the feed
through `/internal/observations`, and check which pushes go out (notifications.md §4).
`webpush` is faked, the clock is a `FakeClock`."""

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
from parking.messages import format_ts
from parking.push.sender import generate_vapid_keys

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLOTS = [f"G{i:02d}" for i in range(1, 21)]  # 20 spaces: 10 % = 2, so the free step is 3
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
T0 = datetime(2026, 10, 9, 14, 5, tzinfo=UTC)
ENDPOINT = "https://push.example.test/send/omw"
PUBLIC, PRIVATE = generate_vapid_keys()

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
"""


class FakeWebpush:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)

    def payloads(self) -> list[dict]:
        return [json.loads(c["data"]) for c in self.calls]


@pytest.fixture
def lot(tmp_path):
    return write_lot(tmp_path)


def write_lot(tmp_path):
    """lot.yaml + a 20-space slot file under `tmp_path/config` (also used by P6.7's tests)."""
    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    slots = [{"id": s, "zone": "ground", "polygon": SQUARE} for s in SLOTS]
    slot_file = {"version": 1, "camera_id": "cam-ground", "image_size": [10, 10], "slots": slots}
    (tmp_path / "config" / "slots" / "cam-ground.json").write_text(json.dumps(slot_file))
    return tmp_path


@contextlib.asynccontextmanager
async def running(lot, webpush, clock, vapid=True, tick=False):
    keys = {"vapid_public_key": PUBLIC, "vapid_private_key": PRIVATE} if vapid else {}
    settings = Settings(
        _env_file=None,
        worker_token=TOKEN,
        vapid_subject="mailto:test@example.test",
        **keys,
    )
    app = create_app(
        lot / "config" / "lot.yaml",
        settings,
        clock=clock,
        db_url=f"sqlite:///{lot}/parking.sqlite",
        tick=tick,
        webpush=webpush,
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client, app.state.runtime


class Feed:
    """Sets how many ground spaces are free, like the replay feed does, and waits for the
    resulting push dispatches."""

    def __init__(self, client, rt, clock):
        self.client, self.rt, self.clock = client, rt, clock

    async def free(self, n: int) -> None:
        taken = set(SLOTS[: len(SLOTS) - n])
        body = {
            "v": 1,
            "camera_id": "cam-ground",
            "ts": format_ts(self.clock.now()),
            "frame_size": [10, 10],
            "inference_ms": 5,
            "detections": len(taken),
            "slots": [{"id": s, "score": 0.5, "taken": s in taken} for s in SLOTS],
        }
        r = await self.client.post("/internal/observations", json=body, headers=AUTH)
        assert r.status_code == 204, r.text
        for notifier in (self.rt.on_my_way, self.rt.almost_full):
            if notifier is not None:
                await notifier.drain()


async def subscribe(client, zones=("ground",)):
    p256dh = generate_vapid_keys()[0]
    auth = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
    body = {
        "subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": p256dh, "auth": auth}},
        "prefs": {"zones": list(zones) if zones else None},
        "tz": "Europe/Bucharest",
        "lang": "ro",
    }
    r = await client.post("/api/push/subscriptions", json=body)
    assert r.status_code == 201


async def on_my_way(client, minutes):
    r = await client.post("/api/push/on-my-way", json={"endpoint": ENDPOINT, "minutes": minutes})
    assert r.status_code == 202, r.text
    return r.json()


def db(lot, model):
    engine = make_engine(f"sqlite:///{lot}/parking.sqlite")
    with Session(engine) as session:
        result = list(session.exec(select(model)))
    engine.dispose()
    return result


async def test_fifteen_minute_window_follows_the_rules(lot):
    """The Done-when: pushes arrive per the rules and stop after 15 min."""
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        feed = Feed(client, rt, clock)
        await feed.free(18)
        await subscribe(client)
        assert (await on_my_way(client, 15))["sent"] is True  # push 1: 18 free, plenty
        [sub] = db(lot, PushSubscription)
        assert (sub.last_sent_free, sub.last_sent_level, sub.on_my_way_sent) == (18, "plenty", 1)

        async def at(minutes, free):
            clock.set(T0 + timedelta(minutes=minutes))
            await feed.free(free)
            return len(webpush.calls)

        assert await at(1, 16) == 1  # Δ2 < 3, still plenty
        assert await at(3, 14) == 2  # Δ4 ≥ 3 since the last push (not since the last status)
        assert await at(3.5, 3) == 2  # filling, but only 30 s since the last push
        assert await at(5.5, 2) == 3  # filling vs plenty last sent, 2 min passed
        assert await at(6, 0) == 4  # became full: ignores the 2 min gap
        assert await at(10, 1) == 5  # full -> filling (1/20 = 5 %)
        assert await at(15, 15) == 5  # window over (until = 14:20, exclusive)
        assert await at(20, 3) == 5

        bodies = [p["body"] for p in webpush.payloads()]
        assert bodies[0].startswith("Parter 18 · ") and bodies[1].startswith("Parter 14 · ")
        assert all(p["title"].startswith("Parking: ") for p in webpush.payloads())  # whole lot
        assert all(p["kind"] == "on_my_way" for p in webpush.payloads())
        assert all(c["headers"] == {"Urgency": "high"} for c in webpush.calls)
        [sub] = db(lot, PushSubscription)
        assert (sub.last_sent_free, sub.last_sent_level, sub.on_my_way_sent) == (
            1, "filling", 5
        )  # fmt: skip
        logs = db(lot, NotificationLog)
        assert [(r.kind, r.status) for r in logs] == [("on_my_way", "sent")] * 5


async def test_six_pushes_per_window_and_restart_resets(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        feed = Feed(client, rt, clock)
        await feed.free(20)
        await subscribe(client)
        await on_my_way(client, 60)
        for i, free in enumerate([16, 20, 16, 20, 16, 20, 16], start=1):
            clock.advance(minutes=3)
            await feed.free(free)
            assert len(webpush.calls) == min(i + 1, 6)
        # a new "on my way" opens a new window: count starts over
        await on_my_way(client, 30)
        assert len(webpush.calls) == 7
        clock.advance(minutes=3)
        await feed.free(20)
        assert len(webpush.calls) == 8


async def test_cancel_stops_pushes(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        feed = Feed(client, rt, clock)
        await feed.free(18)
        await subscribe(client)
        await on_my_way(client, 30)
        assert await on_my_way(client, 0) == {"until": None, "sent": False}
        clock.advance(minutes=5)
        await feed.free(0)
        assert len(webpush.calls) == 1


async def test_no_data_at_start_then_first_data_pushes(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        feed = Feed(client, rt, clock)
        await subscribe(client)
        await on_my_way(client, 15)
        assert webpush.payloads()[0]["body"] == "No data yet"
        clock.advance(minutes=2)
        await feed.free(18)  # the start-up status -> first data
        assert len(webpush.calls) == 2
        assert webpush.payloads()[1]["body"].startswith("Parter 18 · ")


async def test_without_push_configured_ingest_still_works(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock, vapid=False) as (client, rt):
        assert rt.on_my_way is None
        await Feed(client, rt, clock).free(5)
        r = await client.get("/api/status")
        assert r.status_code == 200 and r.json()["zones"][0]["free"] == 5
