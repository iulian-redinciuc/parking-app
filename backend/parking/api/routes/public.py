"""Public routes (api.md §2): `/healthz`, `/api/lot`, `/api/status`, `/api/stream`.

All share the public GET rate limit; zone names follow `?lang=` / `Accept-Language`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping

import anyio
from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sse_starlette import EventSourceResponse, ServerSentEvent

from parking.api.deps import DEFAULT_LANG, ApiError, LangDep, Runtime, RuntimeDep, public_limit
from parking.api.sse import RETRY_MS, Broadcaster
from parking.messages import LotStatus

log = logging.getLogger(__name__)

# a client that can't take an event (or ping) for this long is dropped
SEND_TIMEOUT_S = 30

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
        ping_message_factory=lambda: ServerSentEvent(comment="ping"),
        send_timeout=SEND_TIMEOUT_S,
    )
