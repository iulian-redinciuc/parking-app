"""History and forecast reads over the rollups (api.md §2, data-model.md §1).

`minute` points come from `zone_minute`, `hour` points from `zone_hour`, `day` points are
the lot-local days of `zone_hour` (weighted by each hour's minutes, so a day is the
time-weighted average of the minutes it has). Buckets with no data are left out.

The forecast is the median `free_avg` of the same lot-local weekday and hour over the last
8 weeks before `at` (wall-clock arithmetic, so it follows DST changes).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import groupby
from typing import Literal
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from parking.db.models import ZoneHour, ZoneMinute
from parking.db.rollups import floor_hour, floor_minute

BucketName = Literal["minute", "hour", "day"]
STEP = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1)}
MAX_POINTS = 2000
FORECAST_WEEKS = 8
FORECAST_MIN_POINTS = 3


@dataclass(frozen=True)
class HistoryPoint:
    t: datetime
    free_avg: float
    free_min: int
    free_max: int
    occupied_avg: float


def local_day(ts: datetime, tz: ZoneInfo) -> datetime:
    """Midnight (lot time) of the day `ts` is in, as UTC."""
    local = ts.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def bucket_start(ts: datetime, bucket: BucketName, tz: ZoneInfo) -> datetime:
    """The start of the bucket `ts` is in (a day starts at lot-local midnight)."""
    if bucket == "minute":
        return floor_minute(ts)
    if bucket == "hour":
        return floor_hour(ts)
    return local_day(ts, tz)


def point_count(start: datetime, end: datetime, bucket: BucketName, tz: ZoneInfo) -> int:
    """How many buckets `[start, end)` touches (days counted as 24 h, close enough for DST)."""
    return -(-(end - bucket_start(start, bucket, tz)) // STEP[bucket])


def _point(row) -> HistoryPoint:
    return HistoryPoint(row.bucket_ts, row.free_avg, row.free_min, row.free_max, row.occupied_avg)


def history(
    session: Session,
    zone_id: str,
    start: datetime,
    end: datetime,
    bucket: BucketName,
    tz: ZoneInfo,
) -> list[HistoryPoint]:
    """The buckets of `zone_id` that start in `[start floored to the bucket, end)`."""
    model = ZoneMinute if bucket == "minute" else ZoneHour
    lo = bucket_start(start, bucket, tz)
    rows = session.exec(
        select(model)
        .where(model.zone_id == zone_id, model.bucket_ts >= lo, model.bucket_ts < end)
        .order_by(col(model.bucket_ts))
    ).all()
    if bucket != "day":
        return [_point(r) for r in rows]
    return days(rows, tz)


def days(hours, tz: ZoneInfo) -> list[HistoryPoint]:
    """Roll `zone_hour` rows (sorted) up into lot-local days, weighting hours by `samples`."""
    out: list[HistoryPoint] = []
    for day, group in groupby(hours, key=lambda h: local_day(h.bucket_ts, tz)):
        group = list(group)
        minutes = sum(h.samples for h in group) or 1
        out.append(
            HistoryPoint(
                t=day,
                free_avg=round(sum(h.free_avg * h.samples for h in group) / minutes, 3),
                free_min=min(h.free_min for h in group),
                free_max=max(h.free_max for h in group),
                occupied_avg=round(sum(h.occupied_avg * h.samples for h in group) / minutes, 3),
            )
        )
    return out


def same_hour_last_weeks(at: datetime, tz: ZoneInfo, weeks: int = FORECAST_WEEKS) -> list[datetime]:
    """The `zone_hour` buckets (UTC hours) holding the start of the lot-local hour `at` is
    in, 1…`weeks` weeks earlier."""
    local = at.astimezone(tz).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    return [
        floor_hour((local - timedelta(weeks=k)).replace(tzinfo=tz).astimezone(UTC))
        for k in range(1, weeks + 1)
    ]


def forecast_values(session: Session, zone_id: str, at: datetime, tz: ZoneInfo) -> list[float]:
    """`free_avg` of the same weekday and hour over the last 8 weeks (the hours that exist)."""
    starts = same_hour_last_weeks(at, tz)
    rows = session.exec(
        select(ZoneHour).where(ZoneHour.zone_id == zone_id, col(ZoneHour.bucket_ts).in_(starts))
    ).all()
    return [r.free_avg for r in rows]


def expected_free(values: list[float], capacity: int) -> int | None:
    """The median rounded to a whole space within `0…capacity`, or None below 3 values."""
    if len(values) < FORECAST_MIN_POINTS:
        return None
    return min(max(round(statistics.median(values)), 0), capacity)
