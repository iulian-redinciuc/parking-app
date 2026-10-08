"""Public routes: `/healthz` and `/api/stream`; the other `/api/*` routes arrive with P2.9."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import anyio
from fastapi import APIRouter
from sqlalchemy import text
from sse_starlette import EventSourceResponse, ServerSentEvent

from parking.api.deps import ApiError, Runtime, RuntimeDep
from parking.api.sse import RETRY_MS, Broadcaster

log = logging.getLogger(__name__)

# a client that can't take an event (or ping) for this long is dropped
SEND_TIMEOUT_S = 30

router = APIRouter()


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


async def _events(broadcaster: Broadcaster) -> AsyncIterator[ServerSentEvent]:
    """`retry`, the current status, then every new one until the client goes away."""
    queue = broadcaster.subscribe()
    latest = broadcaster.latest  # taken with the subscribe: no gap, no duplicate
    log.debug("stream client connected (%d)", broadcaster.clients)
    try:
        yield ServerSentEvent(retry=RETRY_MS)
        if latest is not None:
            yield ServerSentEvent(latest.data, event="status", id=str(latest.id))
        while True:
            event = await queue.get()
            yield ServerSentEvent(event.data, event="status", id=str(event.id))
    finally:
        broadcaster.unsubscribe(queue)
        log.debug("stream client gone (%d)", broadcaster.clients)


@router.get("/api/stream")
async def stream(rt: RuntimeDep) -> EventSourceResponse:
    """Live `LotStatus` events over SSE (api.md §3); 503 above the client cap."""
    if rt.broadcaster.full:
        raise ApiError(503, "unavailable", "too many live clients, try again later")
    return EventSourceResponse(
        _events(rt.broadcaster),
        headers={"Cache-Control": "no-cache"},
        ping=rt.config.api.sse_ping_s,
        ping_message_factory=lambda: ServerSentEvent(comment="ping"),
        send_timeout=SEND_TIMEOUT_S,
    )
