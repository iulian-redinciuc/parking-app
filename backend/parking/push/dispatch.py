"""What the push notifiers share (notifications.md §4-5): one payload per (lang, tz, zones), and
the ingest hook that runs a dispatch off the event loop.

`Ingestor` calls `status_changed(old, new)` with its lock held; the DB queries and the sends run
in a worker thread behind a lock that every notifier and the scheduler share, so ingest never
waits for a push service and two dispatches never race on the same subscription row.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime

import anyio
from sqlalchemy import Engine

from parking.config import LotConfig
from parking.core.clock import Clock
from parking.db.models import PushSubscription
from parking.messages import LotStatus
from parking.push.payload import status_payload
from parking.push.sender import PushSender, SendResult, Urgency

log = logging.getLogger(__name__)


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


def send_status(
    sender: PushSender,
    config: LotConfig,
    subs: list[PushSubscription],
    status: LotStatus | None,
    kind: str,
    url: str,
    urgency: Urgency,
) -> list[tuple[PushSubscription, SendResult]]:
    """The status push to each of `subs`, one payload per (lang, tz, zones) so `send_many`
    shares it; `(sub, result)` pairs."""
    groups: dict[tuple, list[PushSubscription]] = defaultdict(list)
    for sub in subs:
        zones = (sub.prefs or {}).get("zones")
        groups[(sub.lang, sub.tz, tuple(zones) if zones else None)].append(sub)
    out = []
    for (lang, tz, zones), group in groups.items():
        shown = localized(status, config, lang) if status is not None else None
        payload = status_payload(shown, kind, url, tz, list(zones or ()))
        out.extend(zip(group, sender.send_many(group, payload, urgency), strict=True))
    return out


class StatusNotifier:
    """Base of the ingest hooks: `dispatch(old, new, now)` runs in a worker thread for every
    change of a free count or level (none for the start-up restore)."""

    name = "push"

    def __init__(
        self,
        engine: Engine,
        sender: PushSender,
        config: LotConfig,
        clock: Clock,
        url: str,
        lock: asyncio.Lock | None = None,
    ):
        self.engine = engine
        self.sender = sender
        self.config = config
        self.clock = clock
        self.url = url
        self._lock = lock or asyncio.Lock()
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
                log.exception("%s dispatch failed", self.name)

    async def drain(self) -> None:
        """Wait for the pending dispatches (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def dispatch(self, old: LotStatus, new: LotStatus, now: datetime) -> list[SendResult]:
        raise NotImplementedError
