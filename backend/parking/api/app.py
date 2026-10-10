"""`create_app()`: the FastAPI app with its lifespan (architecture.md §4).

Start-up: `db upgrade`, load lot.yaml and the slot files, restore the `StateStore` from the
DB (stale until fresh data, data-model.md §2) and start the 1 s `tick()` task. Every new
status goes to the SSE `Broadcaster` (`/api/stream`) and, with push configured, to the
on-my-way and almost-full rules (`OnMyWayNotifier`, `AlmostFullNotifier`, notifications.md
§4-5); the minutely reminder job (`PushScheduler`, APScheduler) runs alongside, and so do the
rollup and retention jobs (`MaintenanceJobs`, data-model.md §4) and, with push, the admin
alert checks every 10 s (`AdminAlertMonitor`, notifications.md §5.1).

Around the routes (api.md §7): CORS for `CORS_ORIGINS` only, gzip for responses over 1 KB
(Starlette never compresses `text/event-stream`), every error as `{"error": {code, message}}`
(api.md §1), per-IP rate limits, and `/docs` only when `LOG_LEVEL=DEBUG`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from parking import __version__
from parking.api.cache import TtlCache
from parking.api.deps import ApiError, RateLimiter, Runtime
from parking.api.ingest import Ingestor
from parking.api.jobs import MaintenanceJobs
from parking.api.routes import admin, internal, public, push
from parking.api.sse import Broadcaster
from parking.config import (
    LotConfig,
    Settings,
    SlotFile,
    cli_env,
    load_lots,
    load_slots,
    total_key,
)
from parking.core.clock import Clock, SystemClock
from parking.db.engine import default_url, make_engine, upgrade
from parking.messages import LotStatus
from parking.push.admin_alerts import AdminAlertMonitor
from parking.push.on_my_way import OnMyWayNotifier
from parking.push.payload import app_url
from parking.push.scheduler import AlmostFullNotifier, PushScheduler
from parking.push.sender import PushSender

log = logging.getLogger(__name__)

TICK_S = 1.0
SHUTDOWN_DRAIN_S = 15  # pending status-change pushes (each send times out after 10 s)
GZIP_MIN_BYTES = 1000
CORS_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE"]
CORS_HEADERS = ["Content-Type", "Authorization"]

# HTTP status -> error code (api.md §1 error format)
ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "bad_request",
    429: "rate_limited",
    503: "unavailable",
}


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
    webpush: Callable[..., Any] | None = None,
) -> FastAPI:
    """The API. `config_path` is lot.yaml; its folder's parent is the app root (`config/`,
    `data/`, `deploy/.env`). `tick=False` leaves the tick task, the reminder job, the
    rollup/retention jobs and the admin alert checks off (tests call `tick()` / `run_due()` /
    `run_minutes()` / `check()`);
    `webpush` replaces pywebpush's sender (tests)."""
    config_path = find_config(config_path)
    root = config_path.resolve().parent.parent
    settings = settings or Settings(_env_file=root / "deploy" / ".env")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lots = load_lots(config_path, cli_env(root))
        url = db_url or settings.parking_db_url or default_url(root)
        await anyio.to_thread.run_sync(upgrade, url)
        engine = make_engine(url)
        clk = clock or SystemClock()
        sender = _push_sender(engine, settings, clk, webpush)
        lock = asyncio.Lock()  # one push dispatch at a time, whatever the lot
        runtimes: dict[str, Runtime] = {}
        for config in lots:
            rt = _lot_runtime(config, lots, settings, engine, clk, sender, lock, root)
            rt.lots = runtimes
            runtimes[config.lot.id] = rt
        first, *others = runtimes.values()
        jobs = MaintenanceJobs(
            engine,
            first.config,
            clk,
            ingestor=first.ingestor,
            more=[(rt.config, rt.ingestor) for rt in others],
        )
        for rt in runtimes.values():
            rt.jobs = jobs
        app.state.runtime = first
        if settings.worker_token is None:
            log.warning("WORKER_TOKEN is not set: every /internal/* request gets 401")
        tasks = []
        for rt in runtimes.values():
            await rt.ingestor.restore()
            if not tick:
                continue
            tasks.append(asyncio.create_task(_tick_loop(rt.ingestor)))
            if rt.scheduler is not None:
                rt.scheduler.start()
            if rt.alerts is not None:
                rt.alerts.start()
        if tick:
            jobs.start()
        for rt in runtimes.values():
            log.info(
                "API ready: lot %s, %d zone(s), db %s", rt.config.lot.id, len(rt.config.zones), url
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for rt in runtimes.values():
                if rt.scheduler is not None:
                    rt.scheduler.shutdown()
                if rt.alerts is not None:
                    await rt.alerts.shutdown()
            jobs.shutdown()
            pending = [
                n.drain() for rt in runtimes.values() for n in (rt.on_my_way, rt.almost_full) if n
            ]
            if pending:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.gather(*pending), SHUTDOWN_DRAIN_S)
            app.state.runtime = None
            engine.dispose()

    debug = settings.log_level.upper() == "DEBUG"
    app = FastAPI(
        title="Parking API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if debug else None,
    )
    app.state.rate_limiter = RateLimiter()
    _add_error_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=CORS_METHODS,
            allow_headers=CORS_HEADERS,
        )
    app.include_router(public.router)
    app.include_router(push.router)
    app.include_router(internal.router)
    app.include_router(admin.router)
    return app


def _lot_runtime(
    config: LotConfig,
    lots: list[LotConfig],
    settings: Settings,
    engine,
    clk: Clock,
    sender: PushSender | None,
    lock: asyncio.Lock,
    root: Path,
) -> Runtime:
    """One lot's store, ingest path, stream and push rules (multi-lot.md §3)."""
    from parking.core.fusion import StateStore

    default = config is lots[0]
    store = StateStore(config, clk, load_slot_files(config, root))
    broadcaster = Broadcaster()
    notifier = almost_full = scheduler = hook = alerts = None
    if sender is not None:
        url = app_url(settings.public_app_url)
        # whose subscriptions this lot's rules serve: its own, and for the default lot also
        # the ones that name no lot; with a single lot, all of them
        own = None if len(lots) == 1 else (config.lot.id, *([None] if default else []))
        notifier = OnMyWayNotifier(engine, sender, config, clk, url, lock, own)
        almost_full = AlmostFullNotifier(engine, sender, config, clk, url, lock, own)
        scheduler = PushScheduler(
            engine, sender, config, clk, url, lambda: _current(store), lock, own
        )
        # the machine's own alerts (disk, restart, backup) come from the default lot's monitor
        alerts = AdminAlertMonitor(
            engine, sender, config, clk, url, store, lock, root if default else None
        )

        def hook(old, new):
            notifier.status_changed(old, new)
            almost_full.status_changed(old, new)

    ingestor = Ingestor(store, engine, broadcaster.publish, hook)
    return Runtime(
        settings,
        config,
        engine,
        store,
        ingestor,
        broadcaster,
        sender,
        notifier,
        almost_full,
        scheduler,
        root,
        None,
        TtlCache(clk),
        alerts,
        total_key=total_key(lots, config.lot.id),
    )


def _current(store) -> LotStatus | None:
    return store.status() if store.has_data else None


def _push_sender(engine, settings: Settings, clock: Clock, webpush) -> PushSender | None:
    """The push sender, or None (push routes answer 503) without VAPID keys or subject."""
    if not (settings.vapid_public_key and settings.vapid_private_key and settings.vapid_subject):
        log.warning("VAPID_* not set: Web Push is off (run `parking push vapid-keys`)")
        return None
    kw = {"webpush": webpush} if webpush is not None else {}
    try:
        return PushSender.from_settings(engine, settings, clock=clock, **kw)
    except Exception as e:  # a malformed private key
        log.error("Web Push is off: %s", e)
        return None


def _error(status: int, code: str, message: str, details=None, headers=None) -> JSONResponse:
    body: dict = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status, headers=headers)


def _add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ERROR_CODES.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else code
        return _error(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {k: v for k, v in err.items() if k in ("type", "loc", "msg")} for err in exc.errors()
        ]
        first = details[0] if details else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "request"
        return _error(422, "bad_request", f"{where}: {first.get('msg', 'invalid')}", details)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        log.error("%s %s failed", request.method, request.url.path, exc_info=exc)
        return _error(500, "internal", "internal server error")


def create_app_from_env() -> FastAPI:
    """Factory for `parking api` (uvicorn, also with `--reload`): lot.yaml from
    `PARKING_CONFIG`, the rest from `deploy/.env` and the environment; sets up logging."""
    config_path = find_config(Path(os.environ.get("PARKING_CONFIG", "config/lot.yaml")))
    settings = Settings(_env_file=config_path.resolve().parent.parent / "deploy" / ".env")
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return create_app(config_path, settings)
