"""`create_app()`: the FastAPI app with its lifespan (architecture.md §4).

Start-up: `db upgrade`, load lot.yaml and the slot files, restore the `StateStore` from the
DB (stale until fresh data, data-model.md §2) and start the 1 s `tick()` task. CORS, rate
limits and the public `/api/*` routes come with P2.8–P2.9.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from parking import __version__
from parking.api.deps import ApiError, Runtime
from parking.api.ingest import Ingestor
from parking.api.routes import internal, public
from parking.config import LotConfig, Settings, SlotFile, cli_env, load_config, load_slots
from parking.core.clock import Clock, SystemClock
from parking.db.engine import default_url, make_engine, upgrade

log = logging.getLogger(__name__)

TICK_S = 1.0


def find_config(path: Path) -> Path:
    """`path`, or for a relative path that doesn't exist also `../path` (run from `backend/`)."""
    if path.is_file() or path.is_absolute():
        return path
    up = Path("..") / path
    return up if up.is_file() else path


def _resolve(path: Path, root: Path) -> Path:
    return path if path.is_absolute() or path.exists() else root / path


def load_slot_files(config: LotConfig, root: Path) -> dict[str, SlotFile]:
    """Slot files of the occupancy cameras; a missing or broken one is logged and skipped."""
    files = {}
    for cam in config.cameras:
        if cam.role != "occupancy" or cam.slots_file is None:
            continue
        try:
            files[cam.id] = load_slots(_resolve(cam.slots_file, root))
        except ValueError as e:  # ConfigError or a pydantic error
            log.error("camera %s: slot file not loaded: %s", cam.id, e)
    return files


async def _tick_loop(ingestor: Ingestor) -> None:
    while True:
        await asyncio.sleep(TICK_S)
        try:
            await ingestor.tick()
        except Exception:
            log.exception("tick failed")


def create_app(
    config_path: Path = Path("config/lot.yaml"),
    settings: Settings | None = None,
    clock: Clock | None = None,
    db_url: str | None = None,
    tick: bool = True,
) -> FastAPI:
    """The API. `config_path` is lot.yaml; its folder's parent is the app root (`config/`,
    `data/`, `deploy/.env`). `tick=False` leaves the tick task off (tests call `tick()`)."""
    config_path = find_config(config_path)
    root = config_path.resolve().parent.parent
    settings = settings or Settings(_env_file=root / "deploy" / ".env")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from parking.core.fusion import StateStore

        config = load_config(config_path, cli_env(root))
        url = db_url or settings.parking_db_url or default_url(root)
        await anyio.to_thread.run_sync(upgrade, url)
        engine = make_engine(url)
        store = StateStore(config, clock or SystemClock(), load_slot_files(config, root))
        ingestor = Ingestor(store, engine)
        app.state.runtime = Runtime(settings, config, engine, store, ingestor)
        if settings.worker_token is None:
            log.warning("WORKER_TOKEN is not set: every /internal/* request gets 401")
        await ingestor.restore()
        task = asyncio.create_task(_tick_loop(ingestor)) if tick else None
        log.info("API ready: lot %s, %d zone(s), db %s", config.lot.id, len(config.zones), url)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            app.state.runtime = None
            engine.dispose()

    app = FastAPI(title="Parking API", version=__version__, lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        body: dict = {"code": exc.code, "message": exc.message}
        if exc.details is not None:
            body["details"] = exc.details
        return JSONResponse({"error": body}, status_code=exc.status)

    app.include_router(public.router)
    app.include_router(internal.router)
    return app
