"""P6.7: reminders and almost-full alerts inside the API (notifications.md §5). The minutely job
is driven by hand (`PushScheduler.tick()`) with a `FakeClock`; `webpush` is faked."""

import base64
import os
from datetime import UTC, datetime, timedelta

import pytest

from parking.core.clock import FakeClock
from parking.db.models import NotificationLog
from parking.push.sender import generate_vapid_keys
from tests.integration.test_on_my_way import ENDPOINT, FakeWebpush, Feed, db, running, write_lot

# Friday 9 Oct 2026, 14:05 UTC = 17:05 in Bucharest (EEST, +3)
T0 = datetime(2026, 10, 9, 14, 5, tzinfo=UTC)
FRIDAY = 5


@pytest.fixture
def lot(tmp_path):
    return write_lot(tmp_path)


async def subscribe(client, endpoint=ENDPOINT, **prefs):
    p256dh = generate_vapid_keys()[0]
    auth = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
    body = {
        "subscription": {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
        "prefs": prefs,
        "tz": "Europe/Bucharest",
        "lang": "en",
    }
    r = await client.post("/api/push/subscriptions", json=body)
    assert r.status_code == 201, r.text


async def minutes(rt, clock, n):
    """`n` runs of the minutely job, one per minute."""
    for _ in range(n):
        clock.advance(minutes=1)
        await rt.scheduler.tick()


async def test_schedule_two_minutes_ahead_fires_once(lot):
    """The Done-when: a reminder set 2 minutes ahead fires once."""
    webpush, clock = FakeWebpush(), FakeClock(T0.replace(second=0))
    async with running(lot, webpush, clock) as (client, rt):
        await Feed(client, rt, clock).free(12)
        await subscribe(client, schedules=[{"days": [FRIDAY], "time": "17:07"}])
        await minutes(rt, clock, 1)
        assert webpush.calls == []
        await minutes(rt, clock, 1)  # 14:07 UTC = 17:07 local
        assert len(webpush.calls) == 1
        await minutes(rt, clock, 10)
        assert len(webpush.calls) == 1
        [payload] = webpush.payloads()
        assert payload["kind"] == "schedule"
        assert payload["title"] == "Parking: 72 free"  # 12 ground + 60 underground
        assert payload["body"].startswith("Ground 12 · Underground ≈60 · ")
        assert webpush.calls[0]["headers"] == {"Urgency": "normal"}
        assert [(r.kind, r.status) for r in db(lot, NotificationLog)] == [("schedule", "sent")]


async def test_schedule_in_quiet_hours_is_skipped(lot):
    webpush, clock = FakeWebpush(), FakeClock(datetime(2026, 10, 9, 19, 58, tzinfo=UTC))
    async with running(lot, webpush, clock) as (client, rt):
        await Feed(client, rt, clock).free(12)
        quiet = {"from": "22:00", "to": "07:00"}
        await subscribe(client, schedules=[{"days": [FRIDAY], "time": "23:00"}], quiet_hours=quiet)
        await minutes(rt, clock, 70)  # through 23:00 local
        assert webpush.calls == []
        logs = db(lot, NotificationLog)
        assert [(r.kind, r.status) for r in logs] == [("schedule", "skipped_quiet")]


async def test_late_job_catches_up_and_a_recent_push_suppresses(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0.replace(second=0))
    async with running(lot, webpush, clock) as (client, rt):
        await Feed(client, rt, clock).free(12)
        await subscribe(client, schedules=[{"days": [FRIDAY], "time": "17:07"}])
        await rt.scheduler.tick()  # 14:05
        clock.advance(minutes=4)  # the job missed 14:06 – 14:08
        await rt.scheduler.tick()
        assert len(webpush.calls) == 1
        # a test push at 17:09 suppresses a 17:15 reminder (10 min gap)
        r = await client.post("/api/push/test", json={"endpoint": ENDPOINT})
        assert r.status_code == 200, r.text
        r = await client.patch(
            "/api/push/subscriptions",
            json={"endpoint": ENDPOINT, "prefs": {"schedules": [{"days": [5], "time": "17:15"}]}},
        )
        assert r.status_code == 200, r.text
        await minutes(rt, clock, 10)
        assert [p["kind"] for p in webpush.payloads()] == ["schedule", "test"]


async def test_almost_full_alerts_with_cooldown_and_quiet_hours(lot):
    webpush, clock = FakeWebpush(), FakeClock(T0)
    async with running(lot, webpush, clock) as (client, rt):
        feed = Feed(client, rt, clock)
        await feed.free(10)
        await subscribe(client, alert_when_almost_full=True, zones=["ground"])
        quiet_all_day = {"from": "00:00", "to": "23:59"}
        await subscribe(
            client, ENDPOINT + "/quiet", alert_when_almost_full=True, quiet_hours=quiet_all_day
        )
        await subscribe(client, ENDPOINT + "/off")  # pref off

        async def at(minutes, free):
            clock.set(T0 + timedelta(minutes=minutes))
            await feed.free(free)
            return len(webpush.calls)

        assert await at(1, 5) == 0  # plenty → filling
        assert await at(2, 0) == 1  # ground full → alert
        assert await at(3, 3) == 1  # better
        assert await at(60, 0) == 1  # full again within 2 h → cooldown
        assert await at(121, 3) == 1
        assert await at(123, 0) == 2  # 2 h later → alert again
        assert all(p["kind"] == "almost_full" for p in webpush.payloads())
        assert all(c["headers"] == {"Urgency": "high"} for c in webpush.calls)
        assert {c["subscription_info"]["endpoint"] for c in webpush.calls} == {ENDPOINT}
        logs = [(r.kind, r.status) for r in db(lot, NotificationLog)]
        assert logs.count(("almost_full", "sent")) == 2
        assert logs.count(("almost_full", "skipped_quiet")) == 3  # the quiet one, every time


async def test_scheduler_runs_in_the_lifespan(lot):
    async with running(lot, FakeWebpush(), FakeClock(T0), tick=True) as (_client, rt):
        assert rt.scheduler._scheduler.running
        [job] = rt.scheduler._scheduler.get_jobs()
        assert str(job.trigger) == "cron[second='0']"
    assert rt.scheduler._scheduler is None
    async with running(lot, FakeWebpush(), FakeClock(T0), vapid=False) as (_client, rt):
        assert rt.scheduler is None and rt.almost_full is None
