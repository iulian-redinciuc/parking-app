"""P7.5: time-weighted minute rollups, the `total` pseudo-zone and hour rollups on synthetic
timelines (data-model.md §4)."""

from datetime import UTC, datetime, timedelta

import pytest

from parking.db.rollups import (
    Bucket,
    floor_hour,
    floor_minute,
    hour_buckets,
    minute_buckets,
    total_points,
)

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_guide_example_45_s_at_10_then_15_s_at_12_averages_10_5():
    points = [(at(-30), 10, 30), (at(45), 12, 28)]
    [b] = minute_buckets("ground", points, T0, at(60))
    assert b.bucket_ts == T0
    assert b.free_avg == 10.5
    assert b.occupied_avg == 29.5
    assert (b.free_min, b.free_max, b.samples) == (10, 12, 2)


def test_value_carries_across_minutes_without_changes():
    points = [(at(-3600), 7, 3)]
    buckets = minute_buckets("z", points, T0, at(180))
    assert [b.bucket_ts for b in buckets] == [T0, at(60), at(120)]
    assert all((b.free_avg, b.free_min, b.free_max, b.samples) == (7, 7, 7, 1) for b in buckets)


def test_minutes_before_the_first_value_are_skipped_and_a_partial_minute_averages_its_part():
    points = [(at(80), 5, 5), (at(100), 9, 1)]
    [b] = minute_buckets("z", points, T0, at(120))
    assert b.bucket_ts == at(60)
    # known for 40 s: 20 s at 5, 20 s at 9
    assert b.free_avg == 7
    assert b.samples == 2


def test_several_changes_in_one_minute_and_one_exactly_on_the_boundary():
    points = [
        (at(-10), 10, 0),
        (at(0), 4, 6),  # on the boundary: replaces the carried value at once
        (at(30), 6, 4),
        (at(30), 8, 2),  # same instant: only the last one lasts
        (at(50), 2, 8),
        (at(60), 3, 7),  # next minute
    ]
    first, second = minute_buckets("z", points, T0, at(120))
    assert first.free_avg == pytest.approx((30 * 4 + 20 * 8 + 10 * 2) / 60, abs=1e-3)
    assert (first.free_min, first.free_max, first.samples) == (2, 8, 3)
    assert (second.free_avg, second.samples) == (3, 1)


def test_points_after_the_range_are_ignored():
    points = [(at(-5), 1, 0), (at(90), 9, 0)]
    [b] = minute_buckets("z", points, T0, at(60))
    assert b.free_avg == 1


def test_total_sums_the_zones_that_have_a_value():
    per_zone = {
        "ground": [(at(-5), 10, 20), (at(30), 12, 18)],
        "underground": [(at(15), 50, 10), (at(30), 49, 11)],
    }
    assert total_points(per_zone) == [
        (at(-5), 10, 20),
        (at(15), 60, 30),
        (at(30), 61, 29),  # both changes at 30 s: one point
    ]
    [b] = minute_buckets("total", total_points(per_zone), T0, at(60))
    assert b.free_avg == pytest.approx((15 * 10 + 15 * 60 + 30 * 61) / 60, abs=1e-3)
    assert (b.free_min, b.free_max) == (10, 61)


def test_hour_rollup_averages_minutes_and_keeps_extremes():
    minutes = [
        Bucket("z", T0 + timedelta(minutes=m), 10 + m % 2, 9 + m % 2, 12, 5.0, 1) for m in range(60)
    ] + [Bucket("z", T0 + timedelta(hours=1), 3, 3, 3, 1, 1)]
    first, second = hour_buckets(minutes)
    assert (first.bucket_ts, first.free_avg, first.free_min, first.free_max) == (T0, 10.5, 9, 12)
    assert (first.occupied_avg, first.samples) == (5, 60)
    assert (second.bucket_ts, second.samples) == (T0 + timedelta(hours=1), 1)


def test_floors():
    ts = datetime(2026, 10, 9, 12, 34, 56, 789, tzinfo=UTC)
    assert floor_minute(ts) == datetime(2026, 10, 9, 12, 34, tzinfo=UTC)
    assert floor_hour(ts) == T0
