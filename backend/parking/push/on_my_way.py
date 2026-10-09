"""Tier 2 dispatch (notifications.md §4): on every status change, push to the subscriptions in
an "I'm on my way" window that `rules.should_send_on_my_way` lets through.

`Ingestor` calls `status_changed(old, new)` with its lock held; the DB query and the sends run
in a worker thread behind this class's own lock, so ingest never waits for a push service and
two changes never race on `last_sent_*`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime

import anyio
from sqlalchemy import Engine
from sqlmodel import select

from parking.config import LotConfig
from parking.core.clock import Clock
from parking.db.engine import session_scope
from parking.db.models import PushSubscription
from parking.messages import LotStatus
from parking.push.payload import status_payload
from parking.push.rules import should_send_on_my_way, watched
from parking.push.sender import PushSender, SendResult

log = logging.getLogger(__name__)

KIND = "on_my_way"


def localized(status: LotStatus, config: LotConfig, lang: str) -> LotStatus:
    """`status` with the zone names in `lang` (the store builds it in English)."""
    zones = [
        z.model_copy(update={"name": config.zone(z.id).display_name(lang)}) for z in status.zones
    ]
    return status.model_copy(update={"zones": zones})


def counts_changed(old: LotStatus, new: LotStatus) -> bool:
    """Only free counts and levels feed the rules; trend/confidence-only changes don't."""
    return [(z.id, z.free, z.level) for z in old.zones] != [
        (z.id, z.free, z.level) for z in new.zones
    ]


def record_sent(engine: Engine, sub: PushSubscription, status: LotStatus | None, config) -> None:
    """After a delivered on-my-way push: remember what it showed and count it in the window."""
    w = watched(sub, status, config.api.levels) if status is not None else None
    with session_scope(engine) as session:
        row = session.get(PushSubscription, sub.id)
        if row is None:
            return
        row.last_sent_free = w.free if w else None
        row.last_sent_level = w.level if w else None
        row.on_my_way_sent += 1


class OnMyWayNotifier:
    def __init__(
        self, engine: Engine, sender: PushSender, config: LotConfig, clock: Clock, url: str
    ):
        self.engine = engine
        self.sender = sender
        self.config = config
        self.clock = clock
        self.url = url
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def status_changed(self, old: LotStatus | None, new: LotStatus) -> None:
        """Ingest hook (no `old` = start-up restore, not a change)."""
        if old is None or not counts_changed(old, new):
            return
        task = asyncio.get_running_loop().create_task(self._run(old, new))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, old: LotStatus, new: LotStatus) -> None:
        async with self._lock:
            try:
                await anyio.to_thread.run_sync(self.dispatch, old, new, self.clock.now())
            except Exception:
                log.exception("on-my-way dispatch failed")

    async def drain(self) -> None:
        """Wait for the pending dispatches (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def dispatch(self, old: LotStatus, new: LotStatus, now: datetime) -> list[SendResult]:
        """Query the active windows, apply the rules, send, update `last_sent_*`."""
        with session_scope(self.engine) as session:
            query = select(PushSubscription).where(PushSubscription.on_my_way_until > now)
            subs = list(session.exec(query))
            for sub in subs:
                session.expunge(sub)
        levels = self.config.api.levels
        due = [s for s in subs if should_send_on_my_way(s, old, new, now, levels)]
        # one payload per (lang, tz, zones): send_many shares it
        groups: dict[tuple, list[PushSubscription]] = defaultdict(list)
        for sub in due:
            zones = (sub.prefs or {}).get("zones")
            groups[(sub.lang, sub.tz, tuple(zones) if zones else None)].append(sub)
        results = []
        for (lang, tz, zones), group in groups.items():
            payload = status_payload(
                localized(new, self.config, lang), KIND, self.url, tz, list(zones or ())
            )
            outcome = self.sender.send_many(group, payload, "high")
            for sub, result in zip(group, outcome, strict=True):
                if result.ok:
                    record_sent(self.engine, sub, new, self.config)
                results.append(result)
        if due:
            sent = sum(r.ok for r in results)
            log.info("on my way: %d of %d active window(s) due, %d sent", len(due), len(subs), sent)
        return results
