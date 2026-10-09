"""Admin routes (api.md §4): password login to a 7-day session token, logout, and the
`require_admin` dependency every other `/api/admin/*` route uses.

A request is an admin's when `Authorization: Bearer <token>` carries either the static
`ADMIN_TOKEN` from `.env` (scripts) or a session token from `POST /api/admin/login`. Session
tokens are 32 random bytes; the DB keeps only their sha256, so a leaked DB can't log anyone
in. The password is checked against `ADMIN_PASSWORD_HASH` (argon2, from
`parking admin hash-password`); login attempts are limited to 5 / 15 min per IP.

Camera health (P7.2) comes from the latest worker health messages; a snapshot is fetched from
the worker's `/control/snapshot` (api.md §5.2, `cameras[].control_url`, `WORKER_TOKEN`).
The slot/line editor (P7.3) reads and writes `config/slots|lines/<camera>.json`, then asks the
worker to `/control/reload`; the reference frame goes to `/control/save-reference`.
Count corrections (P7.4) set a `flow` zone's count and are kept in the `correction` audit log.
Admin alerts (P7.8): a logged-in admin opts a push subscription in; the open issues are listed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

import anyio
import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from parking.api.deps import (
    ApiError,
    LangDep,
    Runtime,
    RuntimeDep,
    public_limit,
    rate_limit,
    runtime,
)
from parking.api.deps import token_ok as static_token_ok
from parking.config import Camera, LineFile, SlotFile
from parking.core.fusion import NotCorrectableError
from parking.db import repo
from parking.db.engine import session_scope
from parking.db.models import AdminSession, PushSubscription
from parking.messages import ZoneStatus, format_ts

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")

SESSION_TTL = timedelta(days=7)
LOGIN_LIMIT = "5/15 minutes"
TOKEN_BYTES = 32
WORKER_TIMEOUT_S = 5.0
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


def _camera(rt: Runtime, camera_id: str) -> Camera:
    camera = next((c for c in rt.config.cameras if c.id == camera_id), None)
    if camera is None:
        raise ApiError(404, "not_found", f"unknown camera '{camera_id}'")
    return camera


async def _call_worker(
    rt: Runtime, camera: Camera, method: str, path: str, params: dict | None = None
) -> httpx.Response:
    """`<control_url><path>` with `WORKER_TOKEN` (api.md §5.2); no `control_url` / token or
    no answer in 5 s -> 503. The caller looks at the status code."""
    if not camera.control_url:
        raise ApiError(503, "unavailable", f"camera '{camera.id}' has no control_url in lot.yaml")
    if rt.settings.worker_token is None:
        raise ApiError(503, "unavailable", "WORKER_TOKEN is not set on this server")
    url = camera.control_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {rt.settings.worker_token.get_secret_value()}"}
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_S) as client:
            return await client.request(method, url, headers=headers, params=params)
    except httpx.HTTPError as e:
        log.warning("camera %s: %s %s failed: %s", camera.id, method, path, type(e).__name__)
        raise ApiError(503, "unavailable", f"the worker for '{camera.id}' didn't answer") from e


def _worker_message(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return f"HTTP {r.status_code}"


@router.get("/cameras/{camera_id}/snapshot", dependencies=[Depends(public_limit)])
async def snapshot(camera_id: str, _: AdminDep, rt: RuntimeDep, annotated: bool = True) -> Response:
    """The worker's current frame as JPEG (annotated with its last analysis by default).

    Unknown camera 404; no `control_url` / `WORKER_TOKEN`, a worker that doesn't answer in
    5 s or answers with an error -> 503. Never cached (`Cache-Control: no-store`)."""
    camera = _camera(rt, camera_id)
    params = {"annotated": "true" if annotated else "false"}
    r = await _call_worker(rt, camera, "GET", "/control/snapshot", params)
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


# --- slot / line editor (P7.3) ---

ConfigKind = Literal["slots", "lines"]


def _config_path(rt: Runtime, camera: Camera, kind: ConfigKind) -> Path:
    path = camera.slots_file if kind == "slots" else camera.lines_file
    if path is None:
        raise ApiError(404, "not_found", f"'{camera.id}' is a {camera.role} camera: no {kind} file")
    return rt.path(path)


async def _read_config(rt: Runtime, camera_id: str, kind: ConfigKind) -> dict:
    camera = _camera(rt, camera_id)
    path = _config_path(rt, camera, kind)

    def read() -> dict:
        if not path.is_file():
            raise ApiError(404, "not_found", f"camera '{camera_id}' has no {kind} file yet")
        try:
            return json.loads(path.read_text())
        except ValueError as e:
            raise ApiError(409, "conflict", f"{path.name} isn't valid JSON: {e}") from None

    return await anyio.to_thread.run_sync(read)


def _write_config(path: Path, text: str) -> bool:
    """Write atomically; an existing file is kept as `<name>.bak` first. True if backed up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.exists()
    if backup:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return backup


async def _save_config(
    rt: Runtime, admin: Admin, camera: Camera, kind: ConfigKind, data: dict
) -> dict:
    """Write the validated file, then ask the worker to reload it. A worker that can't be
    reached doesn't undo the save: it reads the file when it next starts."""
    path = _config_path(rt, camera, kind)
    backup = await anyio.to_thread.run_sync(_write_config, path, format_config_json(data))
    log.info("camera %s: %s file saved by %s", camera.id, kind, admin.actor)
    try:
        r = await _call_worker(rt, camera, "POST", "/control/reload")
        reloaded = r.status_code == 200
        message = None if reloaded else _worker_message(r)
    except ApiError as e:
        reloaded, message = False, e.message
    if not reloaded:
        log.warning(
            "camera %s: worker reload after the %s save failed: %s", camera.id, kind, message
        )
    return {
        "saved": str(camera.slots_file if kind == "slots" else camera.lines_file),
        "backup": backup,
        "reloaded": reloaded,
        "message": message,
    }


def _bad(message: str) -> ApiError:
    return ApiError(422, "bad_request", message)


@router.get("/cameras/{camera_id}/slots", dependencies=[Depends(public_limit)])
async def get_slots(camera_id: str, _: AdminDep, rt: RuntimeDep) -> dict:
    """The camera's slot file as stored (config.md §2)."""
    return await _read_config(rt, camera_id, "slots")


@router.put("/cameras/{camera_id}/slots", dependencies=[Depends(public_limit)])
async def put_slots(camera_id: str, body: SlotFile, admin: AdminDep, rt: RuntimeDep) -> dict:
    """Validates like `load_slots` plus: the path's camera, its zones, slot ids unique across
    the lot. Writes the file (old one kept as `.bak`), reloads the worker, and updates the
    API's slot ids and capacities (published to the live stream)."""
    camera = _camera(rt, camera_id)
    _config_path(rt, camera, "slots")
    if body.camera_id != camera_id:
        raise _bad(f"camera_id is '{body.camera_id}', expected '{camera_id}'")
    for zone in [s.zone for s in body.slots] + [z.zone for z in body.count_zones]:
        if zone not in camera.zones:
            raise _bad(
                f"zone '{zone}' isn't one of {camera_id}'s zones ({', '.join(camera.zones)})"
            )
    elsewhere = {i for c, f in rt.store.slot_files.items() if c != camera_id for i in f.ids()}
    taken = sorted(set(body.ids()) & elsewhere)
    if taken:
        raise _bad(f"slot id(s) already used by another camera: {', '.join(taken)}")
    result = await _save_config(
        rt, admin, camera, "slots", body.model_dump(mode="json", exclude_none=True)
    )
    await rt.ingestor.slot_file(camera_id, body)
    return {**result, "slots": len(body.slots)}


@router.get("/cameras/{camera_id}/lines", dependencies=[Depends(public_limit)])
async def get_lines(camera_id: str, _: AdminDep, rt: RuntimeDep) -> dict:
    """The camera's line file as stored (config.md §3)."""
    return await _read_config(rt, camera_id, "lines")


@router.put("/cameras/{camera_id}/lines", dependencies=[Depends(public_limit)])
async def put_lines(camera_id: str, body: LineFile, admin: AdminDep, rt: RuntimeDep) -> dict:
    """Validates like `load_lines` (plus the path's camera), writes with a `.bak`, reloads."""
    camera = _camera(rt, camera_id)
    _config_path(rt, camera, "lines")
    if body.camera_id != camera_id:
        raise _bad(f"camera_id is '{body.camera_id}', expected '{camera_id}'")
    return await _save_config(
        rt, admin, camera, "lines", body.model_dump(mode="json", exclude_none=True)
    )


@router.post("/cameras/{camera_id}/reference-frame", dependencies=[Depends(public_limit)])
async def reference_frame(camera_id: str, admin: AdminDep, rt: RuntimeDep) -> dict:
    """The worker saves its current frame as the shift-detection reference (vision.md §6).
    Before its first frame 409; unreachable or any other error 503."""
    camera = _camera(rt, camera_id)
    r = await _call_worker(rt, camera, "POST", "/control/save-reference")
    if r.status_code == 409:
        raise ApiError(409, "conflict", _worker_message(r))
    if r.status_code != 200:
        raise ApiError(503, "unavailable", f"the worker answered: {_worker_message(r)}")
    log.info("camera %s: reference frame saved by %s", camera_id, admin.actor)
    return r.json()


# --- count corrections (P7.4) ---

NOTE_MAX = 200


class CorrectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occupied: int = Field(ge=0)
    note: str = Field(default="", max_length=NOTE_MAX)


@router.post("/zones/{zone_id}/correct", dependencies=[Depends(public_limit)])
async def correct_zone(
    zone_id: str, body: CorrectBody, admin: AdminDep, rt: RuntimeDep, lang: LangDep
) -> ZoneStatus:
    """Sets a `flow` zone's count (the entry/exit counter drifts; slots/count zones are
    measured, so 409). Above the capacity 422. Writes a `correction` row with the caller as
    actor, resets the zone's confidence and publishes the new status to every app."""
    if zone_id not in rt.store.capacity:
        raise ApiError(404, "not_found", f"unknown zone '{zone_id}'")
    capacity = rt.store.capacity[zone_id]
    if body.occupied > capacity:
        raise _bad(f"occupied {body.occupied} is above the zone's capacity ({capacity})")
    try:
        await rt.ingestor.correct(zone_id, body.occupied, admin.actor, body.note.strip())
    except NotCorrectableError as e:
        raise ApiError(409, "conflict", f"{e}: only entry/exit counts can be corrected") from None
    return next(z for z in rt.store.status(lang).zones if z.id == zone_id)


@router.get("/corrections", dependencies=[Depends(public_limit)])
async def corrections(
    _: AdminDep,
    rt: RuntimeDep,
    lang: LangDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    """The audit log, newest first: who, when, old -> new and the note."""

    def load() -> list[dict]:
        with session_scope(rt.engine) as session:
            return [row.model_dump() for row in repo.recent_corrections(session, limit)]

    names = {z.id: z.display_name(lang) for z in rt.config.zones}
    return [
        {
            "id": row["id"],
            "ts": format_ts(row["ts"]),
            "zone_id": row["zone_id"],
            "zone_name": names.get(row["zone_id"], row["zone_id"]),
            "old_occupied": row["old_occupied"],
            "new_occupied": row["new_occupied"],
            "actor": row["actor"],
            "note": row["note"],
        }
        for row in await anyio.to_thread.run_sync(load)
    ]


def format_config_json(value: object) -> str:
    """The slot editor's layout (`formatJson` in tools/slot-editor/editor.js): one top-level
    key per line, one slot per line, so saved files diff well. Whole floats become ints."""

    def inline(v: object) -> str:
        if isinstance(v, list):
            return "[" + ", ".join(inline(x) for x in v) + "]"
        if isinstance(v, dict):
            if not v:
                return "{}"
            return "{ " + ", ".join(f"{json.dumps(k)}: {inline(x)}" for k, x in v.items()) + " }"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return json.dumps(v)

    def fmt(v: object, indent: str) -> str:
        pad = indent + "  "
        if isinstance(v, list):
            if not v or all(not isinstance(x, dict) for x in v):
                return inline(v)
            return "[\n" + ",\n".join(pad + inline(x) for x in v) + f"\n{indent}]"
        if isinstance(v, dict) and v:
            items = ",\n".join(f"{pad}{json.dumps(k)}: {fmt(x, pad)}" for k, x in v.items())
            return "{\n" + items + f"\n{indent}}}"
        return inline(v)

    return fmt(value, "") + "\n"


# --- admin alerts (P7.8, notifications.md §5.1) ---

ENDPOINT_MAX = 2048


class AlertsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1, max_length=ENDPOINT_MAX)
    enabled: bool


def _subscription(rt: Runtime, endpoint: str, enabled: bool | None = None) -> bool | None:
    """The subscription's `admin_alerts` (set to `enabled` first if given); None = unknown."""
    with session_scope(rt.engine) as session:
        query = select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        row = session.exec(query).first()
        if row is None:
            return None
        if enabled is not None:
            row.admin_alerts = enabled
            row.last_seen_at = rt.store.clock.now()
        return row.admin_alerts


@router.get("/alerts", dependencies=[Depends(public_limit)])
async def alerts(
    _: AdminDep,
    rt: RuntimeDep,
    endpoint: Annotated[str | None, Query(max_length=ENDPOINT_MAX)] = None,
) -> dict:
    """Whether this browser's subscription gets admin alerts (`enabled`, false for an unknown
    or missing endpoint), whether push is on (`available`) and the open issues."""
    enabled = await anyio.to_thread.run_sync(_subscription, rt, endpoint) if endpoint else None
    now = rt.store.clock.now()
    issues = rt.alerts.tracker.issues(now) if rt.alerts is not None else []
    for issue in issues:
        issue["since"] = format_ts(issue["since"])
        if issue["last_alert_at"] is not None:
            issue["last_alert_at"] = format_ts(issue["last_alert_at"])
    return {"enabled": bool(enabled), "available": rt.alerts is not None, "issues": issues}


@router.put("/alerts", dependencies=[Depends(public_limit)])
async def set_alerts(body: AlertsBody, admin: AdminDep, rt: RuntimeDep) -> dict:
    """Turns admin alerts on or off for a push subscription (this browser's); 404 unknown."""
    enabled = await anyio.to_thread.run_sync(_subscription, rt, body.endpoint, body.enabled)
    if enabled is None:
        raise ApiError(404, "not_found", "unknown subscription endpoint")
    log.info("admin alerts %s for a subscription by %s", "on" if enabled else "off", admin.actor)
    return {"enabled": enabled}
