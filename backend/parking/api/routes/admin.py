"""Admin routes (api.md §4): password login to a 7-day session token, logout, and the
`require_admin` dependency every other `/api/admin/*` route uses.

A request is an admin's when `Authorization: Bearer <token>` carries either the static
`ADMIN_TOKEN` from `.env` (scripts) or a session token from `POST /api/admin/login`. Session
tokens are 32 random bytes; the DB keeps only their sha256, so a leaked DB can't log anyone
in. The password is checked against `ADMIN_PASSWORD_HASH` (argon2, from
`parking admin hash-password`); login attempts are limited to 5 / 15 min per IP.

Camera health (P7.2) comes from the latest worker health messages; a snapshot is fetched from
the worker's `/control/snapshot` (api.md §5.2, `cameras[].control_url`, `WORKER_TOKEN`).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

import anyio
import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from parking.api.deps import ApiError, Runtime, RuntimeDep, public_limit, rate_limit, runtime
from parking.api.deps import token_ok as static_token_ok
from parking.db.engine import session_scope
from parking.db.models import AdminSession
from parking.messages import format_ts

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")

SESSION_TTL = timedelta(days=7)
LOGIN_LIMIT = "5/15 minutes"
TOKEN_BYTES = 32
SNAPSHOT_TIMEOUT_S = 5.0
SNAPSHOT_MAX_BYTES = 20_000_000  # a 4K JPEG is a few MB
login_limit = rate_limit(LOGIN_LIMIT, "admin-login")


def hash_password(password: str) -> str:
    """The argon2id hash for `ADMIN_PASSWORD_HASH` (argon2-cffi's default parameters)."""
    return PasswordHasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """argon2 verify (its final compare is constant time); a malformed hash is never ok."""
    try:
        return PasswordHasher().verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError) as e:
        log.error("ADMIN_PASSWORD_HASH is not a valid argon2 hash: %s", e)
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(header: str | None) -> str | None:
    scheme, _, value = (header or "").partition(" ")
    value = value.strip()
    return value if scheme.lower() == "bearer" and value else None


@dataclass(frozen=True)
class Admin:
    """Who is calling: `actor` is `admin-token` or `session:<id>` (the audit log's actor)."""

    actor: str
    session_id: int | None = None
    expires_at: datetime | None = None


def _active_session(rt: Runtime, token: str) -> AdminSession | None:
    now = rt.store.clock.now()
    with session_scope(rt.engine) as session:
        row = session.exec(
            select(AdminSession).where(
                AdminSession.token_hash == token_hash(token),
                col(AdminSession.revoked_at).is_(None),
            )
        ).first()
        if row is None or row.expires_at <= now:
            return None
        session.expunge(row)
        return row


def require_admin(request: Request) -> Admin:
    """The admin auth dependency: `ADMIN_TOKEN` or a live session token, else 401."""
    rt = runtime(request)
    header = request.headers.get("authorization")
    static = rt.settings.admin_token.get_secret_value() if rt.settings.admin_token else None
    if static_token_ok(header, static):
        return Admin("admin-token")
    token = _bearer(header)
    row = _active_session(rt, token) if token else None
    if row is None:
        raise ApiError(
            401,
            "unauthorized",
            "missing, wrong or expired admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Admin(f"session:{row.id}", row.id, row.expires_at)


AdminDep = Annotated[Admin, Depends(require_admin)]


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)


@router.post("/login", dependencies=[Depends(login_limit)])
async def login(body: LoginBody, rt: RuntimeDep, request: Request) -> dict:
    """Password → a new session token, valid 7 days. 5 attempts / 15 min per IP."""
    if rt.settings.admin_password_hash is None:
        raise ApiError(503, "unavailable", "admin login is not configured on this server")
    stored = rt.settings.admin_password_hash.get_secret_value()
    if not await anyio.to_thread.run_sync(verify_password, stored, body.password):
        raise ApiError(401, "unauthorized", "wrong password")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = rt.store.clock.now()
    expires = now + SESSION_TTL
    ip = request.client.host if request.client else None
    agent = (request.headers.get("user-agent") or "")[:200] or None

    def store() -> int | None:
        row = AdminSession(
            token_hash=token_hash(token),
            created_at=now,
            expires_at=expires,
            ip=ip,
            user_agent=agent,
        )
        with session_scope(rt.engine) as session:
            session.add(row)
            session.flush()
            return row.id

    session_id = await anyio.to_thread.run_sync(store)
    log.info("admin login: session %s from %s", session_id, ip)
    return {"token": token, "expires_at": format_ts(expires)}


@router.get("/session", dependencies=[Depends(public_limit)])
async def current_session(admin: AdminDep) -> dict:
    """Checks a stored token: who it is and when it expires (`null` for `ADMIN_TOKEN`)."""
    expires = format_ts(admin.expires_at) if admin.expires_at else None
    return {"actor": admin.actor, "expires_at": expires}


@router.post("/logout", status_code=204, dependencies=[Depends(public_limit)])
async def logout(admin: AdminDep, rt: RuntimeDep) -> Response:
    """Revokes the calling session token (with `ADMIN_TOKEN` there is nothing to revoke)."""
    if admin.session_id is not None:

        def revoke() -> None:
            with session_scope(rt.engine) as session:
                row = session.get(AdminSession, admin.session_id)
                if row is not None and row.revoked_at is None:
                    row.revoked_at = rt.store.clock.now()

        await anyio.to_thread.run_sync(revoke)
        log.info("admin logout: session %s", admin.session_id)
    return Response(status_code=204)


# --- cameras (P7.2) ---


@router.get("/cameras", dependencies=[Depends(public_limit)])
async def cameras(_: AdminDep, rt: RuntimeDep) -> list[dict]:
    """Every lot.yaml camera with its latest health; `unknown` until a worker has reported.

    `last_frame_age_s` grows with the time since that health message, so a silent worker's
    frame keeps ageing; `last_health_age_s` is the time since the message itself."""
    now = rt.store.clock.now()
    out = []
    for camera in rt.config.cameras:
        msg = rt.store.health.get(camera.id)
        seen = rt.ingestor.health_seen(camera.id)
        since = (now - seen).total_seconds() if seen else None
        frame_age = msg.last_frame_age_s if msg else None
        if frame_age is not None and since is not None:
            frame_age += since
        out.append(
            {
                "id": camera.id,
                "role": camera.role,
                "zones": camera.zones,
                "state": msg.state if msg else "unknown",
                "issue": msg.issue if msg else None,
                "fps": msg.fps if msg else None,
                "last_frame_age_s": _round(frame_age),
                "inference_ms_avg": _round(msg.inference_ms_avg if msg else None),
                "unhealthy_ratio": msg.unhealthy_ratio if msg else None,
                "last_health_age_s": _round(since),
                "snapshot": camera.control_url is not None,
            }
        )
    return out


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


@router.get("/cameras/{camera_id}/snapshot", dependencies=[Depends(public_limit)])
async def snapshot(camera_id: str, _: AdminDep, rt: RuntimeDep, annotated: bool = True) -> Response:
    """The worker's current frame as JPEG (annotated with its last analysis by default).

    Unknown camera 404; no `control_url` / `WORKER_TOKEN`, a worker that doesn't answer in
    5 s or answers with an error -> 503. Never cached (`Cache-Control: no-store`)."""
    camera = next((c for c in rt.config.cameras if c.id == camera_id), None)
    if camera is None:
        raise ApiError(404, "not_found", f"unknown camera '{camera_id}'")
    if not camera.control_url:
        raise ApiError(503, "unavailable", f"camera '{camera_id}' has no control_url in lot.yaml")
    if rt.settings.worker_token is None:
        raise ApiError(503, "unavailable", "WORKER_TOKEN is not set on this server")
    url = camera.control_url.rstrip("/") + "/control/snapshot"
    headers = {"Authorization": f"Bearer {rt.settings.worker_token.get_secret_value()}"}
    params = {"annotated": "true" if annotated else "false"}
    try:
        async with httpx.AsyncClient(timeout=SNAPSHOT_TIMEOUT_S) as client:
            r = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        log.warning("camera %s: snapshot failed: %s", camera_id, type(e).__name__)
        raise ApiError(503, "unavailable", f"the worker for '{camera_id}' didn't answer") from e
    if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):
        log.warning("camera %s: snapshot answered HTTP %s", camera_id, r.status_code)
        message = (
            "the worker has no frame yet"
            if r.status_code == 503
            else f"the worker for '{camera_id}' answered HTTP {r.status_code}"
        )
        raise ApiError(503, "unavailable", message)
    if len(r.content) > SNAPSHOT_MAX_BYTES:
        raise ApiError(503, "unavailable", "the snapshot is too large")
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
