"""Temporal smoothing (vision.md §3, testing.md §2) and the injectable clock."""

from datetime import UTC, datetime, timedelta

import pytest

from parking.core.clock import Clock, FakeClock, SystemClock
from parking.core.smoothing import CountSmoother, SlotSmoother


def feed(s, slot, readings):
    return [s.update(slot, r) for r in readings]


def test_first_reading_sets_state():
    s = SlotSmoother(k=3)
    assert s.state("G01") is None
    assert s.update("G01", True) is True
    assert s.update("G02", False) is False
    assert s.states == {"G01": True, "G02": False}


def test_k_minus_one_contrary_readings_dont_flip():
    s = SlotSmoother(k=3)
    s.update("G01", False)
    assert feed(s, "G01", [True, True]) == [False, False]
    assert s.state("G01") is False


def test_k_contrary_readings_flip():
    s = SlotSmoother(k=3)
    s.update("G01", False)
    assert feed(s, "G01", [True, True, True]) == [False, False, True]
    # and back again
    assert feed(s, "G01", [False, False, False]) == [True, True, False]


def test_agreeing_reading_resets_pending():
    s = SlotSmoother(k=3)
    s.update("G01", False)
    assert feed(s, "G01", [True, True, False, True, True]) == [False] * 5
    assert s.update("G01", True) is True


def test_alternating_noise_never_flips():
    s = SlotSmoother(k=3)
    s.update("G01", False)
    assert not any(feed(s, "G01", [True, False] * 50))
    s.update("G02", True)
    assert all(feed(s, "G02", [False, True] * 50))


def test_slots_are_independent():
    s = SlotSmoother(k=2)
    s.update_many({"A": False, "B": False})
    assert s.update_many({"A": True, "B": False}) == {"A": False, "B": False}
    assert s.update_many({"A": True, "B": True}) == {"A": True, "B": False}


def test_k_one_follows_raw():
    s = SlotSmoother(k=1)
    assert feed(s, "G01", [False, True, False, True]) == [False, True, False, True]


def test_restore_seeds_state_and_drops_pending():
    s = SlotSmoother(k=3)
    s.update("G01", False)
    feed(s, "G01", [True, True])
    s.restore({"G01": False, "G02": True})
    assert s.states == {"G01": False, "G02": True}
    # the two pending readings were dropped: one more True doesn't flip
    assert s.update("G01", True) is False
    # a restored slot is not "first reading": a contrary reading doesn't set it directly
    assert s.update("G02", False) is True


def test_invalid_k():
    with pytest.raises(ValueError):
        SlotSmoother(k=0)
    with pytest.raises(ValueError):
        CountSmoother(k=0)


def test_count_first_reading_is_direct():
    c = CountSmoother(k=3)
    assert c.value("ramp") is None
    assert c.update("ramp", 7) == 7


def test_count_median_of_last_k():
    c = CountSmoother(k=3)
    assert [c.update("ramp", n) for n in [10, 10, 30, 10, 11, 12, 12]] == [
        10,
        10,
        10,
        10,
        11,
        11,
        12,
    ]
    assert c.value("ramp") == 12


def test_count_one_frame_glitch_never_shows():
    c = CountSmoother(k=3)
    out = [c.update("ramp", n) for n in [5, 5, 0, 5, 5, 40, 5]]
    assert out == [5] * 7


def test_count_restore_and_zones_independent():
    c = CountSmoother(k=3)
    c.restore({"ramp": 4})
    assert c.update("ramp", 9) == 4  # median_low of [4, 9]
    assert c.update("ramp", 9) == 9
    assert c.update("other", 2) == 2


def test_fake_clock():
    t0 = datetime(2026, 10, 8, 12, 0)
    clock: Clock = FakeClock(t0)
    assert clock.now() == t0.replace(tzinfo=UTC)
    assert clock.advance(30) == datetime(2026, 10, 8, 12, 0, 30, tzinfo=UTC)
    assert clock.advance(minutes=1).minute == 1
    clock.set(datetime(2027, 1, 1))
    assert clock.now() == datetime(2027, 1, 1, tzinfo=UTC)
    assert FakeClock().now().tzinfo is UTC


def test_system_clock_is_utc_now():
    now = SystemClock().now()
    assert now.tzinfo is UTC
    assert abs(datetime.now(UTC) - now) < timedelta(seconds=5)
