"""Tables for Phase 2 (data-model.md §1). Push and admin tables arrive with Phases 6–7.

Timestamps are tz-aware UTC, stored as fixed-width ISO 8601 text so they sort as strings.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Index, String, TypeDecorator
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
    source: str  # observation | health | tick | flow | correction | reset


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
