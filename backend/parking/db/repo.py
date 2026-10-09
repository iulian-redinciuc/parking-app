"""Read/write helpers over a `Session` (data-model.md). Callers own the transaction."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func
from sqlmodel import Session, select

from parking.core.fusion import Change, SlotChange, ZoneChange
from parking.db.models import CameraHealth, Correction, FlowEvent, SlotState, ZoneState
from parking.messages import CameraHealthMsg, FlowEventMsg


def record_changes(session: Session, changes: Iterable[Change]) -> int:
    """Add a `slot_state` row per slot flip and a `zone_state` row per zone whose count moved.

    `startup` changes only republish what was restored from these tables, so they're skipped.
    Returns the number of rows added.
    """
    rows: list[SlotState | ZoneState] = []
    for change in changes:
        if isinstance(change, SlotChange):
            rows.append(
                SlotState(
                    ts=change.ts,
                    camera_id=change.camera_id,
                    slot_id=change.slot_id,
                    taken=change.taken,
                )
            )
        elif isinstance(change, ZoneChange) and change.count_changed:
            if change.source == "startup":
                continue
            rows.append(
                ZoneState(
                    ts=change.ts,
                    zone_id=change.zone_id,
                    occupied=change.occupied,
                    free=change.free,
                    confidence=change.confidence,
                    source=change.source,
                )
            )
    session.add_all(rows)
    return len(rows)


def latest_zone_states(session: Session) -> dict[str, ZoneState]:
    """The newest `zone_state` row per zone."""
    newest = select(func.max(ZoneState.id)).group_by(ZoneState.zone_id)
    rows = session.exec(select(ZoneState).where(ZoneState.id.in_(newest)))
    return {row.zone_id: row for row in rows}


def latest_slot_states(session: Session) -> dict[str, dict[str, bool]]:
    """camera id -> slot id -> taken, from the newest `slot_state` row per slot."""
    newest = select(func.max(SlotState.id)).group_by(SlotState.camera_id, SlotState.slot_id)
    out: dict[str, dict[str, bool]] = {}
    for row in session.exec(select(SlotState).where(SlotState.id.in_(newest))):
        out.setdefault(row.camera_id, {})[row.slot_id] = row.taken
    return out


def zone_samples_since(session: Session, since: datetime) -> dict[str, list[tuple[datetime, int]]]:
    """zone id -> (ts, free) of its `zone_state` rows from `since` on, oldest first (trend)."""
    out: dict[str, list[tuple[datetime, int]]] = {}
    rows = session.exec(select(ZoneState).where(ZoneState.ts >= since).order_by(ZoneState.id))
    for row in rows:
        out.setdefault(row.zone_id, []).append((row.ts, row.free))
    return out


def known_flow_event_ids(session: Session, event_ids: Iterable[str]) -> set[str]:
    """The ids among `event_ids` that already have a `flow_event` row (retried deliveries)."""
    ids = list(event_ids)
    if not ids:
        return set()
    return set(session.exec(select(FlowEvent.event_id).where(FlowEvent.event_id.in_(ids))))


def add_flow_event(session: Session, msg: FlowEventMsg, zone_id: str, applied: bool) -> None:
    """One `flow_event` row; `applied=False` when the counter clamped it."""
    session.add(
        FlowEvent(
            event_id=msg.event_id,
            ts=msg.ts,
            camera_id=msg.camera_id,
            zone_id=zone_id,
            direction=msg.direction,
            track_id=msg.track_id,
            confidence=msg.confidence,
            applied=applied,
        )
    )


def upsert_camera_health(session: Session, msg: CameraHealthMsg) -> CameraHealth:
    """Insert or replace the single `camera_health` row of `msg.camera_id`."""
    fields = ("ts", "state", "issue", "fps", "last_frame_age_s", "inference_ms_avg")
    values = {key: getattr(msg, key) for key in fields}
    row = session.get(CameraHealth, msg.camera_id)
    if row is None:
        row = CameraHealth(camera_id=msg.camera_id, **values)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    session.add(row)
    return row


def camera_health(session: Session) -> dict[str, CameraHealth]:
    """Every camera's latest health row."""
    return {row.camera_id: row for row in session.exec(select(CameraHealth))}


def recent_corrections(session: Session, limit: int) -> list[Correction]:
    """The newest `limit` corrections, newest first."""
    query = select(Correction).order_by(Correction.id.desc()).limit(limit)
    return list(session.exec(query))


def correction_counters(
    session: Session, zone_ids: Iterable[str]
) -> dict[str, tuple[datetime, int]]:
    """zone id -> (time of its last correction, `flow_event` rows since), for the flow
    confidence after a restart (data-model.md §2). Zones never corrected are left out."""
    out: dict[str, tuple[datetime, int]] = {}
    for zone_id in zone_ids:
        last = session.exec(
            select(Correction)
            .where(Correction.zone_id == zone_id)
            .order_by(Correction.id.desc())
            .limit(1)
        ).first()
        if last is None:
            continue
        events = session.exec(
            select(func.count())
            .select_from(FlowEvent)
            .where(FlowEvent.zone_id == zone_id, FlowEvent.ts > last.ts)
        ).one()
        out[zone_id] = (last.ts, int(events))
    return out
