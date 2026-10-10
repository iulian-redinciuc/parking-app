"""Public routes (api.md §2): `/healthz`, `/api/lot`, `/api/status`, `/api/stream`,
`/api/history`, `/api/forecast`.

All share the public GET rate limit; zone names follow `?lang=` / `Accept-Language`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import anyio
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import text
from sse_starlette import EventSourceResponse, ServerSentEvent

from parking.api.deps import DEFAULT_LANG, ApiError, LangDep, Runtime, RuntimeDep, public_limit
from parking.api.sse import RETRY_MS, Broadcaster
from parking.db import history as hist
from parking.db.engine import session_scope
from parking.db.rollups import TOTAL
from parking.messages import LotStatus, format_ts

log = logging.getLogger(__name__)

# a client that can't take an event (or ping) for this long is dropped
SEND_TIMEOUT_S = 30

# history / forecast answers are cached this long (in memory) and may be cached by clients
CACHE_CONTROL = "public, max-age=60"
# default ranges when `from` is left out
DEFAULT_SPAN = {
    "minute": timedelta(hours=24),
    "hour": timedelta(hours=24),
    "day": timedelta(days=30),
}
FORECAST_AHEAD = timedelta(minutes=30)
FORECAST_BASIS = f"median of last {hist.FORECAST_WEEKS} same weekday/hour"

router = APIRouter(dependencies=[Depends(public_limit)])


def _db_ok(rt: Runtime) -> bool:
    try:
        with rt.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/healthz")
async def healthz(rt: RuntimeDep) -> dict:
    """200 while the API is alive, even with cameras down (api.md §2)."""
    health = rt.store.health
    stats = rt.ingestor.stats
    return {
        "status": "ok",
        "db": await anyio.to_thread.run_sync(_db_ok, rt),
        "cameras": {
            c.id: health[c.id].state if c.id in health else "unknown" for c in rt.config.cameras
        },
        "restarts": {c.id: rt.ingestor.restarts.get(c.id, 0) for c in rt.config.cameras},
        "ingest": {
            "observations": stats.observations,
            "flow_events": stats.flow_events,
            "health": stats.health,
            "rejected": stats.rejected,
            "db_errors": stats.db_errors,
        },
        "stream": {
            "clients": rt.broadcaster.clients,
            "published": rt.broadcaster.published,
            "dropped": rt.broadcaster.dropped,
        },
    }


@router.get("/api/lot")
async def lot(rt: RuntimeDep, lang: LangDep, response: Response) -> dict:
    """Static lot info (api.md §2); `LOT_LAT`/`LOT_LON` from the settings win over lot.yaml."""
    cfg = rt.config
    loc = cfg.lot.location
    s = rt.settings
    response.headers["Vary"] = "Accept-Language"
    return {
        "v": 1,
        "id": cfg.lot.id,
        "name": cfg.lot.name,
        "location": {
            "lat": s.lot_lat if s.lot_lat is not None else loc.lat,
            "lon": s.lot_lon if s.lot_lon is not None else loc.lon,
        },
        "notify_radius_m": cfg.lot.notify_radius_m,
        "timezone": cfg.lot.timezone,
        "zones": [
            {
                "id": z.id,
                "name": z.display_name(lang),
                "method": z.method,
                "capacity": rt.store.capacity[z.id],
            }
            for z in cfg.zones
        ],
        "levels": {"plenty": cfg.api.levels.plenty, "filling": cfg.api.levels.filling},
        # who runs the cameras, for the Privacy screen (security-privacy.md §4.1)
        "privacy": {"operator": s.privacy_operator, "contact": s.privacy_contact},
    }


@router.get("/api/status", response_model=LotStatus)
async def status(rt: RuntimeDep, lang: LangDep, response: Response) -> LotStatus:
    """The current `LotStatus`; 503 until the first data (or a restore from the DB)."""
    if not rt.store.has_data:
        raise ApiError(503, "unavailable", "no data received yet since start-up")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Vary"] = "Accept-Language"
    return rt.store.status(lang)


async def _events(
    broadcaster: Broadcaster, lang: str = DEFAULT_LANG, names: Mapping[str, str] | None = None
) -> AsyncIterator[ServerSentEvent]:
    """`retry`, the current status, then every new one until the client goes away."""
    names = names or {}
    queue = broadcaster.subscribe()
    latest = broadcaster.latest  # taken with the subscribe: no gap, no duplicate
    log.debug("stream client connected (%d)", broadcaster.clients)
    try:
        yield ServerSentEvent(retry=RETRY_MS)
        if latest is not None:
            data = broadcaster.render(latest, lang, names)
            yield ServerSentEvent(data, event="status", id=str(latest.id))
        while True:
            event = await queue.get()
            data = broadcaster.render(event, lang, names)
            yield ServerSentEvent(data, event="status", id=str(event.id))
    finally:
        broadcaster.unsubscribe(queue)
        log.debug("stream client gone (%d)", broadcaster.clients)


@router.get("/api/stream")
async def stream(rt: RuntimeDep, lang: LangDep) -> EventSourceResponse:
    """Live `LotStatus` events over SSE (api.md §3); 503 above the client cap."""
    if rt.broadcaster.full:
        raise ApiError(503, "unavailable", "too many live clients, try again later")
    names = {z.id: z.display_name(lang) for z in rt.config.zones}
    return EventSourceResponse(
        _events(rt.broadcaster, lang, names),
        headers={"Cache-Control": "no-cache"},
        ping=rt.config.api.sse_ping_s,
        # a named event, not a `: ping` comment: browsers' EventSource hides comments, and the
        # frontend's 30 s watchdog (frontend.md §3) needs to see the stream is alive
        ping_message_factory=lambda: ServerSentEvent("", event="ping"),
        send_timeout=SEND_TIMEOUT_S,
    )


def _zone_capacity(rt: Runtime, zone: str) -> int:
    """The zone's capacity (`total` = all zones); unknown zone -> 404."""
    if zone == TOTAL:
        return sum(rt.store.capacity.values())
    if zone not in rt.store.capacity:
        raise ApiError(404, "not_found", f"Zone '{zone}' does not exist")
    return rt.store.capacity[zone]


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


@router.get("/api/history")
async def history(
    rt: RuntimeDep,
    response: Response,
    zone: Annotated[str, Query(max_length=64, description="A zone id or `total`.")] = TOTAL,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    bucket: hist.BucketName = "hour",
) -> dict:
    """Free/occupied per minute, hour or lot-local day from the rollups (max 2000 points)."""
    _zone_capacity(rt, zone)
    response.headers["Cache-Control"] = CACHE_CONTROL
    key = ("history", zone, start, end, bucket)
    if (cached := rt.cache.get(key)) is not None:
        return cached
    tz = ZoneInfo(rt.config.lot.timezone)
    end_ts = _utc(end) if end else rt.store.clock.now()
    start_ts = _utc(start) if start else end_ts - DEFAULT_SPAN[bucket]
    if start_ts >= end_ts:
        raise ApiError(422, "bad_request", "`from` must be before `to`")
    count = hist.point_count(start_ts, end_ts, bucket, tz)
    if count > hist.MAX_POINTS:
        raise ApiError(
            422,
            "bad_request",
            f"range too long: {count} {bucket} points, at most {hist.MAX_POINTS}",
        )

    def load() -> list[hist.HistoryPoint]:
        with session_scope(rt.engine) as session:
            return hist.history(session, zone, start_ts, end_ts, bucket, tz)

    points = await anyio.to_thread.run_sync(load)
    body = {
        "zone": zone,
        "bucket": bucket,
        "from": format_ts(start_ts),
        "to": format_ts(end_ts),
        "points": [
            {
                "t": format_ts(p.t),
                "free_avg": p.free_avg,
                "free_min": p.free_min,
                "free_max": p.free_max,
                "occupied_avg": p.occupied_avg,
            }
            for p in points
        ],
    }
    rt.cache.put(key, body)
    return body


@router.get("/api/forecast")
async def forecast(
    rt: RuntimeDep,
    response: Response,
    zone: Annotated[str, Query(max_length=64, description="A zone id or `total`.")] = TOTAL,
    at: Annotated[datetime | None, Query(description="Default: now + 30 min.")] = None,
) -> dict:
    """The median free count of the same weekday and hour over the last 8 weeks;
    404 `not_enough_data` with fewer than 3 of those hours in `zone_hour`."""
    capacity = _zone_capacity(rt, zone)
    key = ("forecast", zone, at)
    if (cached := rt.cache.get(key)) is None:
        at_ts = _utc(at) if at else rt.store.clock.now() + FORECAST_AHEAD
        tz = ZoneInfo(rt.config.lot.timezone)

        def load() -> list[float]:
            with session_scope(rt.engine) as session:
                return hist.forecast_values(session, zone, at_ts, tz)

        values = await anyio.to_thread.run_sync(load)
        cached = (at_ts, len(values), hist.expected_free(values, capacity))
        rt.cache.put(key, cached)
    at_ts, samples, expected = cached
    if expected is None:
        raise ApiError(
            404,
            "not_enough_data",
            f"only {samples} of the last {hist.FORECAST_WEEKS} same weekday/hour have data"
            f" (at least {hist.FORECAST_MIN_POINTS} needed)",
        )
    response.headers["Cache-Control"] = CACHE_CONTROL
    return {
        "zone": zone,
        "at": format_ts(at_ts),
        "free_expected": expected,
        "basis": FORECAST_BASIS,
        "samples": samples,
    }
