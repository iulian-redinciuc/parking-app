"""Rollups and retention (data-model.md §4).

`zone_state` holds a zone's count only when it changes, so each zone's `free` / `occupied`
is a step function: the value at time t is the last row at or before t. The minute rollup
is **time-weighted** over that step function (free 10 for 45 s then 12 for 15 s -> 10.5);
the hour rollup averages the minutes. `total` is a pseudo-zone: the sum of the zones that
have a value at that time.

Both the API's minutely job and `parking db aggregate --backfill` build the minutes from
`zone_state`, so a live rollup and a rebuilt one are the same. Rebuilding a range deletes
its buckets first, so every job can safely redo the last few minutes/hours.

`prune` deletes rows past the retention periods, but always keeps the newest `zone_state`
per zone and `slot_state` per slot: they're what the API restores at start-up and the value
the next minute carries in. `retention_report` is the check that it really happened: the
oldest row of every table and how many rows are still there past their period.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, insert
from sqlmodel import Session, select

from parking.db.models import (
    AdminSession,
    Correction,
    FlowEvent,
    NotificationLog,
    PushSubscription,
    SlotState,
    ZoneHour,
    ZoneMinute,
    ZoneState,
)

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
TOTAL = "total"

Point = tuple[datetime, int, int]  # (ts, free, occupied)


@dataclass(frozen=True)
class Retention:
    """How long rows are kept (data-model.md §4). `zone_hour` and `correction` are kept."""

    raw: timedelta = timedelta(days=90)  # slot_state, zone_state, flow_event
    minute: timedelta = timedelta(days=30)  # zone_minute
    log: timedelta = timedelta(days=30)  # notification_log


DEFAULT_RETENTION = Retention()


@dataclass(frozen=True)
class Bucket:
    zone_id: str
    bucket_ts: datetime
    free_avg: float
    free_min: int
    free_max: int
    occupied_avg: float
    samples: int


def floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def floor_hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


# --- pure rollup maths ---


def _bucket(zone_id: str, start: datetime, segments: list[tuple[float, int, int]]) -> Bucket:
    """`segments` = (seconds, free, occupied) pieces of one bucket."""
    seconds = sum(s for s, _, _ in segments)
    return Bucket(
        zone_id=zone_id,
        bucket_ts=start,
        free_avg=round(sum(s * f for s, f, _ in segments) / seconds, 3),
        free_min=min(f for _, f, _ in segments),
        free_max=max(f for _, f, _ in segments),
        occupied_avg=round(sum(s * o for s, _, o in segments) / seconds, 3),
        samples=len(segments),
    )


def minute_buckets(
    zone_id: str, points: list[Point], start: datetime, end: datetime
) -> list[Bucket]:
    """Time-weighted minute buckets of one zone in `[start, end)` (both on a minute).

    `points` is the step function sorted by time; points before `start` only give the value
    carried in. A minute with no known value is skipped; one known for part of it (the zone's
    first row) averages over that part. `samples` = values that lasted > 0 s in the minute.
    """
    out: list[Bucket] = []
    i, n = 0, len(points)
    current: Point | None = None
    while i < n and points[i][0] <= start:
        current = points[i]
        i += 1
    t = start
    while t < end:
        bucket_end = t + MINUTE
        segments: list[tuple[float, int, int]] = []
        seg_start = t
        while i < n and points[i][0] < bucket_end:
            ts = points[i][0]
            if current is not None and ts > seg_start:
                segments.append(((ts - seg_start).total_seconds(), current[1], current[2]))
            current, seg_start = points[i], ts
            i += 1
        if current is not None:
            segments.append(((bucket_end - seg_start).total_seconds(), current[1], current[2]))
        if segments:
            out.append(_bucket(zone_id, t, segments))
        t = bucket_end
    return out


def total_points(per_zone: Mapping[str, list[Point]]) -> list[Point]:
    """The `total` step function: at each change, the sum over the zones with a value."""
    events = sorted(
        ((ts, zone_id, free, occ) for zone_id, pts in per_zone.items() for ts, free, occ in pts),
        key=lambda e: e[0],
    )
    current: dict[str, tuple[int, int]] = {}
    out: list[Point] = []
    for k, (ts, zone_id, free, occ) in enumerate(events):
        current[zone_id] = (free, occ)
        if k + 1 < len(events) and events[k + 1][0] == ts:
            continue  # same instant: emit once, after all of them
        out.append((ts, sum(f for f, _ in current.values()), sum(o for _, o in current.values())))
    return out


def hour_buckets(minutes: Iterable[Bucket]) -> list[Bucket]:
    """Roll minute buckets up per zone and hour: mean of the minute averages, min of the
    minimums, max of the maximums; `samples` = minutes rolled up."""
    groups: dict[tuple[str, datetime], list[Bucket]] = defaultdict(list)
    for m in minutes:
        groups[(m.zone_id, floor_hour(m.bucket_ts))].append(m)
    return [
        Bucket(
            zone_id=zone_id,
            bucket_ts=hour,
            free_avg=round(sum(m.free_avg for m in ms) / len(ms), 3),
            free_min=min(m.free_min for m in ms),
            free_max=max(m.free_max for m in ms),
            occupied_avg=round(sum(m.occupied_avg for m in ms) / len(ms), 3),
            samples=len(ms),
        )
        for (zone_id, hour), ms in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    ]


# --- DB ---


def zone_timelines(
    session: Session, zone_ids: Iterable[str], start: datetime, end: datetime
) -> dict[str, list[Point]]:
    """zone id -> its value carried into `start` (if any) plus its rows in `[start, end)`."""
    out: dict[str, list[Point]] = {}
    for zone_id in zone_ids:
        before = session.exec(
            select(ZoneState)
            .where(ZoneState.zone_id == zone_id, ZoneState.ts < start)
            .order_by(ZoneState.ts.desc(), ZoneState.id.desc())
            .limit(1)
        ).first()
        rows = session.exec(
            select(ZoneState)
            .where(ZoneState.zone_id == zone_id, ZoneState.ts >= start, ZoneState.ts < end)
            .order_by(ZoneState.ts, ZoneState.id)
        )
        points = [(r.ts, r.free, r.occupied) for r in ([before] if before else []) + list(rows)]
        if points:
            out[zone_id] = points
    return out


def compute_minutes(
    session: Session, zone_ids: Iterable[str], start: datetime, end: datetime
) -> list[Bucket]:
    """Minute buckets of every zone and `total` in `[start, end)`."""
    timelines = zone_timelines(session, zone_ids, start, end)
    out: list[Bucket] = []
    for zone_id, points in timelines.items():
        out += minute_buckets(zone_id, points, start, end)
    if timelines:
        out += minute_buckets(TOTAL, total_points(timelines), start, end)
    return out


def _replace(session: Session, model, buckets: list[Bucket], start, end) -> int:
    """Delete `model`'s buckets in `[start, end)` and insert `buckets`."""
    session.exec(delete(model).where(model.bucket_ts >= start, model.bucket_ts < end))
    if buckets:
        session.exec(insert(model), params=[asdict(b) for b in buckets])
    return len(buckets)


def aggregate_minutes(
    session: Session, zone_ids: Iterable[str], start: datetime, end: datetime
) -> int:
    """(Re)build `zone_minute` for `[start, end)` from `zone_state`; returns the rows written."""
    start, end = floor_minute(start), floor_minute(end)
    if start >= end:
        return 0
    return _replace(session, ZoneMinute, compute_minutes(session, zone_ids, start, end), start, end)


def aggregate_hours(session: Session, start: datetime, end: datetime) -> int:
    """(Re)build `zone_hour` for `[start, end)` from `zone_minute`; returns the rows written."""
    start, end = floor_hour(start), floor_hour(end)
    if start >= end:
        return 0
    rows = session.exec(
        select(ZoneMinute).where(ZoneMinute.bucket_ts >= start, ZoneMinute.bucket_ts < end)
    )
    minutes = [Bucket(**r.model_dump()) for r in rows]
    return _replace(session, ZoneHour, hour_buckets(minutes), start, end)


def last_bucket(session: Session, model) -> datetime | None:
    """The newest `bucket_ts` of `ZoneMinute` / `ZoneHour`, or None while it's empty."""
    return session.exec(select(func.max(model.bucket_ts))).one()


def backfill(
    session: Session, zone_ids: list[str], now: datetime, retention: Retention = DEFAULT_RETENTION
) -> tuple[int, int]:
    """Rebuild every rollup from `zone_state`: minutes up to `now`'s minute (only those within
    the minute retention are stored), hours up to `now`'s hour. Returns (minutes, hours)."""
    session.exec(delete(ZoneMinute))
    session.exec(delete(ZoneHour))
    first = session.exec(select(func.min(ZoneState.ts))).one()
    if first is None:
        return 0, 0
    end_minute, end_hour = floor_minute(now), floor_hour(now)
    keep_from = now - retention.minute
    minutes_written = hours_written = 0
    day = floor_hour(first)
    while day < end_minute:  # a day at a time keeps memory flat
        chunk_end = min(day + DAY, end_minute)
        minutes = compute_minutes(session, zone_ids, day, chunk_end)
        kept = [m for m in minutes if m.bucket_ts >= keep_from]
        hours = [h for h in hour_buckets(minutes) if h.bucket_ts < end_hour]
        if kept:
            session.exec(insert(ZoneMinute), params=[asdict(b) for b in kept])
        if hours:
            session.exec(insert(ZoneHour), params=[asdict(b) for b in hours])
        minutes_written += len(kept)
        hours_written += len(hours)
        day = chunk_end
    return minutes_written, hours_written


def _expired(now: datetime, retention: Retention) -> dict[str, tuple]:
    """table -> (model, time column, the condition for rows past their retention)."""
    raw = now - retention.raw
    newest_zone = select(func.max(ZoneState.id)).group_by(ZoneState.zone_id)
    newest_slot = select(func.max(SlotState.id)).group_by(SlotState.camera_id, SlotState.slot_id)
    return {
        "zone_state": (
            ZoneState,
            ZoneState.ts,
            (ZoneState.ts < raw, ZoneState.id.not_in(newest_zone)),
        ),
        "slot_state": (
            SlotState,
            SlotState.ts,
            (SlotState.ts < raw, SlotState.id.not_in(newest_slot)),
        ),
        "flow_event": (FlowEvent, FlowEvent.ts, (FlowEvent.ts < raw,)),
        "zone_minute": (
            ZoneMinute,
            ZoneMinute.bucket_ts,
            (ZoneMinute.bucket_ts < now - retention.minute,),
        ),
        "notification_log": (
            NotificationLog,
            NotificationLog.ts,
            (NotificationLog.ts < now - retention.log,),
        ),
        "admin_session": (AdminSession, AdminSession.expires_at, (AdminSession.expires_at < now,)),
    }


def prune(
    session: Session, now: datetime, retention: Retention = DEFAULT_RETENTION
) -> dict[str, int]:
    """Delete rows past `retention` and expired admin sessions; returns rows deleted per
    table. The newest `zone_state` per zone and `slot_state` per slot are always kept."""
    return {
        table: session.exec(delete(model).where(*where)).rowcount
        for table, (model, _, where) in _expired(now, retention).items()
    }


# the prune job runs once a day, so a row may outlive its period by up to a day
PRUNE_GRACE = DAY + HOUR

# tables with no retention period, and why (shown by `parking db retention`)
KEPT = {
    "zone_hour": (ZoneHour, ZoneHour.bucket_ts, "kept: hourly averages for the stats"),
    "correction": (Correction, Correction.ts, "kept: audit log of count corrections"),
    "push_subscription": (
        PushSubscription,
        PushSubscription.created_at,
        "kept until the device unsubscribes or its push address stops working",
    ),
}


@dataclass(frozen=True)
class TableRetention:
    table: str
    rows: int
    oldest: datetime | None  # the table's oldest time (admin_session: the earliest expiry)
    keep: str  # "90 days", "until it expires", or why it is kept
    overdue: int  # rows the daily prune should have deleted by now


def retention_report(
    session: Session, now: datetime, retention: Retention = DEFAULT_RETENTION
) -> list[TableRetention]:
    """What the retention jobs left behind (data-model.md §4): per table its rows, its oldest
    time and how many rows are past their period by more than `PRUNE_GRACE`. `overdue` > 0
    means the prune job isn't deleting. The newest `zone_state` per zone and `slot_state` per
    slot never count: they are kept on purpose, so `oldest` can be older than the period."""
    keep = {
        "zone_state": retention.raw,
        "slot_state": retention.raw,
        "flow_event": retention.raw,
        "zone_minute": retention.minute,
        "notification_log": retention.log,
    }

    def stats(model, column) -> tuple[int, datetime | None]:
        return session.exec(select(func.count(), func.min(column)).select_from(model)).one()

    out: list[TableRetention] = []
    for table, (model, column, where) in _expired(now - PRUNE_GRACE, retention).items():
        rows, oldest = stats(model, column)
        overdue = session.exec(select(func.count()).select_from(model).where(*where)).one()
        period = f"{keep[table] / DAY:g} days" if table in keep else "until it expires"
        out.append(TableRetention(table, rows, oldest, period, overdue))
    for table, (model, column, why) in KEPT.items():
        rows, oldest = stats(model, column)
        out.append(TableRetention(table, rows, oldest, why, 0))
    return out
