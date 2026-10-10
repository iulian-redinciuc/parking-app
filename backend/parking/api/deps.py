"""Shared request dependencies: the app's runtime objects, worker-token auth (api.md §5.1),
rate limits and the zone-name language (api.md §1, §7)."""

from __future__ import annotations

import hmac
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Query, Request
from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from sqlalchemy import Engine

from parking.api.cache import TtlCache
from parking.api.ingest import Ingestor
from parking.api.jobs import MaintenanceJobs
from parking.api.sse import Broadcaster
from parking.config import LotConfig, Settings
from parking.core.fusion import StateStore
from parking.push.admin_alerts import AdminAlertMonitor
from parking.push.on_my_way import OnMyWayNotifier
from parking.push.scheduler import AlmostFullNotifier, PushScheduler
from parking.push.sender import PushSender


class ApiError(Exception):
    """Rendered as `{"error": {"code", "message"}}` (api.md §1 error format)."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


@dataclass
class Runtime:
    """What the lifespan builds for one lot. `app.state.runtime` is the default lot's (the
    first in lot.yaml); `lots` holds every lot's by id, in file order (multi-lot.md §3).
    The settings, the engine, the push sender, the root and the jobs are the same objects in
    all of them."""

    settings: Settings
    config: LotConfig
    engine: Engine
    store: StateStore
    ingestor: Ingestor
    broadcaster: Broadcaster
    push: PushSender | None = None  # None without VAPID keys in .env
    on_my_way: OnMyWayNotifier | None = None  # likewise
    almost_full: AlmostFullNotifier | None = None  # likewise
    scheduler: PushScheduler | None = None  # likewise
    root: Path = Path(".")  # the app root: `config/`, `data/`, `deploy/`
    jobs: MaintenanceJobs | None = None  # rollups + retention (data-model.md §4)
    cache: TtlCache | None = None  # history + forecast responses (60 s)
    alerts: AdminAlertMonitor | None = None  # admin alerts (None without VAPID keys)
    lots: dict[str, Runtime] = field(default_factory=dict, repr=False)
    total_key: str = "total"  # this lot's `total` pseudo-zone in the rollup tables

    @property
    def all(self) -> list[Runtime]:
        """Every lot's runtime, the default lot first."""
        return list(self.lots.values()) or [self]

    def of_camera(self, camera_id: str | None) -> Runtime | None:
        """The runtime of the lot `camera_id` belongs to (camera ids are unique across lots)."""
        return next(
            (rt for rt in self.all if any(c.id == camera_id for c in rt.config.cameras)), None
        )

    def of_zone(self, zone_id: str | None) -> Runtime | None:
        return next((rt for rt in self.all if zone_id in rt.store.capacity), None)

    def path(self, path: Path) -> Path:
        """A relative config path (e.g. `slots_file`) under the app root, whatever the cwd."""
        return path if path.is_absolute() else self.root / path


def runtime(request: Request) -> Runtime:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise ApiError(503, "unavailable", "the API is starting up")
    return rt


RuntimeDep = Annotated[Runtime, Depends(runtime)]


def lot_runtime(request: Request) -> Runtime:
    """The lot named in the path (`/api/lots/{lot}/...`), else the default lot."""
    rt = runtime(request)
    lot_id = request.path_params.get("lot")
    if lot_id is None:
        return rt
    found = rt.lots.get(lot_id)
    if found is None:
        raise ApiError(404, "not_found", f"unknown lot '{lot_id}'")
    return found


def camera_runtime(request: Request) -> Runtime:
    """The lot of the `{camera_id}` in the path; the default lot for an unknown camera (the
    route answers 404)."""
    rt = runtime(request)
    return rt.of_camera(request.path_params.get("camera_id")) or rt


def zone_runtime(request: Request) -> Runtime:
    """The lot of the `{zone_id}` in the path; the default lot for an unknown zone."""
    rt = runtime(request)
    return rt.of_zone(request.path_params.get("zone_id")) or rt


LotDep = Annotated[Runtime, Depends(lot_runtime)]
CameraLotDep = Annotated[Runtime, Depends(camera_runtime)]
ZoneLotDep = Annotated[Runtime, Depends(zone_runtime)]


def token_ok(header: str | None, token: str | None) -> bool:
    """`Authorization: Bearer <token>`, compared in constant time; no token set = never ok."""
    if not token or not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode(), token.encode())


def require_worker_token(request: Request) -> None:
    settings = runtime(request).settings
    token = settings.worker_token.get_secret_value() if settings.worker_token else None
    if not token_ok(request.headers.get("authorization"), token):
        raise ApiError(401, "unauthorized", "missing or wrong worker token")


# --- rate limits (api.md §7: per client IP, in memory, per API process) ---

PUBLIC_GET = "120/minute"


class RateLimiter:
    """Moving-window limits from `limits` (the library behind slowapi), kept per app."""

    def __init__(self) -> None:
        self._limiter = MovingWindowRateLimiter(MemoryStorage())

    def hit(self, item: RateLimitItem, group: str, key: str) -> float | None:
        """None if allowed, else the seconds until the next request is."""
        if self._limiter.hit(item, group, key):
            return None
        reset = self._limiter.get_window_stats(item, group, key).reset_time
        return max(reset - time.time(), 0.0)


def rate_limit(limit: str, group: str) -> Callable[[Request], None]:
    """A dependency allowing `limit` (e.g. `120/minute`) per client IP across `group`."""
    item = parse(limit)

    def check(request: Request) -> None:
        limiter: RateLimiter = request.app.state.rate_limiter
        key = request.client.host if request.client else "unknown"
        wait = limiter.hit(item, group, key)
        if wait is not None:
            retry = str(max(math.ceil(wait), 1))
            raise ApiError(
                429, "rate_limited", f"too many requests ({limit})", headers={"Retry-After": retry}
            )

    return check


public_limit = rate_limit(PUBLIC_GET, "public")


# --- language of zone names (api.md §1: `?lang=`, else `Accept-Language`, else `en`) ---

DEFAULT_LANG = "en"


def _primary(tag: str) -> str:
    return tag.strip().split("-")[0].split("_")[0].lower()


def accept_languages(header: str | None) -> list[str]:
    """Primary language subtags of an `Accept-Language` header, best first (`*` and q=0 out)."""
    ranked = []
    for i, part in enumerate((header or "").split(",")):
        tag, _, params = part.partition(";")
        q = 1.0
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        tag = _primary(tag)
        if tag and tag != "*" and q > 0:
            ranked.append((-q, i, tag))
    return [tag for _, _, tag in sorted(ranked)]


def resolve_lang(query: str | None, header: str | None, available: set[str]) -> str:
    """The first of `?lang=` and the `Accept-Language` languages the zone names have, else en."""
    candidates = ([_primary(query)] if query else []) + accept_languages(header)
    return next((lang for lang in candidates if lang in available), DEFAULT_LANG)


def lang(
    request: Request,
    lang: Annotated[
        str | None, Query(max_length=35, description="Language of zone names, e.g. `ro`.")
    ] = None,
) -> str:
    available = {
        code for rt in runtime(request).all for zone in rt.config.zones for code in zone.name
    }
    return resolve_lang(lang, request.headers.get("accept-language"), available)


LangDep = Annotated[str, Depends(lang)]
