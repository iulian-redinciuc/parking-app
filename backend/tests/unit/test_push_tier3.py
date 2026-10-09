"""P6.7: the Tier 3 rules (notifications.md §5) with a fake clock: quiet hours across midnight,
reminders in several time zones and over both DST changes, almost-full transitions."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from parking.core.clock import FakeClock
from parking.core.fusion import level_for
from parking.db.models import PushSubscription
from parking.messages import LotStatus, Totals, ZoneStatus
from parking.push.rules import (
    almost_full_zones,
    in_quiet_hours,
    schedule_instants,
    should_send_almost_full,
    should_send_schedule,
)

BUCHAREST = "Europe/Bucharest"
FRI = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)  # Friday 08:00 in Bucharest (EEST, +3)
QUIET = {"from": "22:00", "to": "07:00"}


CAP = {"ground": 40, "underground": 60}


def status(ground: int, underground: int = 30) -> LotStatus:
    zones = [
        ZoneStatus(
            id=zid,
            name=zid.title(),
            method="slots" if zid == "ground" else "flow",
            capacity=CAP[zid],
            occupied=CAP[zid] - free,
            free=free,
            level=level_for(free, CAP[zid]),
            confidence=0.9,
            stale=False,
            trend="steady",
            updated_at=FRI,
        )
        for zid, free in (("ground", ground), ("underground", underground))
    ]
    free = ground + underground
    total = Totals(
        capacity=100, occupied=100 - free, free=free, level=level_for(free, 100),
        confidence=0.9, stale=False,
    )  # fmt: skip
    return LotStatus(lot="main", updated_at=FRI, total=total, zones=zones)


def sub(tz=BUCHAREST, schedules=(), quiet=None, almost_full=False, zones=None, last_sent_at=None):
    return PushSubscription(
        id="s1",
        endpoint="https://push.example.test/x",
        p256dh="k",
        auth="a",
        tz=tz,
        prefs={
            "schedules": list(schedules),
            "quiet_hours": quiet,
            "alert_when_almost_full": almost_full,
            "zones": zones,
        },
        last_sent_at=last_sent_at,
    )


def minutely(s: PushSubscription, start: datetime, hours: float) -> list[datetime]:
    """What a job running at second 0 of every minute finds due over `hours`."""
    clock, found = FakeClock(start), []
    end = start + timedelta(hours=hours)
    while clock.now() < end:
        before = clock.now()
        found += schedule_instants(s, before, clock.advance(minutes=1))
    return found


def local(ts: datetime, tz: str) -> str:
    return ts.astimezone(ZoneInfo(tz)).strftime("%a %H:%M %Z")


# --- quiet hours ---


@pytest.mark.parametrize(
    ("hhmm", "quiet"),
    [
        ("21:59", False),
        ("22:00", True),
        ("23:59", True),
        ("00:00", True),
        ("03:00", True),
        ("06:59", True),
        ("07:00", False),
        ("12:00", False),
    ],
)
def test_quiet_hours_span_midnight(hhmm, quiet):
    h, m = map(int, hhmm.split(":"))
    # Bucharest is +3 in October: local hh:mm = UTC hh-3:mm
    now = datetime(2026, 10, 9, tzinfo=UTC) + timedelta(hours=h - 3, minutes=m)
    assert in_quiet_hours(sub(quiet=QUIET), now) is quiet


def test_quiet_hours_same_day_and_none():
    day = {"from": "12:00", "to": "14:00"}
    at = datetime(2026, 10, 9, 10, 30, tzinfo=UTC)  # 13:30 in Bucharest
    assert in_quiet_hours(sub(quiet=day), at)
    assert not in_quiet_hours(sub(quiet=day), at + timedelta(hours=1))  # 14:30
    assert not in_quiet_hours(sub(quiet={"from": "08:00", "to": "08:00"}), at)  # empty
    assert not in_quiet_hours(sub(), at)


def test_quiet_hours_follow_the_subscription_tz():
    at = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)  # 06:00 Bucharest, 23:00 New York (Thu)
    assert in_quiet_hours(sub(quiet=QUIET), at)
    assert in_quiet_hours(sub(tz="America/New_York", quiet=QUIET), at)
    assert not in_quiet_hours(sub(tz="Asia/Tokyo", quiet=QUIET), at)  # 12:00


# --- schedules ---


def test_weekday_reminder_fires_once_per_day_in_local_time():
    s = sub(schedules=[{"days": [1, 2, 3, 4, 5], "time": "08:30"}])
    fired = minutely(s, datetime(2026, 10, 9, tzinfo=UTC), hours=24 * 7)  # Fri .. Thu
    assert [local(t, BUCHAREST) for t in fired] == [
        f"{d} 08:30 EEST" for d in ("Fri", "Mon", "Tue", "Wed", "Thu")
    ]
    assert fired[0] == datetime(2026, 10, 9, 5, 30, tzinfo=UTC)


def test_same_schedule_in_other_time_zones():
    rule = [{"days": [5], "time": "08:30"}]  # Fridays
    start = datetime(2026, 10, 9, tzinfo=UTC)
    assert minutely(sub(tz="America/New_York", schedules=rule), start, 24) == [
        datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
    ]
    assert minutely(sub(tz="Asia/Tokyo", schedules=rule), start - timedelta(hours=9), 24) == [
        datetime(2026, 10, 8, 23, 30, tzinfo=UTC)
    ]


def test_autumn_dst_repeated_hour_fires_once():
    """Bucharest, Sunday 25 Oct 2026: 04:00 EEST → 03:00 EET, so 03:30 happens twice."""
    s = sub(schedules=[{"days": [7], "time": "03:30"}, {"days": [7], "time": "08:30"}])
    fired = minutely(s, datetime(2026, 10, 24, 18, 0, tzinfo=UTC), hours=24)
    assert fired == [
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),  # 03:30 EEST, the first pass
        datetime(2026, 10, 25, 6, 30, tzinfo=UTC),  # 08:30 EET: an hour later in UTC than Fri
    ]
    assert [local(t, BUCHAREST) for t in fired] == ["Sun 03:30 EEST", "Sun 08:30 EET"]


def test_spring_dst_skipped_time_still_fires_once():
    """Bucharest, Sunday 29 Mar 2026: 03:00 EET → 04:00 EEST, so 03:30 doesn't exist."""
    s = sub(schedules=[{"days": [7], "time": "03:30"}])
    fired = minutely(s, datetime(2026, 3, 28, 18, 0, tzinfo=UTC), hours=24)
    assert fired == [datetime(2026, 3, 29, 1, 30, tzinfo=UTC)]
    assert local(fired[0], BUCHAREST) == "Sun 04:30 EEST"


def test_reminder_at_midnight_and_overlapping_schedules():
    s = sub(
        schedules=[
            {"days": [6], "time": "00:00"},
            {"days": [5, 6], "time": "00:00"},  # same instant twice → once
        ]
    )
    fired = minutely(s, datetime(2026, 10, 9, 12, 0, tzinfo=UTC), hours=24)
    assert [local(t, BUCHAREST) for t in fired] == ["Sat 00:00 EEST"]


def test_should_send_schedule_skips_quiet_hours_and_recent_pushes():
    rule = [{"days": [5], "time": "08:30"}]
    due = datetime(2026, 10, 9, 5, 30, tzinfo=UTC)
    window = (due - timedelta(minutes=1), due)
    assert should_send_schedule(sub(schedules=rule), *window, due)
    assert not should_send_schedule(sub(schedules=rule), due, due + timedelta(minutes=1), due)
    # quiet hours 08:00–09:00 cover the reminder
    quiet = {"from": "08:00", "to": "09:00"}
    assert not should_send_schedule(sub(schedules=rule, quiet=quiet), *window, due)
    assert should_send_schedule(sub(schedules=rule, quiet=QUIET), *window, due)
    # a push 9 min ago (e.g. on my way) → skip; 10 min ago → send
    recent = sub(schedules=rule, last_sent_at=due - timedelta(minutes=9))
    assert not should_send_schedule(recent, *window, due)
    older = sub(schedules=rule, last_sent_at=due - timedelta(minutes=10))
    assert should_send_schedule(older, *window, due)


# --- almost full ---


def test_almost_full_on_worse_levels_only():
    s = sub(almost_full=True)
    # ground has 40 spaces: 8+ plenty, 2–7 filling, 1 almost_full, 0 full
    assert [z.id for z in almost_full_zones(s, status(5), status(1))] == ["ground"]
    assert [z.id for z in almost_full_zones(s, status(1), status(0))] == ["ground"]
    assert [z.id for z in almost_full_zones(s, status(20), status(0))] == ["ground"]
    assert almost_full_zones(s, status(0), status(1)) == []  # getting better
    assert almost_full_zones(s, status(1), status(1)) == []  # no change
    assert almost_full_zones(s, None, status(0)) == []  # start-up restore
    stale = status(0)
    stale.zones[0].stale = True
    assert almost_full_zones(s, status(5), stale) == []


def test_almost_full_only_for_watched_zones():
    only_under = sub(almost_full=True, zones=["underground"])
    assert almost_full_zones(only_under, status(5), status(0)) == []
    assert [z.id for z in almost_full_zones(only_under, status(5, 30), status(5, 0))] == [
        "underground"
    ]


def test_should_send_almost_full_pref_cooldown_and_quiet_hours():
    now = FRI
    on = sub(almost_full=True)
    assert should_send_almost_full(on, status(5), status(1), now, None)
    assert not should_send_almost_full(sub(), status(5), status(1), now, None)  # pref off
    assert not should_send_almost_full(on, status(5), status(1), now, now - timedelta(hours=1))
    assert should_send_almost_full(on, status(5), status(1), now, now - timedelta(hours=2))
    night = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)  # 23:00 in Bucharest
    quiet = sub(almost_full=True, quiet=QUIET)
    assert not should_send_almost_full(quiet, status(5), status(1), night, None)
    assert should_send_almost_full(quiet, status(5), status(1), now, None)
