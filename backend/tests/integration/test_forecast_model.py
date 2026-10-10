"""P9.6 (forecast.md): the nightly `forecast` job, `/api/forecast` answering from the model
only while it beats the median, and `parking forecast train` / `status`."""

import math
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from parking.api.app import create_app
from parking.cli import app as cli
from parking.config import Settings
from parking.core.clock import FakeClock
from parking.db.engine import make_engine, session_scope, upgrade
from parking.db.models import ForecastModel, WeatherHour, ZoneHour
from parking.forecast.days import HolidayCalendar

TZ = ZoneInfo("Europe/Bucharest")
NOW = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)
LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 44.43771, lon: 26.09812}, timezone: Europe/Bucharest}
zones:
  - {id: ground, name: {en: Ground}, method: count, capacity: 100}
forecast: %s
"""
WITH_MODEL = "{holidays: RO, weather: true}"


def write_lot(tmp_path, forecast=WITH_MODEL):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML % forecast)
    upgrade(f"sqlite:///{tmp_path}/parking.sqlite")
    return tmp_path


def fill(lot, weeks=30, effects=True):
    """Hours up to NOW for `ground` and `total`: a weekly curve; with `effects` holidays
    empty the lot and rain fills it. Returns the rain per hour."""
    cal, rng = HolidayCalendar("RO"), random.Random(1)
    engine = make_engine(f"sqlite:///{lot}/parking.sqlite")
    t, rain = NOW - timedelta(weeks=weeks), 0.0
    with session_scope(engine) as session:
        while t < NOW:
            local = t.astimezone(TZ)
            if rng.random() < 0.04:
                rain = rng.uniform(0.5, 6)
            elif rng.random() < 0.3:
                rain = 0.0
            peak, centre, width = (0.85, 12.5, 4) if local.weekday() < 5 else (0.45, 14, 3.5)
            share = 0.08 + peak * math.exp(-(((local.hour + 0.5 - centre) / width) ** 2))
            if effects and cal.is_holiday(local.date()):
                share *= 0.35
            if effects and rain > 0:
                share = min(1, share * 1.25)
            free = min(100, max(0, 100 * (1 - share) + rng.gauss(0, 2 if effects else 0)))
            for zone in ("ground", "total"):
                session.add(
                    ZoneHour(
                        zone_id=zone,
                        bucket_ts=t,
                        free_avg=round(free, 2),
                        free_min=int(free),
                        free_max=int(free) + 1,
                        occupied_avg=100 - free,
                        samples=60,
                    )
                )
            session.add(WeatherHour(lot_id="main", bucket_ts=t, temp_c=15.0, precip_mm=rain))
            t += timedelta(hours=1)
    engine.dispose()


def rain_at(lot, hour, mm):
    engine = make_engine(f"sqlite:///{lot}/parking.sqlite")
    with session_scope(engine) as session:
        session.merge(WeatherHour(lot_id="main", bucket_ts=hour, temp_c=15.0, precip_mm=mm))
    engine.dispose()


def open_client(lot, clock):
    app = create_app(
        lot / "config" / "lot.yaml",
        Settings(_env_file=None),
        clock=clock,
        db_url=f"sqlite:///{lot}/parking.sqlite",
        tick=False,
    )
    return TestClient(app)


def no_weather(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, json={"hourly": {"time": []}})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def clock():
    return FakeClock(NOW)


def test_job_trains_and_forecast_uses_the_model(tmp_path, clock):
    lot = write_lot(tmp_path)
    fill(lot)
    monday_noon = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)  # 12:00 in Bucharest
    with open_client(lot, clock) as client:
        params = {"zone": "ground", "at": "2026-10-12T09:10:00Z"}
        before = client.get("/api/forecast", params=params).json()
        assert before["method"] == "median" and before["weather"] is False

        calls = []
        jobs = client.app.state.runtime.jobs
        reports = jobs.run_forecast(NOW, weather_client=no_weather(calls))
        assert calls == ["api.open-meteo.com"]  # 30 weeks of weather are there already
        assert [(r.zone_id, r.active) for r in reports] == [("ground", True), ("total", True)]

        rain_at(lot, monday_noon, 0.0)
        clock.advance(61)  # past the 60 s answer cache
        dry = client.get("/api/forecast", params=params).json()
        assert dry["method"] == "model" and dry["weather"] is True
        assert (
            dry["basis"]
            == "median of last 8 same weekday/hour + model (calendar, holidays, weather)"
        )
        assert dry["samples"] == 8 and dry["at"] == "2026-10-12T09:10:00.000Z"

        rain_at(lot, monday_noon, 4.0)
        clock.advance(61)
        wet = client.get("/api/forecast", params=params).json()
        assert wet["free_expected"] < dry["free_expected"]  # rain fills this lot

        # an hour the weather table doesn't reach: still the model, without the weather
        far = client.get("/api/forecast", params={"at": "2026-10-30T09:10:00Z"}).json()
        assert far["method"] == "model" and far["weather"] is False

        # the nightly job stopped: after 3 days the model is no longer served
        clock.advance(4 * 86400)
        assert client.get("/api/forecast", params=params).json()["method"] == "median"


def test_christmas_is_emptier_than_the_median_says(tmp_path, clock):
    lot = write_lot(tmp_path)
    fill(lot)
    with open_client(lot, clock) as client:
        client.app.state.runtime.jobs.run_forecast(NOW, weather_client=no_weather([]))
    # 3 days before Christmas (a Friday): the median only knows ordinary Fridays
    with open_client(lot, FakeClock(datetime(2026, 10, 11, 8, 0, tzinfo=UTC))) as client:
        friday = client.get("/api/forecast", params={"at": "2026-10-16T09:00:00Z"}).json()
        engine = make_engine(f"sqlite:///{lot}/parking.sqlite")
        with session_scope(engine) as session:
            for k in range(1, 9):  # the 8 Fridays before Christmas, as busy as any
                session.merge(
                    ZoneHour(
                        zone_id="total",
                        bucket_ts=datetime(2026, 12, 25, 10, 0, tzinfo=UTC) - timedelta(weeks=k),
                        free_avg=friday["free_expected"],
                        free_min=0,
                        free_max=100,
                        occupied_avg=0,
                        samples=60,
                    )
                )
            row = session.get(ForecastModel, "total")
            row.trained_at = datetime(2026, 12, 22, 3, 30, tzinfo=UTC)
        engine.dispose()
    with open_client(lot, FakeClock(datetime(2026, 12, 22, 8, 0, tzinfo=UTC))) as client:
        xmas = client.get("/api/forecast", params={"at": "2026-12-25T10:00:00Z"}).json()
        assert xmas["method"] == "model"
        assert xmas["free_expected"] > friday["free_expected"] + 15


def test_model_that_does_not_beat_the_median_is_not_served(tmp_path, clock):
    lot = write_lot(tmp_path, "{}")
    fill(lot, effects=False)
    with open_client(lot, clock) as client:
        jobs = client.app.state.runtime.jobs
        reports = jobs.run_forecast(NOW)  # weather is off: no request is made
        assert [r.active for r in reports] == [False, False]
        assert client.get("/api/forecast").json()["method"] == "median"


def test_forecast_model_off(tmp_path, clock):
    lot = write_lot(tmp_path, "{model: false}")
    fill(lot)
    with open_client(lot, clock) as client:
        assert client.app.state.runtime.jobs.run_forecast(NOW) == []
        assert client.get("/api/forecast").json()["method"] == "median"


def test_weather_failure_does_not_stop_training(tmp_path, clock):
    lot = write_lot(tmp_path)
    fill(lot)
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with open_client(lot, clock) as client:
        reports = client.app.state.runtime.jobs.run_forecast(NOW, weather_client=down)
        assert [r.active for r in reports] == [True, True]


def test_cli_train_and_status(tmp_path):
    lot = write_lot(tmp_path, "{holidays: RO}")
    args = [
        "--config",
        str(lot / "config" / "lot.yaml"),
        "--url",
        f"sqlite:///{lot}/parking.sqlite",
    ]
    result = CliRunner().invoke(cli, ["forecast", "status", *args])
    assert result.exit_code == 0 and "no forecast model trained yet" in result.output
    result = CliRunner().invoke(cli, ["forecast", "train", *args])
    assert result.exit_code == 0, result.output
    assert "not enough history: 0 hours" in result.output
    result = CliRunner().invoke(cli, ["forecast", "status", *args])
    lines = result.output.splitlines()
    assert lines[0].startswith("last trained: ") and lines[1].split()[:2] == ["zone", "hours"]
    assert [line.split()[0] for line in lines[2:]] == ["ground", "total"]
    assert "median (not enough history" in lines[2]
