"""Applies worker payloads to the `StateStore`, records the changes and publishes the status
(SSE, then the on-my-way push hook).

Every input goes through one `asyncio.Lock`, so changes are applied, written and published in
the order they arrived. DB writes run in a worker thread (`anyio.to_thread`); a DB error is
logged and counted but never loses the in-memory state or crashes the API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import anyio
from sqlalchemy import Engine

from parking.core.fusion import Change, SlotChange, StateStore, ZoneChange
from parking.db import repo
from parking.db.engine import session_scope
from parking.messages import (
    CameraHealthMsg,
    FlowEventAck,
    FlowEventBatch,
    FlowEventMsg,
    LotStatus,
    Observation,
)

log = logging.getLogger(__name__)

# no health message for this long -> the camera is `down` (api.md §5.1, crashed worker)
HEALTH_TIMEOUT = timedelta(seconds=30)

Publish = Callable[[LotStatus], None]
# (previous status or None at start-up, new status); called with the ingest lock held
StatusHook = Callable[[LotStatus | None, LotStatus], None]


@dataclass
class IngestStats:
    """Counters shown in `/healthz`."""

    observations: int = 0
    flow_events: int = 0
    health: int = 0
    rejected: int = 0  # bad payloads (422)
    db_errors: int = 0


@dataclass
class _Pending:
    changes: list[Change] = field(default_factory=list)
    flow: list[tuple[FlowEventMsg, str, bool]] = field(default_factory=list)
    health: list[CameraHealthMsg] = field(default_factory=list)


class Ingestor:
    """The API's single writer: worker payload -> `StateStore` -> DB -> `publish(status)`."""

    def __init__(
        self,
        store: StateStore,
        engine: Engine,
        publish: Publish | None = None,
        on_status: StatusHook | None = None,
    ):
        self.store = store
        self.engine = engine
        self.publish = publish
        self.on_status = on_status  # the push rules (on-my-way, almost-full)
        self._last_status: LotStatus | None = None
        self.stats = IngestStats()
        self._lock = asyncio.Lock()
        # camera id -> store-clock time its last health message arrived
        self._health_seen: dict[str, datetime] = {}

    # --- start-up ---

    async def restore(self) -> None:
        """Seed the store from the DB (data-model.md §2); restored zones stay stale."""
        since = self.store.clock.now() - self.store.trend_window
        slots, zones, samples = await anyio.to_thread.run_sync(self._load, since)
        counts = {
            zone_id: row.occupied
            for zone_id, row in zones.items()
            if zone_id in self.store.capacity and self.store.config.zone(zone_id).method != "slots"
        }
        updated = {z: row.ts for z, row in zones.items() if z in self.store.capacity}
        trend = {z: s for z, s in samples.items() if z in self.store.capacity}
        async with self._lock:
            changes = self.store.restore(slots, counts, updated, trend)
            self._log(changes)
            self._publish()
        if updated:
            log.info("restored %d zone(s) from the database (stale until fresh data)", len(updated))

    def _load(self, since: datetime):
        with session_scope(self.engine) as session:
            slots = repo.latest_slot_states(session)
            zones = repo.latest_zone_states(session)
            for row in zones.values():
                session.expunge(row)
            return slots, zones, repo.zone_samples_since(session, since)

    # --- inputs ---

    async def observation(self, obs: Observation) -> None:
        async with self._lock:
            changes = self.store.apply_observation(obs)
            self.stats.observations += 1
            await self._commit(_Pending(changes=changes))

    async def flow_events(self, batch: FlowEventBatch) -> FlowEventAck:
        """Apply new events in order; ids seen before (memory or DB) count as duplicates."""
        for msg in batch.events:
            self.store.flow_zone(msg.camera_id)  # unknown camera -> reject the whole batch
        async with self._lock:
            ids = [msg.event_id for msg in batch.events]
            known = await anyio.to_thread.run_sync(self._known_ids, ids)
            pending = _Pending()
            accepted = duplicates = 0
            for msg in batch.events:
                if msg.event_id in known:
                    duplicates += 1
                    continue
                known.add(msg.event_id)
                applied, changes = self.store.apply_flow_event(msg)
                if applied is None:
                    duplicates += 1
                    continue
                accepted += 1
                pending.changes += changes
                pending.flow.append((msg, self.store.flow_zone(msg.camera_id), applied))
            self.stats.flow_events += accepted
            await self._commit(pending)
        return FlowEventAck(accepted=accepted, duplicates=duplicates)

    def _known_ids(self, ids: list[str]) -> set[str]:
        with session_scope(self.engine) as session:
            return repo.known_flow_event_ids(session, ids)

    async def health(self, msg: CameraHealthMsg) -> None:
        async with self._lock:
            before = self.store.health.get(msg.camera_id)
            changes = self.store.apply_health(msg)
            self._health_seen[msg.camera_id] = self.store.clock.now()
            self.stats.health += 1
            if before is None or (before.state, before.issue) != (msg.state, msg.issue):
                issue = f" ({msg.issue})" if msg.issue else ""
                log.info("camera %s: %s%s", msg.camera_id, msg.state, issue)
            await self._commit(_Pending(changes=changes, health=[msg]))

    async def tick(self) -> None:
        """Once a second: cameras silent for 30 s go `down`, zones go stale, trends move."""
        async with self._lock:
            now = self.store.clock.now()
            pending = _Pending()
            for camera_id, seen in self._health_seen.items():
                last = self.store.health.get(camera_id)
                if last is None or last.state == "down" or now - seen <= HEALTH_TIMEOUT:
                    continue
                silent = int((now - seen).total_seconds())
                log.warning("camera %s: no health message for %d s -> down", camera_id, silent)
                msg = CameraHealthMsg(camera_id=camera_id, ts=now, state="down")
                pending.changes += self.store.apply_health(msg)
                pending.health.append(msg)
            pending.changes += self.store.tick()
            await self._commit(pending)

    # --- output ---

    async def _commit(self, pending: _Pending) -> None:
        """Write the changes, log them and publish the new status (called with the lock)."""
        if not (pending.changes or pending.flow or pending.health):
            return
        try:
            await anyio.to_thread.run_sync(self._write, pending)
        except Exception:
            self.stats.db_errors += 1
            log.exception("database write failed; the in-memory state is kept")
        self._log(pending.changes)
        if pending.changes:
            self._publish()

    def _write(self, pending: _Pending) -> None:
        with session_scope(self.engine) as session:
            for msg, zone_id, applied in pending.flow:
                repo.add_flow_event(session, msg, zone_id, applied)
            repo.record_changes(session, pending.changes)
            for msg in pending.health:
                repo.upsert_camera_health(session, msg)

    def _publish(self) -> None:
        status = self.store.status()
        if self.publish is not None:
            self.publish(status)
        if self.on_status is not None:
            try:
                self.on_status(self._last_status, status)
            except Exception:
                log.exception("status hook failed")
        self._last_status = status

    @staticmethod
    def _log(changes: Iterable[Change]) -> None:
        for change in changes:
            if isinstance(change, ZoneChange):
                log.info(
                    "zone %s: %d free, %d occupied (%s, trend %s, confidence %.2f%s) [%s]",
                    change.zone_id,
                    change.free,
                    change.occupied,
                    change.level,
                    change.trend,
                    change.confidence,
                    ", stale" if change.stale else "",
                    change.source,
                )
            elif isinstance(change, SlotChange):
                log.debug(
                    "slot %s/%s: %s",
                    change.camera_id,
                    change.slot_id,
                    "taken" if change.taken else "free",
                )
