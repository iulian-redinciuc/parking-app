"""P6.6: the on-my-way rules (notifications.md §4) with a fake clock."""

from datetime import UTC, datetime, timedelta

import pytest

from parking.config import Levels
from parking.core.clock import FakeClock
from parking.core.fusion import level_for
from parking.db.models import PushSubscription
from parking.messages import LotStatus, Totals, ZoneStatus
from parking.push.rules import (
    MAX_PER_WINDOW,
    became_full,
    free_threshold,
    should_send_on_my_way,
    watched,
)

T0 = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
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
            updated_at=T0,
        )
        for zid, free in (("ground", ground), ("underground", underground))
    ]
    free = ground + underground
    total = Totals(
        capacity=100,
        occupied=100 - free,
        free=free,
        level=level_for(free, 100),
        confidence=0.9,
        stale=False,
    )
    return LotStatus(lot="main", updated_at=T0, total=total, zones=zones)


def sub(
    *,
    zones=None,
    until=T0 + timedelta(minutes=15),
    last_sent_at=T0,
    last=None,
    sent=1,
) -> PushSubscription:
    """A subscription whose last push showed `last` (a status) at `last_sent_at`."""
    s = PushSubscription(
        id="s1",
        endpoint="https://push.example.test/x",
        p256dh="k",
        auth="a",
        prefs={"zones": zones},
        on_my_way_until=until,
        last_sent_at=last_sent_at,
        on_my_way_sent=sent,
    )
    if last is not None:
        w = watched(s, last)
        s.last_sent_free, s.last_sent_level = w.free, w.level
    return s


@pytest.fixture
def clock():
    return FakeClock(T0)


def later(clock: FakeClock, minutes: float) -> datetime:
    clock.advance(minutes=minutes)
    return clock.now()


def test_free_threshold_is_max_of_3_and_10_percent():
    assert free_threshold(10) == 3
    assert free_threshold(40) == 4
    assert free_threshold(100) == 10


def test_watched_sums_preferred_zones():
    s = sub(zones=["ground"])
    assert watched(s, status(10)).free == 10 and watched(s, status(10)).capacity == 40
    w = watched(sub(), status(10, 30))
    assert (w.free, w.capacity, w.level) == (40, 100, "plenty")
    # every preferred zone gone from lot.yaml -> all zones
    assert watched(sub(zones=["roof"]), status(10, 30)).free == 40


def test_watched_uses_the_lot_thresholds():
    s = sub(zones=["ground"])
    assert watched(s, status(10)).level == "plenty"  # 25 %
    assert watched(s, status(10), Levels(plenty=0.3, filling=0.1)).level == "filling"


def test_no_window_or_expired_window_never_sends(clock):
    old, new = status(30), status(2)  # level change + big drop
    assert not should_send_on_my_way(sub(until=None, last=old), old, new, later(clock, 5))
    s = sub(last=old)
    assert should_send_on_my_way(s, old, new, clock.now())
    assert not should_send_on_my_way(s, old, new, later(clock, 10))  # exactly 15 min: over
    assert not should_send_on_my_way(s, old, new, later(clock, 1))


def test_level_change_sends(clock):
    s = sub(zones=["ground"], last=status(9))  # 9/40 = plenty
    assert should_send_on_my_way(s, status(9), status(7), later(clock, 3))  # filling, Δ2


def test_small_change_same_level_does_not_send(clock):
    s = sub(zones=["ground"], last=status(30))
    assert not should_send_on_my_way(s, status(30), status(27), later(clock, 3))  # Δ3 < 4


def test_big_change_same_level_sends(clock):
    s = sub(zones=["ground"], last=status(30))
    assert should_send_on_my_way(s, status(30), status(26), later(clock, 3))  # Δ4 = 10 %
    # measured against the last push, not the previous status: drifts add up
    assert should_send_on_my_way(s, status(27), status(26), clock.now())


def test_other_zones_are_ignored_with_preferred_zones(clock):
    s = sub(zones=["ground"], last=status(30, 30))
    assert not should_send_on_my_way(s, status(30, 30), status(30, 2), later(clock, 3))
    assert should_send_on_my_way(
        sub(last=status(30, 30)), status(30, 30), status(30, 2), clock.now()
    )


def test_two_minute_gap(clock):
    s = sub(zones=["ground"], last=status(30))
    assert not should_send_on_my_way(s, status(30), status(5), later(clock, 1.9))
    assert should_send_on_my_way(s, status(30), status(5), later(clock, 0.1))  # 2:00


def test_became_full_ignores_the_gap(clock):
    s = sub(zones=["ground"], last=status(1))
    now = later(clock, 0.5)
    assert became_full(s, status(1), status(0))
    assert should_send_on_my_way(s, status(1), status(0), now)
    # already full before: no "became full"
    assert not became_full(s, status(0), status(0))
    # a zone the user doesn't watch filling up doesn't count
    assert not became_full(sub(zones=["ground"]), status(10, 1), status(10, 0))
    assert not became_full(s, None, status(0))


def test_at_most_six_pushes_per_window(clock):
    old, new = status(30), status(2)
    now = later(clock, 5)
    assert should_send_on_my_way(sub(last=old, sent=MAX_PER_WINDOW - 1), old, new, now)
    assert not should_send_on_my_way(sub(last=old, sent=MAX_PER_WINDOW), old, new, now)
    # the cap holds even for "became full"
    assert not should_send_on_my_way(
        sub(zones=["ground"], last=status(1), sent=6), status(1), status(0), now
    )


def test_last_push_without_data_sends_once_data_arrives(clock):
    s = sub(last=None)  # the immediate push said "No data yet"
    assert should_send_on_my_way(s, status(30), status(30), later(clock, 2))
    assert not should_send_on_my_way(s, status(30), status(30), T0 + timedelta(minutes=1))
