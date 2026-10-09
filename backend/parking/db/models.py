"""Tables for Phases 2, 6 and 7 (data-model.md §1).

Timestamps are tz-aware UTC, stored as fixed-width ISO 8601 text so they sort as strings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Index, String, TypeDecorator
from sqlmodel import Field, SQLModel


class UtcDateTime(TypeDecorator):
    """`datetime` <-> `2026-10-08T18:03:09.123456+00:00` (naive input is taken as UTC)."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> str | None:
        if value is None:
            return None
        ts = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return ts.isoformat(timespec="microseconds")

    def process_result_value(self, value: str | None, dialect) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def _ts(**kw):
    return Field(sa_type=UtcDateTime, **kw)


class ZoneState(SQLModel, table=True):
    """A zone's count, written only when it changes."""

    __tablename__ = "zone_state"
    __table_args__ = (Index("ix_zone_state_zone_id_ts", "zone_id", "ts"),)

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = _ts(index=True)
    zone_id: str
    occupied: int
    free: int
    confidence: float
    source: str  # observation | health | tick | flow | correction | reset | config


class SlotState(SQLModel, table=True):
    """A slot's smoothed state, written only when it flips."""

    __tablename__ = "slot_state"
    __table_args__ = (Index("ix_slot_state_slot_id_ts", "slot_id", "ts"),)

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = _ts()
    camera_id: str
    slot_id: str
    taken: bool


class FlowEvent(SQLModel, table=True):
    __tablename__ = "flow_event"

    event_id: str = Field(primary_key=True)  # from the worker (idempotency)
    ts: datetime = _ts(index=True)
    camera_id: str
    zone_id: str
    direction: str  # in | out
    track_id: int
    confidence: float
    applied: bool  # false if ignored (clamped at 0 or capacity)


class Correction(SQLModel, table=True):
    __tablename__ = "correction"

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = _ts()
    zone_id: str
    old_occupied: int
    new_occupied: int
    actor: str  # admin-token | session:<id> | scheduled-reset
    note: str = ""


class _Rollup(SQLModel):
    """One bucket of a zone's free/occupied counts (`total` is a pseudo-zone)."""

    zone_id: str = Field(primary_key=True)
    bucket_ts: datetime = _ts(primary_key=True)  # start of the minute/hour (UTC)
    free_avg: float  # time-weighted
    free_min: int
    free_max: int
    occupied_avg: float
    samples: int  # minute: count values seen in it; hour: minutes rolled up


class ZoneMinute(_Rollup, table=True):
    __tablename__ = "zone_minute"


class ZoneHour(_Rollup, table=True):
    __tablename__ = "zone_hour"


class CameraHealth(SQLModel, table=True):
    """Latest health per camera; history goes to logs only."""

    __tablename__ = "camera_health"

    camera_id: str = Field(primary_key=True)
    ts: datetime = _ts()
    state: str  # ok | degraded | down
    issue: str | None = None
    fps: float | None = None
    last_frame_age_s: float | None = None
    inference_ms_avg: float | None = None


class PushSubscription(SQLModel, table=True):
    """One browser's Web Push subscription (pseudonymous: no names or emails)."""

    __tablename__ = "push_subscription"

    id: str = Field(primary_key=True)  # uuid4
    endpoint: str = Field(unique=True)
    p256dh: str
    auth: str
    prefs: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    tz: str = "UTC"
    lang: str = "en"
    created_at: datetime = _ts()
    last_seen_at: datetime = _ts()
    on_my_way_until: datetime | None = _ts(default=None)
    on_my_way_sent: int = 0  # pushes in the current on-my-way window (max 6, rules.py)
    last_sent_at: datetime | None = _ts(default=None)
    last_sent_free: int | None = None
    last_sent_level: str | None = None
    failures: int = 0  # consecutive send failures; deleted at 404/410 or >= 5

    def subscription_info(self) -> dict[str, Any]:
        """The `PushSubscription` JSON shape pywebpush expects."""
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


class NotificationLog(SQLModel, table=True):
    """One send attempt. `subscription_id` is kept as plain text (no foreign key) so the log
    survives the subscription being deleted after a 404/410."""

    __tablename__ = "notification_log"

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = _ts(index=True)
    subscription_id: str
    kind: str  # test | on_my_way | schedule | almost_full | admin_alert
    payload: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    status: str  # sent | failed | skipped_quiet
    error: str | None = None


class AdminSession(SQLModel, table=True):
    """An admin login (api.md §4). Only the sha256 of the token is stored."""

    __tablename__ = "admin_session"

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(unique=True)  # sha256 hex
    created_at: datetime = _ts()
    expires_at: datetime = _ts(index=True)
    revoked_at: datetime | None = _ts(default=None)
    ip: str | None = None
    user_agent: str | None = None
