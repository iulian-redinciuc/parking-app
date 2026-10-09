"""Tier 2 dispatch (notifications.md §4): on every status change, push to the subscriptions in
an "I'm on my way" window that `rules.should_send_on_my_way` lets through. The hook plumbing
(worker thread, shared lock) is `dispatch.StatusNotifier`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import select

from parking.db.engine import session_scope
from parking.db.models import PushSubscription
from parking.messages import LotStatus
from parking.push.dispatch import StatusNotifier, send_status
from parking.push.rules import should_send_on_my_way, watched
from parking.push.sender import SendResult

log = logging.getLogger(__name__)

KIND = "on_my_way"


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


class OnMyWayNotifier(StatusNotifier):
    name = "on-my-way"

    def dispatch(self, old: LotStatus, new: LotStatus, now: datetime) -> list[SendResult]:
        """Query the active windows, apply the rules, send, update `last_sent_*`."""
        with session_scope(self.engine) as session:
            query = select(PushSubscription).where(PushSubscription.on_my_way_until > now)
            subs = list(session.exec(query))
            for sub in subs:
                session.expunge(sub)
        levels = self.config.api.levels
        due = [s for s in subs if should_send_on_my_way(s, old, new, now, levels)]
        results = []
        for sub, result in send_status(self.sender, self.config, due, new, KIND, self.url, "high"):
            if result.ok:
                record_sent(self.engine, sub, new, self.config)
            results.append(result)
        if due:
            sent = sum(r.ok for r in results)
            log.info("on my way: %d of %d active window(s) due, %d sent", len(due), len(subs), sent)
        return results
