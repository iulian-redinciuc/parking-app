"""Hourly weather for the forecast features, from Open-Meteo (forecast.md §3).

Free and keyless for non-commercial use (data CC BY 4.0, https://open-meteo.com). Only the
lot's coordinates, rounded to 2 decimals (~1 km), are sent. `weather_hour` holds the past
hours (as measured / reanalysed) and the next 7 days (as forecast, replaced by the actual
values on later runs).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, col, delete, func, select

from parking.db.engine import session_scope
from parking.db.models import WeatherHour

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = "temperature_2m,precipitation"
TIMEOUT_S = 20
PAST_DAYS_MAX = 92  # the forecast endpoint's limit
PAST_DAYS = 7  # a normal nightly refresh
FORECAST_DAYS = 7
ARCHIVE_LAG = timedelta(days=6)  # the archive is a few days behind

Row = tuple[datetime, float | None, float | None]  # hour (UTC), °C, mm


def parse(body: dict) -> list[Row]:
    """The hours of an Open-Meteo answer that have at least one value."""
    hourly = body.get("hourly") or {}
    rows: list[Row] = []
    for t, temp, precip in zip(
        hourly.get("time", []),
        hourly.get("temperature_2m", []),
        hourly.get("precipitation", []),
        strict=False,
    ):
        if temp is None and precip is None:
            continue
        rows.append((datetime.fromisoformat(t).replace(tzinfo=UTC), temp, precip))
    return rows


def _get(client: httpx.Client, url: str, lat: float, lon: float, **params) -> list[Row]:
    query = {
        "latitude": round(lat, 2),
        "longitude": round(lon, 2),
        "hourly": HOURLY,
        "timezone": "UTC",
        **params,
    }
    response = client.get(url, params=query, timeout=TIMEOUT_S)
    response.raise_for_status()
    return parse(response.json())


def fetch_recent(client: httpx.Client, lat: float, lon: float, past_days: int) -> list[Row]:
    """The last `past_days` days and the next 7."""
    return _get(client, FORECAST_URL, lat, lon, past_days=past_days, forecast_days=FORECAST_DAYS)


def fetch_archive(
    client: httpx.Client, lat: float, lon: float, start: date, end: date
) -> list[Row]:
    return _get(
        client, ARCHIVE_URL, lat, lon, start_date=start.isoformat(), end_date=end.isoformat()
    )


def store(session: Session, lot_id: str, rows: list[Row]) -> int:
    """Replace the lot's hours in the span of `rows` with them."""
    if not rows:
        return 0
    times = [r[0] for r in rows]
    session.exec(
        delete(WeatherHour).where(
            col(WeatherHour.lot_id) == lot_id,
            col(WeatherHour.bucket_ts) >= min(times),
            col(WeatherHour.bucket_ts) <= max(times),
        )
    )
    session.add_all(
        WeatherHour(lot_id=lot_id, bucket_ts=t, temp_c=temp, precip_mm=precip)
        for t, temp, precip in rows
    )
    return len(rows)


def span(session: Session, lot_id: str) -> tuple[datetime | None, datetime | None]:
    return session.exec(
        select(func.min(WeatherHour.bucket_ts), func.max(WeatherHour.bucket_ts)).where(
            WeatherHour.lot_id == lot_id
        )
    ).one()


def load(session: Session, lot_id: str) -> dict[datetime, tuple[float | None, float | None]]:
    rows = session.exec(select(WeatherHour).where(WeatherHour.lot_id == lot_id)).all()
    return {r.bucket_ts: (r.temp_c, r.precip_mm) for r in rows}


def at(session: Session, lot_id: str, hour: datetime) -> tuple[float | None, float | None]:
    row = session.get(WeatherHour, (lot_id, hour))
    return (None, None) if row is None else (row.temp_c, row.precip_mm)


def refresh(
    engine: Engine,
    lot_id: str,
    lat: float,
    lon: float,
    now: datetime,
    since: datetime | None = None,
    client: httpx.Client | None = None,
) -> int:
    """Bring the lot's `weather_hour` up to date: the last week and the next 7 days; the
    last 92 days when the table is empty or behind; and, once, the archive back to `since`
    (the lot's oldest `zone_hour`) when that is further back. Returns the hours written."""
    own = client is None
    client = client or httpx.Client()
    try:
        with session_scope(engine) as session:
            first, last = span(session, lot_id)
        written = 0
        recent_from = now - timedelta(days=PAST_DAYS_MAX)
        if (
            since is not None
            and since < recent_from
            and (first is None or first > since + ARCHIVE_LAG)
        ):
            rows = fetch_archive(client, lat, lon, since.date(), (now - ARCHIVE_LAG).date())
            with session_scope(engine) as session:
                written += store(session, lot_id, rows)
        behind = last is None or last < now - timedelta(days=PAST_DAYS)
        rows = fetch_recent(client, lat, lon, PAST_DAYS_MAX if behind or written else PAST_DAYS)
        with session_scope(engine) as session:
            written += store(session, lot_id, rows)
        return written
    finally:
        if own:
            client.close()
