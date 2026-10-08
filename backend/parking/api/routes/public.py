"""Public routes. P2.7 adds only `/healthz`; `/api/*` arrive with P2.8–P2.9."""

from __future__ import annotations

import anyio
from fastapi import APIRouter
from sqlalchemy import text

from parking.api.deps import Runtime, RuntimeDep

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
    }
