"""Read/write helpers over a `Session` (data-model.md). Callers own the transaction."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func
from sqlmodel import Session, select

from parking.core.fusion import Change, SlotChange, ZoneChange
from parking.db.models import CameraHealth, SlotState, ZoneState
from parking.messages import CameraHealthMsg


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
