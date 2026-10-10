"""P9.6 (forecast.md): the boosted trees, the holiday calendar, the Open-Meteo client and
the nightly training with its held-out check, on synthetic `zone_hour` rows."""

import json
import math
import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
from sqlmodel import SQLModel, select

from parking.config import ConfigError, ForecastCfg
from parking.db.engine import make_engine, session_scope
from parking.db.models import ForecastModel, WeatherHour, ZoneHour
from parking.forecast import gbm, weather
from parking.forecast import model as fm
from parking.forecast.days import HolidayCalendar, calendar_of

TZ = ZoneInfo("Europe/Bucharest")
NOW = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)
CAPACITY = 100


@pytest.fixture
def engine():
    engine = make_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def busy(local: datetime) -> float:
    """Occupied share: busy on workday office hours, quieter at weekends."""
    peak, centre, width = (0.85, 12.5, 4) if local.weekday() < 5 else (0.45, 14, 3.5)
    return 0.08 + peak * math.exp(-(((local.hour + 0.5 - centre) / width) ** 2))


def fill(engine, weeks: int, cal: HolidayCalendar, effects: bool, noise=2.0, seed=1):
    """`weeks` of hours up to NOW; with `effects` holidays empty the lot and rain fills it."""
    rng = random.Random(seed)
    t, rain = NOW - timedelta(weeks=weeks), 0.0
    with session_scope(engine) as session:
        while t < NOW:
            local = t.astimezone(TZ)
            if rng.random() < 0.04:
                rain = rng.uniform(0.5, 6)
            elif rng.random() < 0.3:
                rain = 0.0
            share = busy(local)
            if effects and cal.is_holiday(local.date()):
                share *= 0.35
            if effects and rain > 0:
                share = min(1, share * 1.25)
            free = min(CAPACITY, max(0, CAPACITY * (1 - share) + rng.gauss(0, noise)))
            session.add(
                ZoneHour(
                    zone_id="ground",
                    bucket_ts=t,
                    free_avg=round(free, 2),
                    free_min=int(free),
                    free_max=int(free) + 1,
                    occupied_avg=CAPACITY - free,
                    samples=60,
                )
            )
            session.add(WeatherHour(lot_id="main", bucket_ts=t, temp_c=15.0, precip_mm=rain))
            t += timedelta(hours=1)


def train(engine, cal, now=NOW, with_weather=True):
    with session_scope(engine) as session:
        wx = weather.load(session, "main") if with_weather else {}
        return fm.train(session, "ground", TZ, cal, wx, now)


# --- gbm ---


def test_gbm_learns_a_step_and_an_interaction():
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 10, size=(2000, 3))
    y = np.where(x[:, 0] > 5, 4.0, 0.0) + np.where((x[:, 1] > 3) & (x[:, 0] > 5), -6.0, 0.0)
    model = gbm.fit(x, y + rng.normal(0, 0.1, 2000), rounds=200, lr=0.1)
    assert np.abs(model.predict(x) - y).mean() < 0.25
    assert model.used_features() <= {0, 1, 2} and {0, 1} <= model.used_features()


def test_gbm_missing_values_go_right_and_json_round_trip():
    rng = np.random.default_rng(1)
    x = rng.uniform(0, 1, size=(600, 2))
    x[::3, 1] = np.nan
    y = np.where(np.isnan(x[:, 1]), 5.0, np.where(x[:, 1] > 0.5, 2.0, -2.0))
    model = gbm.fit(x, y, rounds=120, lr=0.1)
    probe = np.array([[0.5, np.nan], [0.5, 0.9], [0.5, 0.1]])
    assert model.predict(probe) == pytest.approx([5, 2, -2], abs=0.5)
    again = gbm.Gbm.from_dict(json.loads(json.dumps(model.to_dict())))
    assert again.predict(probe) == pytest.approx(model.predict(probe))


def test_gbm_with_nothing_to_learn_is_the_mean():
    x = np.zeros((100, 2))
    model = gbm.fit(x, np.full(100, 3.0))
    assert model.trees == [] and model.predict(x[:2]) == pytest.approx([3, 3])


def test_gbm_thresholds():
    assert list(gbm.thresholds(np.array([0, 1, 1, 2, np.nan]), 32)) == [0, 1]
    assert len(gbm.thresholds(np.arange(1000.0), 32)) == 32
    assert len(gbm.thresholds(np.full(10, np.nan), 32)) == 0


# --- holidays ---


def test_holiday_calendar():
    cal = HolidayCalendar("RO", extra=[date(2026, 3, 3)])
    assert cal.is_holiday(date(2026, 12, 25)) and cal.is_holiday(date(2026, 3, 3))
    assert not cal.is_holiday(date(2026, 10, 7))
    assert cal.flags(date(2026, 11, 30)) == (1, 1, 0)  # St Andrew, then the national day
    assert cal.flags(date(2026, 12, 2)) == (0, 0, 1)
    assert HolidayCalendar().flags(date(2026, 12, 25)) == (0, 0, 0)


def test_forecast_config():
    cfg = ForecastCfg()
    assert cfg.model and not cfg.weather and cfg.holidays is None
    assert calendar_of(ForecastCfg(holidays="RO")) is calendar_of(ForecastCfg(holidays="RO"))
    assert ForecastCfg(holidays="DE", holidays_subdiv="BY").holidays_subdiv == "BY"
    for bad in (
        {"holidays": "XX"},
        {"holidays": "DE", "holidays_subdiv": "ZZ"},
        {"holidays_subdiv": "BY"},
        {"wether": True},
    ):
        with pytest.raises((ValueError, ConfigError)):
            ForecastCfg(**bad)


# --- weather ---

BODY = {
    "hourly": {
        "time": ["2026-10-10T00:00", "2026-10-10T01:00", "2026-10-10T02:00"],
        "temperature_2m": [11.5, None, None],
        "precipitation": [0.0, 0.4, None],
    }
}


def weather_client(calls: list, body=BODY) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.url.params)))
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_weather_parse_skips_empty_hours():
    assert weather.parse(BODY) == [
        (datetime(2026, 10, 10, 0, tzinfo=UTC), 11.5, 0.0),
        (datetime(2026, 10, 10, 1, tzinfo=UTC), None, 0.4),
    ]
    assert weather.parse({}) == []


def test_weather_refresh_first_time_asks_for_92_days_with_rounded_coordinates(engine):
    calls = []
    assert (
        weather.refresh(engine, "main", 44.43771, 26.09812, NOW, None, weather_client(calls)) == 2
    )
    assert calls == [
        (
            "api.open-meteo.com",
            {
                "latitude": "44.44",
                "longitude": "26.1",
                "hourly": "temperature_2m,precipitation",
                "timezone": "UTC",
                "past_days": "92",
                "forecast_days": "7",
            },
        )
    ]
    with session_scope(engine) as session:
        assert weather.at(session, "main", datetime(2026, 10, 10, 1, tzinfo=UTC)) == (None, 0.4)
        assert weather.at(session, "main", datetime(2026, 10, 10, 2, tzinfo=UTC)) == (None, None)
        assert weather.at(session, "north", datetime(2026, 10, 10, 1, tzinfo=UTC)) == (None, None)


def test_weather_refresh_when_up_to_date_asks_for_a_week_and_replaces(engine):
    with session_scope(engine) as session:
        weather.store(session, "main", [(NOW + timedelta(days=6), 1.0, 1.0)])
        weather.store(session, "main", [(datetime(2026, 10, 10, 0, tzinfo=UTC), 99.0, 9.0)])
    calls = []
    weather.refresh(engine, "main", 1, 2, NOW, NOW - timedelta(days=30), weather_client(calls))
    assert [c[1]["past_days"] for c in calls] == ["7"]
    with session_scope(engine) as session:
        assert weather.at(session, "main", datetime(2026, 10, 10, 0, tzinfo=UTC)) == (11.5, 0.0)
        assert len(session.exec(select(WeatherHour)).all()) == 3


def test_weather_refresh_reaches_back_to_old_history_through_the_archive(engine):
    calls = []
    weather.refresh(engine, "main", 1, 2, NOW, NOW - timedelta(days=200), weather_client(calls))
    assert [c[0] for c in calls] == ["archive-api.open-meteo.com", "api.open-meteo.com"]
    assert calls[0][1]["start_date"] == "2026-03-24" and calls[0][1]["end_date"] == "2026-10-04"
    assert calls[1][1]["past_days"] == "92"


def test_weather_refresh_raises_on_an_error_answer(engine):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(httpx.HTTPStatusError):
        weather.refresh(engine, "main", 1, 2, NOW, None, client)


# --- training and the held-out check ---


def test_features():
    cal = HolidayCalendar("RO")
    at = datetime(2026, 12, 25, 7, 30, tzinfo=UTC)  # Friday 09:30 in Bucharest, Christmas
    row = fm.features(at, [10.0, 14.0, 30.0], TZ, cal, (3.5, None))
    assert row[:9] == [9, 4, 12, 1, 1, 0, 14, 20, 3.5] and math.isnan(row[9])
    assert len(row) == len(fm.FEATURES)


def test_model_is_used_when_holidays_and_rain_move_the_lot(engine):
    cal = HolidayCalendar("RO")
    fill(engine, 30, cal, effects=True)
    report = train(engine, cal)
    assert report.active and report.holdout_rows == 28 * 24
    assert report.mae_model < 0.97 * report.mae_median
    with session_scope(engine) as session:
        active = fm.load_active(session, "ground", NOW + timedelta(hours=5))
        assert active is not None and active.uses_weather
        # a rainy workday noon: fuller than the dry one, both within the capacity
        at = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
        values = [20.0, 22.0, 18.0, 21.0]
        dry = active.expected_free(at, values, TZ, cal, (15.0, 0.0), CAPACITY)
        wet = active.expected_free(at, values, TZ, cal, (15.0, 3.0), CAPACITY)
        assert wet < dry <= 25
        assert active.expected_free(at, [0.0, 0.0, 0.0], TZ, cal, (15.0, 3.0), 5) in range(6)
        assert fm.load_active(session, "ground", NOW + fm.MAX_AGE + timedelta(hours=1)) is None


def test_model_is_not_used_when_it_does_not_beat_the_median(engine):
    cal = HolidayCalendar("RO")
    fill(engine, 30, cal, effects=False, noise=0)  # every week alike: nothing to add
    report = train(engine, cal)
    assert not report.active and report.mae_model == pytest.approx(report.mae_median)
    assert "does not beat" in report.note
    with session_scope(engine) as session:
        row = session.get(ForecastModel, "ground")
        assert row.model is None and row.mae_median == pytest.approx(report.mae_median)
        assert fm.load_active(session, "ground", NOW) is None


def test_no_model_without_enough_history(engine):
    cal = HolidayCalendar()
    fill(engine, 8, cal, effects=True)  # 5 weeks with a median: 1 to train on, 4 held out
    report = train(engine, cal)
    assert not report.active and report.mae_model is None
    assert report.note.startswith("not enough history: 168 hours")


def test_a_later_failed_check_replaces_an_active_model(engine):
    cal = HolidayCalendar("RO")
    fill(engine, 30, cal, effects=True)
    assert train(engine, cal).active
    # nothing new for 5 weeks: the held-out weeks are empty
    assert not train(engine, cal, NOW + timedelta(weeks=5)).active
    with session_scope(engine) as session:
        assert fm.load_active(session, "ground", NOW + timedelta(weeks=5)) is None


def test_a_model_from_other_features_or_broken_json_is_ignored(engine):
    with session_scope(engine) as session:
        for zone, body in (
            ("a", '{"features": ["hour"], "base": 0, "lr": 1, "trees": []}'),
            ("b", "{nope"),
        ):
            session.add(
                ForecastModel(
                    zone_id=zone, trained_at=NOW, active=True, rows=1, holdout_rows=1, model=body
                )
            )
    with session_scope(engine) as session:
        assert fm.load_active(session, "a", NOW) is None
        assert fm.load_active(session, "b", NOW) is None
        assert fm.load_active(session, "missing", NOW) is None


def test_short_hours_are_not_targets(engine):
    cal = HolidayCalendar()
    fill(engine, 6, cal, effects=False)
    with session_scope(engine) as session:
        for row in session.exec(
            select(ZoneHour).where(ZoneHour.bucket_ts >= NOW - timedelta(days=1))
        ):
            row.samples = 10
    with session_scope(engine) as session:
        data = fm.dataset(session, "ground", TZ, cal, {}, NOW)
    assert len(data) == 3 * 7 * 24 - 24 and data.x.shape == (len(data), len(fm.FEATURES))
    assert np.isnan(data.x[:, -1]).all()
