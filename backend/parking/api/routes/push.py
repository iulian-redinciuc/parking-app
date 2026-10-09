"""Web Push routes (api.md §2, notifications.md §2): the VAPID public key and the
subscription store, plus "I'm on my way" and a test push.

A subscription is found by its `endpoint` (the browser's push URL, unique and unguessable);
every call with an endpoint updates `last_seen_at`. POSTs share the push budget (20/hour per
IP), the test push has its own 3/hour per endpoint; GET, PATCH and DELETE use the public budget.
Without VAPID keys in `.env` the key and the sending routes answer 503.
"""

from __future__ import annotations

import math
import uuid
from datetime import timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from fastapi import APIRouter, Depends, Request, Response
from limits import parse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import select

from parking.api.deps import ApiError, RateLimiter, Runtime, RuntimeDep, public_limit, rate_limit
from parking.db.engine import session_scope
from parking.db.models import PushSubscription
from parking.messages import format_ts
from parking.push.payload import app_url, status_payload
from parking.push.sender import PushSender, SendResult, Urgency

router = APIRouter(prefix="/api/push")

PUSH_POST = "20/hour"
TEST_PER_ENDPOINT = parse("3/hour")
push_limit = rate_limit(PUSH_POST, "push")

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
B64URL = r"^[A-Za-z0-9_-]+={0,2}$"


# --- bodies (api.md §2) ---


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Schedule(Body):
    days: list[Annotated[int, Field(ge=1, le=7)]] = Field(min_length=1, max_length=7)
    time: str = Field(pattern=HHMM)

    @field_validator("days")
    @classmethod
    def _sorted_unique(cls, days: list[int]) -> list[int]:
        return sorted(set(days))


class QuietHours(Body):
    from_: str = Field(alias="from", pattern=HHMM)
    to: str = Field(pattern=HHMM)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Prefs(Body):
    """Notification preferences, all optional. `radius_m: null` = the lot's `notify_radius_m`,
    `zones: null` = every zone; zone ids are checked against lot.yaml by the routes."""

    proximity: bool = False
    radius_m: int | None = Field(default=None, ge=100, le=5000)
    zones: list[str] | None = Field(default=None, max_length=20)
    schedules: list[Schedule] = Field(default_factory=list, max_length=10)
    quiet_hours: QuietHours | None = None
    alert_when_almost_full: bool = False

    def stored(self) -> dict:
        return self.model_dump(by_alias=True)


class Keys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200, pattern=B64URL)
    auth: str = Field(min_length=1, max_length=100, pattern=B64URL)


Endpoint = Annotated[str, Field(min_length=12, max_length=2048, pattern=r"^https://\S+$")]


class SubscriptionJson(BaseModel):
    """The browser's `PushSubscription.toJSON()`; `expirationTime` and the like are ignored."""

    endpoint: Endpoint
    keys: Keys


def _check_tz(tz: str) -> str:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown IANA time zone {tz!r}") from e
    return tz


class SubscribeBody(Body):
    subscription: SubscriptionJson
    prefs: Prefs = Field(default_factory=Prefs)
    tz: str = Field(default="UTC", min_length=1, max_length=64)
    lang: str = Field(default="en", min_length=2, max_length=35, pattern=r"^[A-Za-z]{2,8}(-\w+)*$")

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, tz: str) -> str:
        return _check_tz(tz)


class PrefsBody(Body):
    endpoint: Endpoint
    prefs: Prefs


class EndpointBody(Body):
    endpoint: Endpoint


class OnMyWayBody(Body):
    endpoint: Endpoint
    minutes: int = Field(description="5–120, or 0 to cancel")

    @field_validator("minutes")
    @classmethod
    def _range(cls, minutes: int) -> int:
        if minutes != 0 and not 5 <= minutes <= 120:
            raise ValueError("minutes must be 0 (cancel) or 5–120")
        return minutes


# --- helpers ---


def _check_zones(rt: Runtime, prefs: Prefs) -> None:
    known = {z.id for z in rt.config.zones}
    unknown = sorted(set(prefs.zones or ()) - known)
    if unknown:
        loc = ["body", "prefs", "zones"]
        msg = f"unknown zone(s): {', '.join(unknown)}"
        details = [{"type": "value_error", "loc": loc, "msg": msg}]
        raise ApiError(422, "bad_request", f"prefs.zones: {msg}", details)


def _sender(rt: Runtime) -> PushSender:
    if rt.push is None:
        raise ApiError(503, "unavailable", "push is not configured on this server")
    return rt.push


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "unknown subscription endpoint")


def _touch(rt: Runtime, endpoint: str, **changes) -> PushSubscription | None:
    """Set `last_seen_at` (and `changes`) on the subscription; a detached copy, or None."""
    with session_scope(rt.engine) as session:
        sub = session.exec(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
        sub = sub.first()
        if sub is None:
            return None
        for name, value in {"last_seen_at": rt.store.clock.now(), **changes}.items():
            setattr(sub, name, value)
        session.flush()
        session.refresh(sub)
        session.expunge(sub)
        return sub


async def _send(rt: Runtime, sub: PushSubscription, kind: str, urgency: Urgency) -> SendResult:
    sender = _sender(rt)
    status = rt.store.status(sub.lang) if rt.store.has_data else None
    zones = (sub.prefs or {}).get("zones")
    payload = status_payload(status, kind, app_url(rt.settings.public_app_url), sub.tz, zones)
    return await anyio.to_thread.run_sync(sender.send, sub, payload, urgency)


# --- routes ---


@router.get("/vapid-public-key", dependencies=[Depends(public_limit)])
async def vapid_public_key(rt: RuntimeDep) -> dict:
    """The key for the browser's `applicationServerKey`."""
    if not rt.settings.vapid_public_key:
        raise ApiError(503, "unavailable", "push is not configured on this server")
    return {"key": rt.settings.vapid_public_key}


@router.post("/subscriptions", status_code=201, dependencies=[Depends(push_limit)])
async def subscribe(body: SubscribeBody, rt: RuntimeDep) -> dict:
    """Create the subscription, or update the one with this endpoint (same id)."""
    _check_zones(rt, body.prefs)
    sub = body.subscription
    now = rt.store.clock.now()

    def upsert() -> str:
        with session_scope(rt.engine) as session:
            query = select(PushSubscription).where(PushSubscription.endpoint == sub.endpoint)
            row = session.exec(query).first()
            if row is None:
                row = PushSubscription(
                    id=str(uuid.uuid4()), endpoint=sub.endpoint, p256dh="", auth="", created_at=now
                )
                session.add(row)
            row.p256dh, row.auth = sub.keys.p256dh, sub.keys.auth
            row.prefs, row.tz, row.lang = body.prefs.stored(), body.tz, body.lang
            row.last_seen_at, row.failures = now, 0
            return row.id

    return {"id": await anyio.to_thread.run_sync(upsert)}


@router.patch("/subscriptions", dependencies=[Depends(public_limit)])
async def update_prefs(body: PrefsBody, rt: RuntimeDep) -> dict:
    """Replace the preferences of a subscription."""
    _check_zones(rt, body.prefs)
    prefs = body.prefs.stored()
    sub = await anyio.to_thread.run_sync(lambda: _touch(rt, body.endpoint, prefs=prefs))
    if sub is None:
        raise _not_found()
    return {"id": sub.id, "prefs": sub.prefs}


@router.delete("/subscriptions", status_code=204, dependencies=[Depends(public_limit)])
async def unsubscribe(body: EndpointBody, rt: RuntimeDep) -> Response:
    """Forget a subscription. Idempotent: an unknown endpoint is a 204 too."""

    def delete() -> None:
        with session_scope(rt.engine) as session:
            query = select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
            for row in session.exec(query):
                session.delete(row)

    await anyio.to_thread.run_sync(delete)
    return Response(status_code=204)


@router.post("/on-my-way", status_code=202, dependencies=[Depends(push_limit)])
async def on_my_way(body: OnMyWayBody, rt: RuntimeDep) -> dict:
    """Push updates for `minutes` (notifications.md §4), starting with one now; 0 cancels."""
    if body.minutes:
        _sender(rt)  # 503 before changing anything
    until = rt.store.clock.now() + timedelta(minutes=body.minutes) if body.minutes else None
    sub = await anyio.to_thread.run_sync(lambda: _touch(rt, body.endpoint, on_my_way_until=until))
    if sub is None:
        raise _not_found()
    if until is None:
        return {"until": None, "sent": False}
    result = await _send(rt, sub, "on_my_way", "high")
    return {"until": format_ts(until), "sent": result.ok}


@router.post("/test", dependencies=[Depends(push_limit)])
async def test_push(body: EndpointBody, rt: RuntimeDep, request: Request) -> dict:
    """One test push with the current status; 3/hour per endpoint."""
    _sender(rt)
    sub = await anyio.to_thread.run_sync(lambda: _touch(rt, body.endpoint))
    if sub is None:
        raise _not_found()
    limiter: RateLimiter = request.app.state.rate_limiter
    wait = limiter.hit(TEST_PER_ENDPOINT, "push-test", sub.id)
    if wait is not None:
        retry = str(max(math.ceil(wait), 1))
        headers = {"Retry-After": retry}
        raise ApiError(429, "rate_limited", "too many test pushes (3/hour)", headers=headers)
    result = await _send(rt, sub, "test", "normal")
    return {"sent": result.ok, "deleted": result.deleted}
