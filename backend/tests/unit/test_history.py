"""P7.6: history/forecast helpers (lot-local days and hours across DST) and the TTL cache."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from parking.api.cache import TtlCache
from parking.core.clock import FakeClock
from parking.db import history as hist

BUC = ZoneInfo("Europe/Bucharest")  # UTC+3 until 2026-10-25 04:00 local, then UTC+2


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_local_day_starts_at_lot_midnight():
    assert hist.local_day(utc(2026, 10, 8, 20, 59), BUC) == utc(2026, 10, 7, 21)
    assert hist.local_day(utc(2026, 10, 8, 21), BUC) == utc(2026, 10, 8, 21)
    assert hist.local_day(utc(2026, 11, 1, 23), BUC) == utc(2026, 11, 1, 22)  # winter time


def test_point_count():
    start = utc(2026, 10, 9)
    assert hist.point_count(start, start + timedelta(minutes=2000), "minute", BUC) == 2000
    assert (
        hist.point_count(start, start + timedelta(minutes=2000, seconds=1), "minute", BUC) == 2001
    )
    # from mid-hour: the first, partial hour counts
    assert (
        hist.point_count(start + timedelta(minutes=30), start + timedelta(hours=2), "hour", BUC)
        == 2
    )
    assert (
        hist.point_count(start, start + timedelta(days=30), "day", BUC) == 31
    )  # from 21:00 the 8th


def test_same_hour_last_weeks_follows_local_time_across_dst():
    at = utc(2026, 11, 6, 13, 10)  # Friday 15:10 local (UTC+2)
    starts = hist.same_hour_last_weeks(at, BUC)
    assert len(starts) == 8
    assert starts[0] == utc(2026, 10, 30, 13)  # 15:00 local, winter time
    assert starts[1] == utc(2026, 10, 23, 12)  # 15:00 local, summer time
    assert starts[-1] == utc(2026, 9, 11, 12)


def test_same_hour_in_a_half_hour_timezone_uses_the_utc_hour_holding_it():
    kolkata = ZoneInfo("Asia/Kolkata")  # UTC+5:30
    starts = hist.same_hour_last_weeks(utc(2026, 10, 9, 4, 0), kolkata)  # 09:30 local
    assert starts[0] == utc(2026, 10, 2, 3)  # 09:00 local = 03:30 UTC


def test_expected_free():
    assert hist.expected_free([10.0, 12.0], 40) is None
    assert hist.expected_free([10.0, 12.0, 14.0], 40) == 12
    assert hist.expected_free([10.0, 12.6, 14.0, 13.0], 40) == 13
    assert hist.expected_free([50.0, 50.0, 50.0], 40) == 40
    assert hist.expected_free([-1.0, -1.0, -1.0], 40) == 0


def test_ttl_cache_expires_and_drops_the_oldest():
    clock = FakeClock(utc(2026, 10, 9))
    cache = TtlCache(clock, ttl_s=60, max_entries=2)
    cache.put("a", 1)
    clock.advance(59)
    assert cache.get("a") == 1
    clock.advance(1)
    assert cache.get("a") is None
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None and cache.get("b") == 2 and cache.get("c") == 3
