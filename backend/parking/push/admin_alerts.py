"""Admin alerts (notifications.md §5.1, P7.8): pushes to the subscriptions an admin opted in
(`push_subscription.admin_alerts`) when something needs a look.

| Issue | Condition | Alert after |
|-------|-----------|-------------|
| `camera_down:<camera>` | the camera's latest health is `down` | 2 min |
| `camera_shifted:<camera>` | its latest health issue is `shifted` (and it isn't down) | at once |
| `stale:<zone>` | the zone is stale, has had data, and none of its cameras is down | 5 min |
| `clamps:<zone>` | > 3 clamped entry/exit events today (lot-local day, after the zone's
  last correction) | at once |

The times count from when the monitor first saw the condition (it checks every 10 s). An issue
gets at most one alert per hour (`kind: admin_alert`, `Urgency: high`; while it lasts the hourly
alert repeats, replacing the last one by its tag), and a "resolved" push (`Urgency: normal`)
once it clears if an alert went out for it. Quiet hours don't apply. The state is kept in
memory: after a restart an issue that is still there alerts again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Literal

import anyio
from sqlalchemy import Engine, func
from sqlmodel import col, select

from parking.api.jobs import lot_timezone
from parking.config import LotConfig
from parking.core.clock import Clock
from parking.db.engine import session_scope
from parking.db.models import Correction, FlowEvent, PushSubscription
from parking.messages import CameraHealthMsg, LotStatus
from parking.push.payload import local_time
from parking.push.sender import PushSender, SendResult

log = logging.getLogger(__name__)

KIND = "admin_alert"
IssueKind = Literal["camera_down", "camera_shifted", "stale", "clamps"]

# how long a condition must last before it alerts
GRACE: dict[str, timedelta] = {
    "camera_down": timedelta(minutes=2),
    "camera_shifted": timedelta(0),
    "stale": timedelta(minutes=5),
    "clamps": timedelta(0),
}
REPEAT = timedelta(hours=1)  # at most one alert per issue per hour
MAX_CLAMPS_PER_DAY = 3
CHECK_S = 10.0


@dataclass(frozen=True)
class Condition:
    """Something wrong right now (before its grace period)."""

    kind: IssueKind
    subject: str  # camera or zone id
    detail: str = ""  # e.g. the camera issue, or the clamp count

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.subject}"


@dataclass
class Issue:
    condition: Condition
    since: datetime  # first seen (continuously since)
    alerted: bool = False  # an alert went out in this episode -> a "resolved" push


@dataclass(frozen=True)
class Notice:
    """One push to send: an alert, or `resolved` for an issue that had one."""

    condition: Condition
    since: datetime
    resolved_at: datetime | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None


@dataclass
class AlertTracker:
    """The per-issue state machine (pure: `update` is fed the current conditions)."""

    open: dict[str, Issue] = field(default_factory=dict)
    last_alert: dict[str, datetime] = field(default_factory=dict)

    def update(self, conditions: Iterable[Condition], now: datetime) -> list[Notice]:
        current = {c.key: c for c in conditions}
        notices = []
        for key, issue in list(self.open.items()):
            if key not in current:
                del self.open[key]
                if issue.alerted:
                    notices.append(Notice(issue.condition, issue.since, now))
        for key, cond in current.items():
            issue = self.open.get(key)
            if issue is None:
                issue = self.open[key] = Issue(cond, now)
            issue.condition = cond  # the detail may change (more clamps, another issue)
            if now - issue.since < GRACE[cond.kind]:
                continue
            last = self.last_alert.get(key)
            if last is not None and now - last < REPEAT:
                continue
            issue.alerted = True
            self.last_alert[key] = now
            notices.append(Notice(cond, issue.since))
        return notices

    def issues(self, now: datetime) -> list[dict[str, Any]]:
        """The open issues for `GET /api/admin/alerts`."""
        out = []
        for issue in list(self.open.values()):
            c = issue.condition
            out.append(
                {
                    "key": c.key,
                    "kind": c.kind,
                    "subject": c.subject,
                    "detail": c.detail or None,
                    "since": issue.since,
                    "active": now - issue.since >= GRACE[c.kind],
                    "last_alert_at": self.last_alert.get(c.key),
                }
            )
        return sorted(out, key=lambda i: (i["since"], i["key"]))


def conditions(
    config: LotConfig,
    health: Mapping[str, CameraHealthMsg],
    status: LotStatus,
    clamps: Mapping[str, int],
) -> list[Condition]:
    """What is wrong now, from the latest health per camera, the status and today's clamps."""
    out = []
    down = set()
    for camera in config.cameras:
        h = health.get(camera.id)
        if h is None:
            continue
        if h.state == "down":
            down.add(camera.id)
            out.append(Condition("camera_down", camera.id, h.issue or ""))
        elif h.issue == "shifted":
            out.append(Condition("camera_shifted", camera.id))
    for zone in status.zones:
        cams = {c.id for c in config.cameras if zone.id in c.zones}
        if zone.stale and zone.updated_at is not None and not cams & down:
            out.append(Condition("stale", zone.id))
    for zone_id, n in clamps.items():
        if n > MAX_CLAMPS_PER_DAY:
            out.append(Condition("clamps", zone_id, str(n)))
    return out


def clamp_counts(engine: Engine, config: LotConfig, now: datetime) -> dict[str, int]:
    """Clamped (`applied=false`) flow events per flow zone since the lot-local midnight or the
    zone's last correction, whichever is later (a correction fixes what the clamps showed)."""
    tz = lot_timezone(config)
    midnight = datetime.combine(now.astimezone(tz).date(), time(), tzinfo=tz)
    zones = [z.id for z in config.zones if z.method == "flow"]
    if not zones:
        return {}
    out = {}
    with session_scope(engine) as session:
        for zone_id in zones:
            fixed = session.exec(
                select(func.max(Correction.ts)).where(Correction.zone_id == zone_id)
            ).one()
            start = max(midnight, fixed) if fixed is not None else midnight
            out[zone_id] = session.exec(
                select(func.count())
                .select_from(FlowEvent)
                .where(FlowEvent.zone_id == zone_id)
                .where(col(FlowEvent.applied).is_(False))
                .where(FlowEvent.ts >= start)
            ).one()
    return out


def alert_payload(
    notice: Notice, config: LotConfig, url: str, tz: str = "UTC", lang: str = "en"
) -> dict[str, Any]:
    """The push for one notice (api.md §6 shape, `kind: admin_alert`). English text: it's for
    the admin; zone names in `lang`. The tag is per issue, so an alert, its hourly repeat and
    its "resolved" replace each other."""
    c = notice.condition
    since = local_time(notice.since, tz)
    base = url.split("#")[0]
    if c.kind in ("camera_down", "camera_shifted"):
        name = c.subject
        link = f"{base}#/admin/cameras/{c.subject}"
    else:
        zone = next((z for z in config.zones if z.id == c.subject), None)
        name = zone.display_name(lang) if zone else c.subject
        link = f"{base}#/admin"
    if notice.resolved:
        title = {
            "camera_down": f"Camera {name} is back up",
            "camera_shifted": f"Camera {name} is no longer shifted",
            "stale": f"{name}: live data again",
            "clamps": f"{name}: entry/exit count fixed",
        }[c.kind]
        body = f"Resolved: {since}–{local_time(notice.resolved_at, tz)}"
    else:
        title, body = {
            "camera_down": (
                f"Camera {name} is down",
                f"Since {since}" + (f" ({c.detail.replace('_', ' ')})" if c.detail else ""),
            ),
            "camera_shifted": (
                f"Camera {name} has shifted",
                f"Since {since}. Check its spaces in the editor",
            ),
            "stale": (f"{name}: no fresh data", f"Stale since {since}"),
            "clamps": (
                f"{name}: entry/exit count is off",
                f"{c.detail} clamped events today. Correct the count",
            ),
        }[c.kind]
    return {
        "title": f"Admin: {title}",
        "body": body,
        "tag": f"admin-{c.key}",
        "url": link,
        "level": None,
        "kind": KIND,
        "issue": c.key,
        "resolved": notice.resolved,
    }


def admin_subscriptions(engine: Engine) -> list[PushSubscription]:
    with session_scope(engine) as session:
        query = select(PushSubscription).where(col(PushSubscription.admin_alerts).is_(True))
        subs = list(session.exec(query))
        for sub in subs:
            session.expunge(sub)
    return subs


class AdminAlertMonitor:
    """Checks the conditions every `CHECK_S` and sends the notices. `run(health, status, now)`
    is the synchronous core (tests call it, or `check()`, with a fake clock)."""

    def __init__(
        self,
        engine: Engine,
        sender: PushSender,
        config: LotConfig,
        clock: Clock,
        url: str,
        store,
        lock: asyncio.Lock | None = None,
    ):
        self.engine = engine
        self.sender = sender
        self.config = config
        self.clock = clock
        self.url = url
        self.store = store  # the StateStore: latest health per camera + the status
        self.tracker = AlertTracker()
        self._lock = lock or asyncio.Lock()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_S)
            await self.check()

    async def check(self) -> list[SendResult]:
        """One check: the store is read on the event loop, the rest runs in a worker thread
        behind the shared push lock."""
        now = self.clock.now()
        health, status = dict(self.store.health), self.store.status()
        async with self._lock:
            try:
                return await anyio.to_thread.run_sync(self.run, health, status, now)
            except Exception:
                log.exception("admin alert check failed")
                return []

    def run(
        self, health: Mapping[str, CameraHealthMsg], status: LotStatus, now: datetime
    ) -> list[SendResult]:
        clamps = clamp_counts(self.engine, self.config, now)
        notices = self.tracker.update(conditions(self.config, health, status, clamps), now)
        if not notices:
            return []
        subs = admin_subscriptions(self.engine)
        groups: dict[tuple[str, str], list[PushSubscription]] = defaultdict(list)
        for sub in subs:
            groups[(sub.tz, sub.lang)].append(sub)
        results = []
        for notice in notices:
            what = "resolved" if notice.resolved else "alert"
            urgency = "normal" if notice.resolved else "high"
            for (tz, lang), group in groups.items():
                payload = alert_payload(notice, self.config, self.url, tz, lang)
                results += self.sender.send_many(group, payload, urgency)
            log.warning(
                "admin %s: %s%s -> %d of %d admin subscription(s)",
                what,
                notice.condition.key,
                f" ({notice.condition.detail})" if notice.condition.detail else "",
                sum(r.ok for r in results[-len(subs) :]) if subs else 0,
                len(subs),
            )
        return results
