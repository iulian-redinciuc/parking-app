"""Health-threshold tuning from logged metrics (vision.md §5, P4.4)."""

from datetime import UTC, datetime, timedelta

import pytest

from parking.config import HealthCfg
from parking.vision.health import HealthMetrics
from parking.vision.health_stats import format_metrics, parse_lines, report, suggest, summarize

T0 = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)


def line(camera, ts, mean, lap, diff, issue=None, prefix="2026-10-09 03:00:00 DEBUG x: "):
    return prefix + format_metrics(camera, ts, HealthMetrics(mean, lap, diff), issue)


def test_format_and_parse_round_trip():
    lines = [
        line("cam-ground", T0, 101.234, 512.5, None),
        line("cam-ground", T0 + timedelta(seconds=5), 3.1, 0.4, 0.012, "black"),
        "some other log line",
        "health_metrics camera=x ts=not-a-time mean=1 lap=1 diff=- issue=-",
    ]
    recs = list(parse_lines(lines))
    assert len(recs) == 2
    assert recs[0].camera == "cam-ground" and recs[0].ts == T0
    assert recs[0].mean == pytest.approx(101.23) and recs[0].frame_diff is None
    assert recs[0].issue is None
    assert recs[1].issue == "black" and recs[1].frame_diff == pytest.approx(0.012)


def _day():
    """A day: bright sharp noon, darker softer night (IR), every 10 min."""
    out = []
    for i in range(144):
        ts = T0 + timedelta(minutes=10 * i)
        night = ts.hour < 6 or ts.hour >= 20
        mean, lap = (30.0 + i % 5, 60.0 + i % 7) if night else (120.0 + i % 9, 400.0 + i % 11)
        out.append(line("cam-ground", ts, mean, lap, 1.0 + (i % 3)))
    return out


def test_summarize_groups_by_lot_local_hour():
    hours, total = summarize(parse_lines(_day()), "Europe/Bucharest")  # UTC+3 in October
    assert total.frames == 144 and len(hours) == 24 and all(g.frames == 6 for g in hours.values())
    # 00:00 UTC is 03:00 local, night; 20:00 UTC is 23:00 local
    assert max(hours[3].mean) < 40 and min(hours[12].mean) >= 120
    d = total.as_dict()
    assert d["mean"]["min"] == 30 and d["issues"] == {}


def test_suggest_stays_below_night_frames():
    _, total = summarize(parse_lines(_day()), "UTC")
    s = suggest(total, HealthCfg())
    assert 0 < s["black_mean_max"] < 30 and s["black_mean_max"] == pytest.approx(15)
    assert 0 < s["blur_laplacian_min"] < 60
    assert s["frozen_diff_max"] == 0.5  # every real diff is above the default


def test_suggest_lowers_frozen_for_a_still_scene():
    recs = parse_lines(line("c", T0 + timedelta(seconds=i), 80, 200, 0.2) for i in range(50))
    _, total = summarize(recs, "UTC")
    assert suggest(total, HealthCfg())["frozen_diff_max"] == pytest.approx(0.1)


def test_suggest_and_report_without_frames():
    hours, total = summarize([], "UTC")
    assert suggest(total, HealthCfg()) == {}
    assert "Suggested" not in report(hours, total, HealthCfg())


def test_report_lists_hours_and_suggestions():
    hours, total = summarize(parse_lines(_day()), "UTC")
    text = report(hours, total, HealthCfg())
    assert "00h" in text and "23h" in text and "all" in text
    assert "black_mean_max: 15.0  [12]" in text


def test_report_warns_without_a_full_day():
    recs = parse_lines(line("c", T0 + timedelta(seconds=i), 80, 200, 2) for i in range(5))
    hours, total = summarize(recs, "UTC")
    assert "Only 1 of 24" in report(hours, total, HealthCfg())
    hours, total = summarize(parse_lines(_day()), "UTC")
    assert "Only" not in report(hours, total, HealthCfg())
