"""Shared request dependencies: the app's runtime objects, worker-token auth (api.md §5.1),
rate limits and the zone-name language (api.md §1, §7)."""

from __future__ import annotations

import hmac
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from sqlalchemy import Engine

from parking.api.ingest import Ingestor
from parking.api.sse import Broadcaster
from parking.config import LotConfig, Settings
from parking.core.fusion import StateStore
from parking.push.on_my_way import OnMyWayNotifier
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
    """What the lifespan builds; kept on `app.state.runtime`."""

    settings: Settings
    config: LotConfig
    engine: Engine
    store: StateStore
    ingestor: Ingestor
    broadcaster: Broadcaster
    push: PushSender | None = None  # None without VAPID keys in .env
    on_my_way: OnMyWayNotifier | None = None  # likewise


def runtime(request: Request) -> Runtime:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise ApiError(503, "unavailable", "the API is starting up")
    return rt


RuntimeDep = Annotated[Runtime, Depends(runtime)]


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
    config = runtime(request).config
    available = {code for zone in config.zones for code in zone.name}
    return resolve_lang(lang, request.headers.get("accept-language"), available)


LangDep = Annotated[str, Depends(lang)]
