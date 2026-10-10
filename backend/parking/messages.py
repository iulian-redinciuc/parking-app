"""Payload models shared by the workers and the API (api.md §1, §5).

Every model ignores unknown fields, so an older reader accepts a newer writer's payload.
Timestamps are timezone-aware UTC and serialise as ISO 8601 with `Z` (naive input = UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer


def _to_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def format_ts(ts: datetime) -> str:
    """`2026-10-07T17:05:12.120Z`: UTC, millisecond precision."""
    return _to_utc(ts).isoformat(timespec="milliseconds").replace("+00:00", "Z")


UtcDatetime = Annotated[
    datetime, AfterValidator(_to_utc), PlainSerializer(format_ts, return_type=str)
]

Level = Literal["plenty", "filling", "almost_full", "full"]
Trend = Literal["filling", "emptying", "steady"]
ZoneMethod = Literal["slots", "count", "flow"]
CameraState = Literal["ok", "degraded", "down"]
CameraIssue = Literal["black", "frozen", "blurry", "shifted", "connect_failed"]
Direction = Literal["in", "out"]
# slot types counted separately (`by_type`); `standard` spaces only show in the zone's totals
SpaceType = Literal["accessible", "ev", "motorcycle", "reserved"]
SPACE_TYPES: tuple[SpaceType, ...] = ("accessible", "ev", "motorcycle", "reserved")


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- workers -> API (api.md §5.1) ---


class SlotScore(Message):
    id: str
    score: float = Field(ge=0.0, le=1.0)
    taken: bool


class Observation(Message):
    """One analysed frame from an occupancy worker (`POST /internal/observations`)."""

    v: Literal[1] = 1
    camera_id: str
    ts: UtcDatetime
    frame_size: tuple[int, int]
    inference_ms: float = Field(ge=0)
    detections: int = Field(ge=0)
    slots: list[SlotScore] = []
    zone_counts: dict[str, int] = {}


class FlowEventMsg(Message):
    """One line crossing from a flow worker. `event_id` makes retries idempotent."""

    v: Literal[1] = 1
    event_id: str = Field(min_length=1)
    camera_id: str
    ts: UtcDatetime
    direction: Direction
    track_id: int
    cls: str
    confidence: float = Field(ge=0.0, le=1.0)


class FlowEventBatch(Message):
    """Body of `POST /internal/flow-events`: 1–100 events."""

    events: list[FlowEventMsg] = Field(min_length=1, max_length=100)


class FlowEventAck(Message):
    """Response of `POST /internal/flow-events`."""

    accepted: int = Field(ge=0)
    duplicates: int = Field(ge=0)


class CameraHealthMsg(Message):
    """Sent by every worker every 10 s (`POST /internal/health`)."""

    v: Literal[1] = 1
    camera_id: str
    ts: UtcDatetime
    state: CameraState
    issue: CameraIssue | None = None
    fps: float | None = Field(default=None, ge=0)
    last_frame_age_s: float | None = Field(default=None, ge=0)
    inference_ms_avg: float | None = Field(default=None, ge=0)
    unhealthy_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    # flow cameras: share of the last 10 s of frames the motion gate let through
    gate_active_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    # when this worker process started; a new value = it was restarted (`restarts` in /healthz)
    started_at: UtcDatetime | None = None
    # the worker's machine (P8.7 admin alerts): used share of the disk holding `data/`, and the
    # CPU temperature where the machine reports one
    disk_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    cpu_temp_c: float | None = None


# --- API -> clients (api.md §1) ---


class Totals(Message):
    capacity: int = Field(ge=0)
    occupied: int = Field(ge=0)
    free: int = Field(ge=0)
    level: Level
    confidence: float = Field(ge=0.0, le=1.0)
    stale: bool


class TypeCount(Message):
    """The spaces of one type in a zone; they are part of the zone's own counts too."""

    capacity: int = Field(ge=0)
    free: int = Field(ge=0)


class ZoneStatus(Message):
    id: str
    name: str
    method: ZoneMethod
    capacity: int = Field(ge=0)
    occupied: int = Field(ge=0)
    free: int = Field(ge=0)
    level: Level
    confidence: float = Field(ge=0.0, le=1.0)
    stale: bool
    trend: Trend
    updated_at: UtcDatetime | None = None
    slots: dict[str, bool] | None = None
    # special spaces per type, only for `slots` zones (`{}` = the zone has none)
    by_type: dict[SpaceType, TypeCount] | None = None


class LotStatus(Message):
    """`GET /api/status` and every SSE `status` event."""

    v: Literal[1] = 1
    lot: str
    updated_at: UtcDatetime
    total: Totals
    zones: list[ZoneStatus]
