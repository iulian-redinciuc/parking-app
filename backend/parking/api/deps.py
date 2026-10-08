"""Shared request dependencies: the app's runtime objects and worker-token auth (api.md §5.1)."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine

from parking.api.ingest import Ingestor
from parking.config import LotConfig, Settings
from parking.core.fusion import StateStore


class ApiError(Exception):
    """Rendered as `{"error": {"code", "message"}}` (api.md §1 error format)."""

    def __init__(self, status: int, code: str, message: str, details: list | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


@dataclass
class Runtime:
    """What the lifespan builds; kept on `app.state.runtime`."""

    settings: Settings
    config: LotConfig
    engine: Engine
    store: StateStore
    ingestor: Ingestor


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
