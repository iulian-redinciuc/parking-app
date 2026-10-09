"""Tier 3 (notifications.md §5): scheduled reminders and almost-full alerts.

`PushScheduler` is an APScheduler `AsyncIOScheduler` started in the API lifespan; its job runs
at second 0 of every minute and sends the status push (`kind: schedule`, `Urgency: normal`) to
the subscriptions with a reminder due since the previous run, in each subscription's tz
(`rules.schedule_instants`). A run late by more than 5 minutes drops the older reminders.

`AlmostFullNotifier` is an ingest hook: when a watched zone gets worse into `almost_full` or
`full`, it pushes (`kind: almost_full`, `Urgency: high`) to the subscriptions with
`prefs.alert_when_almost_full`, at most once per 2 h each (counted from `notification_log`).

Both skip quiet hours and log the skipped pushes as `skipped_quiet`. Neither touches
`last_sent_free` / `last_sent_level` (those are the on-my-way rules' "last push"); the sender's
`last_sent_at` is what the 10-minute reminder gap looks at.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

import anyio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Engine, func
from sqlmodel import select

from parking.config import LotConfig
from parking.core.clock import Clock
from parking.db.engine import session_scope
from parking.db.models import NotificationLog, PushSubscription
from parking.messages import LotStatus
from parking.push.dispatch import StatusNotifier, send_status
from parking.push.rules import (
    ALMOST_FULL_COOLDOWN,
    SCHEDULE_CATCH_UP,
    almost_full_zones,
    in_quiet_hours,
    schedule_instants,
    should_send_almost_full,
    should_send_schedule,
)
from parking.push.sender import PushSender, SendResult

log = logging.getLogger(__name__)

SCHEDULE = "schedule"
ALMOST_FULL = "almost_full"
JOB_MISFIRE_S = 30


def _subscriptions(engine: Engine, wanted) -> list[PushSubscription]:
    """Every subscription whose prefs pass `wanted` (prefs are JSON: filtered here)."""
    with session_scope(engine) as session:
        subs = [s for s in session.exec(select(PushSubscription)) if wanted(s.prefs or {})]
        for sub in subs:
            session.expunge(sub)
    return subs


def _log_skipped(engine: Engine, subs: Iterable[PushSubscription], kind: str, now) -> None:
    with session_scope(engine) as session:
        for sub in subs:
            session.add(
                NotificationLog(
                    ts=now, subscription_id=sub.id, kind=kind, payload={}, status="skipped_quiet"
                )
            )


class PushScheduler:
    """The minutely reminder job. `run_due(start, end, now)` is the synchronous core (tests
    call it with a fake clock); `tick()` is what APScheduler runs."""

    def __init__(
        self,
        engine: Engine,
        sender: PushSender,
        config: LotConfig,
        clock: Clock,
        url: str,
        status: Callable[[], LotStatus | None],
        lock: asyncio.Lock | None = None,
    ):
        self.engine = engine
        self.sender = sender
        self.config = config
        self.clock = clock
        self.url = url
        self.status = status  # () -> the current LotStatus or None
        self._lock = lock or asyncio.Lock()
        self._last_run: datetime | None = None
        self._scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        # a run right after start-up still sees a reminder due in the minute before it
        self._last_run = self.clock.now() - timedelta(minutes=1)
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(
            self.tick,
            CronTrigger(second=0, timezone="UTC"),
            id="push-schedules",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=JOB_MISFIRE_S,
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._scheduler = None

    async def tick(self) -> None:
        now = self.clock.now()
        start = max(self._last_run or now - timedelta(minutes=1), now - SCHEDULE_CATCH_UP)
        self._last_run = now
        async with self._lock:
            try:
                await anyio.to_thread.run_sync(self.run_due, start, now, now)
            except Exception:
                log.exception("schedule run failed")

    def run_due(self, start: datetime, end: datetime, now: datetime) -> list[SendResult]:
        """Send the reminders due in `(start, end]`."""
        subs = _subscriptions(self.engine, lambda p: p.get("schedules"))
        due = [s for s in subs if schedule_instants(s, start, end)]
        if not due:
            return []
        _log_skipped(self.engine, [s for s in due if in_quiet_hours(s, now)], SCHEDULE, now)
        send = [s for s in due if should_send_schedule(s, start, end, now)]
        pairs = send_status(
            self.sender, self.config, send, self.status(), SCHEDULE, self.url, "normal"
        )
        results = [r for _, r in pairs]
        log.info(
            "schedules: %d due, %d sent, %d skipped",
            len(due),
            sum(r.ok for r in results),
            len(due) - len(send),
        )
        return results


class AlmostFullNotifier(StatusNotifier):
    name = "almost-full"

    def dispatch(self, old: LotStatus, new: LotStatus, now: datetime) -> list[SendResult]:
        subs = _subscriptions(self.engine, lambda p: p.get("alert_when_almost_full"))
        hit = [s for s in subs if almost_full_zones(s, old, new)]
        if not hit:
            return []
        last = self._last_alerts([s.id for s in hit], now)
        cooled = [
            s for s in hit if last.get(s.id) is None or now - last[s.id] >= ALMOST_FULL_COOLDOWN
        ]
        _log_skipped(self.engine, [s for s in cooled if in_quiet_hours(s, now)], ALMOST_FULL, now)
        send = [s for s in hit if should_send_almost_full(s, old, new, now, last.get(s.id))]
        pairs = send_status(self.sender, self.config, send, new, ALMOST_FULL, self.url, "high")
        results = [r for _, r in pairs]
        log.info(
            "almost full: %d subscription(s) watching, %d sent",
            len(hit),
            sum(r.ok for r in results),
        )
        return results

    def _last_alerts(self, ids: list[str], now: datetime) -> dict[str, datetime]:
        """Subscription id -> its last delivered almost-full alert within the cooldown."""
        with session_scope(self.engine) as session:
            rows = session.exec(
                select(NotificationLog.subscription_id, func.max(NotificationLog.ts))
                .where(NotificationLog.kind == ALMOST_FULL)
                .where(NotificationLog.status == "sent")
                .where(NotificationLog.ts > now - ALMOST_FULL_COOLDOWN)
                .where(NotificationLog.subscription_id.in_(ids))
                .group_by(NotificationLog.subscription_id)
            )
            return dict(rows.all())
