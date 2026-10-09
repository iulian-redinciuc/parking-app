"""P7.6: `GET /api/history` and `GET /api/forecast` on a seeded DB (api.md §2): minute/hour
rows as stored, lot-local days from `zone_hour`, the 2000-point limit, the 8-week median
forecast with its 3-point minimum, and the 60 s in-memory cache."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from parking.api.app import create_app
from parking.config import Settings
from parking.core.clock import FakeClock
from parking.db.engine import make_engine, session_scope, upgrade
from parking.db.models import ZoneHour, ZoneMinute

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # Friday, 15:00 in Bucharest (UTC+3)
LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 1, lon: 2}, timezone: Europe/Bucharest}
zones:
  - {id: ground, name: {en: Ground}, method: count, capacity: 40}
  - {id: underground, name: {en: Underground}, method: flow, capacity: 60}
cameras:
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "file:y.jpg"
    lines_file: config/lines/cam-ramp.json
    detector: {model: models/x}
"""


@pytest.fixture
def lot(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    upgrade(f"sqlite:///{tmp_path}/parking.sqlite")
    return tmp_path


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def client(lot, clock):
    settings = Settings(_env_file=None)
    app = create_app(
        lot / "config" / "lot.yaml",
        settings,
        clock=clock,
        db_url=f"sqlite:///{lot}/parking.sqlite",
        tick=False,
    )
    with TestClient(app) as client:
        yield client


def seed(lot, model, rows):
    """`rows` = (zone, bucket_ts, free_avg[, samples]); min/max = free ∓ 2."""
    engine = make_engine(f"sqlite:///{lot}/parking.sqlite")
    with session_scope(engine) as session:
        for zone, ts, free, *samples in rows:
            session.add(
                model(
                    zone_id=zone,
                    bucket_ts=ts,
                    free_avg=free,
                    free_min=int(free) - 2,
                    free_max=int(free) + 2,
                    occupied_avg=40 - free,
                    samples=samples[0] if samples else 60,
                )
            )
    engine.dispose()


def iso(ts):
    return ts.isoformat().replace("+00:00", "Z")


def test_hour_history_reads_zone_hour_in_range(lot, client):
    seed(lot, ZoneHour, [("ground", T0 - timedelta(hours=h), 10.0 + h) for h in range(5)])
    seed(lot, ZoneHour, [("underground", T0 - timedelta(hours=1), 50.0)])
    r = client.get(
        "/api/history",
        params={"zone": "ground", "from": iso(T0 - timedelta(hours=3)), "to": iso(T0)},
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=60"
    body = r.json()
    assert body["zone"] == "ground" and body["bucket"] == "hour"
    assert [p["t"] for p in body["points"]] == [
        "2026-10-09T09:00:00.000Z",
        "2026-10-09T10:00:00.000Z",
        "2026-10-09T11:00:00.000Z",
    ]
    assert body["points"][0] == {
        "t": "2026-10-09T09:00:00.000Z",
        "free_avg": 13.0,
        "free_min": 11,
        "free_max": 15,
        "occupied_avg": 27.0,
    }


def test_minute_history_defaults_to_total_and_the_last_24_hours(lot, client):
    seed(
        lot,
        ZoneMinute,
        [
            ("total", T0 - timedelta(minutes=2), 30.5, 3),
            ("total", T0 - timedelta(minutes=1), 31.0, 1),
            ("total", T0 - timedelta(hours=25), 1.0),  # outside the default range
            ("ground", T0 - timedelta(minutes=1), 9.0),
        ],
    )
    body = client.get("/api/history", params={"bucket": "minute"}).json()
    assert body["zone"] == "total"
    assert body["from"] == "2026-10-08T12:00:00.000Z" and body["to"] == "2026-10-09T12:00:00.000Z"
    assert [(p["t"], p["free_avg"]) for p in body["points"]] == [
        ("2026-10-09T11:58:00.000Z", 30.5),
        ("2026-10-09T11:59:00.000Z", 31.0),
    ]


def test_day_history_groups_hours_by_lot_local_day(lot, client):
    # local midnight in Bucharest (UTC+3) is 21:00 UTC
    d = datetime(2026, 10, 8, tzinfo=UTC)
    seed(
        lot,
        ZoneHour,
        [
            ("ground", d.replace(hour=20), 10.0, 60),  # 23:00 local on the 8th
            ("ground", d.replace(hour=21), 20.0, 30),  # 00:00 local on the 9th
            ("ground", d.replace(hour=22), 30.0, 60),
        ],
    )
    body = client.get(
        "/api/history",
        params={"zone": "ground", "bucket": "day", "from": "2026-10-08T00:00:00Z", "to": iso(T0)},
    ).json()
    assert body["points"] == [
        {
            "t": "2026-10-07T21:00:00.000Z",
            "free_avg": 10.0,
            "free_min": 8,
            "free_max": 12,
            "occupied_avg": 30.0,
        },
        {
            "t": "2026-10-08T21:00:00.000Z",
            "free_avg": round((20 * 30 + 30 * 60) / 90, 3),
            "free_min": 18,
            "free_max": 32,
            "occupied_avg": round((20 * 30 + 10 * 60) / 90, 3),
        },
    ]


def test_empty_history_is_200_with_no_points(client):
    body = client.get("/api/history", params={"zone": "underground"}).json()
    assert body["points"] == []


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"zone": "roof"}, 404, "not_found"),
        ({"bucket": "week"}, 422, "bad_request"),
        ({"from": "yesterday"}, 422, "bad_request"),
        ({"from": "2026-10-09T12:00:00Z", "to": "2026-10-09T12:00:00Z"}, 422, "bad_request"),
        ({"from": "2026-10-09T12:00:00Z", "to": "2026-10-09T11:00:00Z"}, 422, "bad_request"),
        # 2001 minutes
        ({"bucket": "minute", "from": "2026-10-08T00:00:00Z", "to": "2026-10-09T09:21:00Z"},
         422, "bad_request"),
        # 2001 hours
        ({"from": "2026-07-18T00:00:00Z", "to": "2026-10-09T09:00:00Z"}, 422, "bad_request"),
    ],
)  # fmt: skip
def test_bad_history_requests(client, params, status, code):
    r = client.get("/api/history", params=params)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


def test_exactly_2000_points_is_allowed(client):
    r = client.get(
        "/api/history",
        params={"bucket": "minute", "from": "2026-10-08T00:00:00Z", "to": "2026-10-09T09:20:00Z"},
    )
    assert r.status_code == 200, r.text


def test_history_is_cached_for_60_seconds(lot, client, clock):
    params = {"zone": "ground", "from": iso(T0 - timedelta(hours=3)), "to": iso(T0)}
    seed(lot, ZoneHour, [("ground", T0 - timedelta(hours=2), 10.0)])
    assert len(client.get("/api/history", params=params).json()["points"]) == 1
    seed(lot, ZoneHour, [("ground", T0 - timedelta(hours=1), 11.0)])
    clock.advance(59)
    assert len(client.get("/api/history", params=params).json()["points"]) == 1
    clock.advance(1)
    assert len(client.get("/api/history", params=params).json()["points"]) == 2


def fridays(values, hour_utc=12):
    """`zone_hour` rows for the same Friday hour 1, 2, … weeks before T0."""
    return [
        ("ground", T0.replace(hour=hour_utc) - timedelta(weeks=k + 1), v)
        for k, v in enumerate(values)
        if v is not None
    ]


def test_forecast_is_the_median_of_the_same_weekday_and_hour(lot, client):
    # weeks 1-5 have data, week 9 is too old, other hours/days don't count
    seed(lot, ZoneHour, fridays([10.0, 12.0, 14.0, 30.0, 8.0, None, None, None, 0.0]))
    seed(lot, ZoneHour, [("ground", T0 - timedelta(weeks=1, hours=1), 0.0)])
    seed(lot, ZoneHour, [("ground", T0 - timedelta(days=6), 0.0)])
    r = client.get("/api/forecast", params={"zone": "ground"})  # at = now + 30 min, 15:30 local
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "public, max-age=60"
    assert r.json() == {
        "zone": "ground",
        "at": "2026-10-09T12:30:00.000Z",
        "free_expected": 12,
        "basis": "median of last 8 same weekday/hour",
        "samples": 5,
    }


def test_forecast_at_another_time_and_rounding(lot, client):
    seed(lot, ZoneHour, fridays([10.0, 11.0, 12.0, 13.0], hour_utc=6))  # 09:00 local
    r = client.get("/api/forecast", params={"zone": "ground", "at": "2026-10-16T09:59:00+03:00"})
    assert r.json()["free_expected"] == 12  # median 11.5 rounds to even
    assert r.json()["at"] == "2026-10-16T06:59:00.000Z"
    assert r.json()["samples"] == 4  # the 4 Fridays before the 16th that have data


def test_forecast_needs_3_points(lot, client):
    seed(lot, ZoneHour, fridays([10.0, 12.0]))
    r = client.get("/api/forecast", params={"zone": "ground"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_enough_data"
    assert client.get("/api/forecast", params={"zone": "roof"}).status_code == 404


def test_forecast_is_capped_at_capacity(lot, client):
    seed(lot, ZoneHour, [("total", ts, 120.0) for _, ts, _ in fridays([1, 1, 1])])
    assert client.get("/api/forecast").json()["free_expected"] == 100  # 40 + 60
