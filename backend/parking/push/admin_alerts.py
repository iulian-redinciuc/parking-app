"""Admin alerts (notifications.md §5.1, P7.8 + P8.7): pushes to the subscriptions an admin opted in
(`push_subscription.admin_alerts`) when something needs a look.

| Issue | Condition | Alert after |
|-------|-----------|-------------|
| `camera_down:<camera>` | the camera's latest health is `down` | 2 min |
| `camera_shifted:<camera>` | its latest health issue is `shifted` (and it isn't down) | at once |
| `stale:<zone>` | the zone is stale, has had data, and none of its cameras is down | 5 min |
| `clamps:<zone>` | > 3 clamped entry/exit events today (lot-local day, after the zone's
  last correction) | at once |
| `flow_mismatch:<zone>` | the zone's flow camera and barrier differ by more than 2 cars in
  their net count (in − out) today (same period as `clamps`); not while either is down or
  has not reported yet (barrier.md §4) | 2 min |
| `disk:<machine>` | the disk holding `data/` is more than 85% full | 5 min |
| `cpu_temp:<machine>` | the CPU is at 80 °C or more (machines that report it) | 5 min |
| `api_restarted:api` | the API process started less than 2 min ago | at once, no "resolved" |
| `backup_failed:api` | the last nightly backup failed, stayed on the machine, or is over
  26 h old (`data/backups/last-run.json`; nothing without that file) | at once |

`<machine>` is `api` (the API's own) or a camera id (its worker's, from the health messages).
The times count from when the monitor first saw the condition (it checks every 10 s). An issue
gets at most one alert per hour (`kind: admin_alert`, `Urgency: high`; while it lasts the hourly
alert repeats, replacing the last one by its tag), and a "resolved" push (`Urgency: normal`)
once it clears if an alert went out for it. Quiet hours don't apply. The state is kept in
memory: after a restart an issue that is still there alerts again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

import anyio
from sqlalchemy import Engine, func
from sqlmodel import col, select

from parking.api.jobs import lot_timezone
from parking.config import LotConfig
from parking.core.clock import Clock
from parking.core.system import SystemStats, system_stats
from parking.db.engine import session_scope
from parking.db.models import Correction, FlowEvent, PushSubscription
from parking.messages import CameraHealthMsg, LotStatus
from parking.push.payload import local_time
from parking.push.sender import PushSender, SendResult

log = logging.getLogger(__name__)

KIND = "admin_alert"
IssueKind = Literal[
    "camera_down",
    "camera_shifted",
    "stale",
    "clamps",
    "flow_mismatch",
    "disk",
    "cpu_temp",
    "api_restarted",
    "backup_failed",
]

# how long a condition must last before it alerts
GRACE: dict[str, timedelta] = {
    "camera_down": timedelta(minutes=2),
    "camera_shifted": timedelta(0),
    "stale": timedelta(minutes=5),
    "clamps": timedelta(0),
    "flow_mismatch": timedelta(minutes=2),  # a car between the two, events still in an outbox
    "disk": timedelta(minutes=5),
    "cpu_temp": timedelta(minutes=5),
    "api_restarted": timedelta(0),
    "backup_failed": timedelta(0),
}
REPEAT = timedelta(hours=1)  # at most one alert per issue per hour
NO_RESOLVED = frozenset({"api_restarted"})  # an event, not a state: nothing to resolve
MAX_CLAMPS_PER_DAY = 3
MAX_FLOW_DIFF = 2  # cars the camera's and the barrier's net count may differ by (Phase 5 target)
DISK_MAX_PCT = 85.0
CPU_TEMP_MAX_C = 80.0  # a Raspberry Pi 5 starts throttling here
RESTART_SHOWN = timedelta(minutes=2)  # how long after its start the API counts as restarted
API = "api"  # the subject of the API machine's own issues
BACKUP_STATUS = Path("data") / "backups" / "last-run.json"  # written by deploy/backup.sh
BACKUP_MAX_AGE = timedelta(hours=26)  # nightly, plus slack
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
                if issue.alerted and issue.condition.kind not in NO_RESOLVED:
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


@dataclass(frozen=True)
class FlowTally:
    """One source's entry/exit events of a zone in the comparison period."""

    camera_id: str
    source: str  # camera | barrier
    cars_in: int = 0
    cars_out: int = 0

    @property
    def net(self) -> int:
        return self.cars_in - self.cars_out

    def __str__(self) -> str:
        return f"{self.source} {self.net:+d} (in {self.cars_in}, out {self.cars_out})"


def mismatch_condition(
    zone_id: str, tallies: Iterable[FlowTally], health: Mapping[str, CameraHealthMsg]
) -> Condition | None:
    """`flow_mismatch` if the zone's two sources disagree; None while one of them is down or
    has never reported (its missing events would be the whole difference)."""
    a, b = tallies
    for tally in (a, b):
        h = health.get(tally.camera_id)
        if h is None or h.state == "down":
            return None
    if abs(a.net - b.net) <= MAX_FLOW_DIFF:
        return None
    return Condition("flow_mismatch", zone_id, f"{a}, {b}")


def conditions(
    config: LotConfig,
    health: Mapping[str, CameraHealthMsg],
    status: LotStatus,
    clamps: Mapping[str, int],
    tallies: Mapping[str, tuple[FlowTally, FlowTally]] | None = None,
) -> list[Condition]:
    """What is wrong now, from the latest health per camera, the status, today's clamps and
    today's events of the zones counted twice (`flow_tallies`)."""
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
    for zone_id, pair in (tallies or {}).items():
        mismatch = mismatch_condition(zone_id, pair, health)
        if mismatch is not None:
            out.append(mismatch)
    return out


def system_conditions(health: Mapping[str, CameraHealthMsg], api: SystemStats) -> list[Condition]:
    """Full disks and hot CPUs: the API's own machine and each worker's (its latest health
    message; a `down` camera's numbers are old, so they're skipped)."""
    machines: dict[str, SystemStats] = {API: api}
    for camera_id, h in health.items():
        if h.state != "down":
            machines[camera_id] = SystemStats(h.disk_pct, h.cpu_temp_c)
    out = []
    for subject, stats in machines.items():
        if stats.disk_pct is not None and stats.disk_pct > DISK_MAX_PCT:
            out.append(Condition("disk", subject, f"{stats.disk_pct:.0f}%"))
        if stats.cpu_temp_c is not None and stats.cpu_temp_c >= CPU_TEMP_MAX_C:
            out.append(Condition("cpu_temp", subject, f"{stats.cpu_temp_c:.0f} °C"))
    return out


def backup_condition(file: Path, now: datetime) -> Condition | None:
    """The outcome `deploy/backup.sh` left of its last run (deployment.md §7): a failed run, an
    archive that stayed on the machine, or no run for over a day. No file = no backups set up
    on this machine (the dev stack), which is not an alert."""
    try:
        text = file.read_text()
    except OSError:
        return None
    try:
        last = json.loads(text)
        ts, status = datetime.fromisoformat(last["ts"]), last["status"]
        ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        message = str(last.get("message") or "")
    except (ValueError, KeyError, TypeError):
        return Condition("backup_failed", API, "the status file can't be read")
    if status == "ok":
        if now - ts <= BACKUP_MAX_AGE:
            return None
        return Condition("backup_failed", API, "no backup since the last good one")
    if not message and status == "local_only":
        message = "not copied off the machine"
    return Condition("backup_failed", API, message)


def _period_start(session, config: LotConfig, zone_id: str, now: datetime) -> datetime:
    """The lot-local midnight or the zone's last correction, whichever is later."""
    tz = lot_timezone(config)
    midnight = datetime.combine(now.astimezone(tz).date(), time(), tzinfo=tz)
    fixed = session.exec(select(func.max(Correction.ts)).where(Correction.zone_id == zone_id)).one()
    return max(midnight, fixed) if fixed is not None else midnight


def clamp_counts(engine: Engine, config: LotConfig, now: datetime) -> dict[str, int]:
    """Clamped (`applied=false`) flow events per flow zone since the lot-local midnight or the
    zone's last correction, whichever is later (a correction fixes what the clamps showed).
    Events of a zone's second source are never applied and don't count here."""
    zones = [z.id for z in config.zones if z.method == "flow"]
    if not zones:
        return {}
    out = {}
    with session_scope(engine) as session:
        for zone_id in zones:
            out[zone_id] = session.exec(
                select(func.count())
                .select_from(FlowEvent)
                .where(FlowEvent.zone_id == zone_id)
                .where(col(FlowEvent.applied).is_(False))
                .where(col(FlowEvent.counted).is_(True))
                .where(FlowEvent.ts >= _period_start(session, config, zone_id, now))
            ).one()
    return out


def flow_tallies(
    engine: Engine, config: LotConfig, now: datetime
) -> dict[str, tuple[FlowTally, FlowTally]]:
    """For every flow zone with both a barrier and a flow camera: each one's events (counted
    or not, clamped or not) over the same period as `clamp_counts`, the counting source first.
    A correction starts the comparison again."""
    zones = [z.id for z in config.zones if z.method == "flow" and config.flow_checker(z.id)]
    out = {}
    with session_scope(engine) as session:
        for zone_id in zones:
            start = _period_start(session, config, zone_id, now)
            rows = session.exec(
                select(FlowEvent.camera_id, FlowEvent.direction, func.count())
                .where(FlowEvent.zone_id == zone_id)
                .where(FlowEvent.ts >= start)
                .group_by(FlowEvent.camera_id, FlowEvent.direction)
            ).all()
            seen = {(camera_id, direction): n for camera_id, direction, n in rows}
            pair = []
            for camera_id in (config.flow_counter(zone_id), config.flow_checker(zone_id)):
                role = config.camera(camera_id).role
                pair.append(
                    FlowTally(
                        camera_id,
                        "barrier" if role == "barrier" else "camera",
                        seen.get((camera_id, "in"), 0),
                        seen.get((camera_id, "out"), 0),
                    )
                )
            out[zone_id] = (pair[0], pair[1])
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
    what = "Camera"
    if c.kind in ("camera_down", "camera_shifted"):
        name = c.subject
        if any(cam.id == c.subject and cam.role == "barrier" for cam in config.cameras):
            what = "Barrier"
        link = f"{base}#/admin/cameras/{c.subject}"
    elif c.kind in ("disk", "cpu_temp", "api_restarted", "backup_failed"):
        name = "the API server" if c.subject == API else f"camera {c.subject}'s machine"
        link = f"{base}#/admin"
    else:
        zone = next((z for z in config.zones if z.id == c.subject), None)
        name = zone.display_name(lang) if zone else c.subject
        link = f"{base}#/admin"
    if notice.resolved:
        title = {
            "camera_down": f"{what} {name} is back up",
            "camera_shifted": f"Camera {name} is no longer shifted",
            "stale": f"{name}: live data again",
            "clamps": f"{name}: entry/exit count fixed",
            "flow_mismatch": f"{name}: camera and barrier agree again",
            "disk": f"Disk space is back on {name}",
            "cpu_temp": f"The CPU has cooled down on {name}",
            "api_restarted": "The API is running",
            "backup_failed": "The backup works again",
        }[c.kind]
        body = f"Resolved: {since}–{local_time(notice.resolved_at, tz)}"
    else:
        title, body = {
            "camera_down": (
                f"{what} {name} is down",
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
            "flow_mismatch": (
                f"{name}: camera and barrier disagree",
                f"Today: {c.detail}. Check the one that is off, then correct the count",
            ),
            "disk": (f"Disk almost full on {name}", f"{c.detail} used, since {since}"),
            "cpu_temp": (f"The CPU is hot on {name}", f"{c.detail}, since {since}"),
            "api_restarted": (
                "The API restarted",
                f"At {since}. Nothing to do if that was you",
            ),
            "backup_failed": (
                "The backup failed",
                f"Since {since}" + (f": {c.detail}" if c.detail else ""),
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
        root: Path | None = None,
    ):
        self.engine = engine
        self.sender = sender
        self.config = config
        self.clock = clock
        self.url = url
        self.store = store  # the StateStore: latest health per camera + the status
        self.tracker = AlertTracker()
        # the API's own machine (P8.7); without `root` (the app root) none of it is checked
        self.root = root
        self.started_at: datetime | None = clock.now() if root is not None else None
        self.probe: Callable[[], SystemStats] = (
            (lambda: system_stats(root / "data" if (root / "data").exists() else root))
            if root is not None
            else SystemStats
        )
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
        tallies = flow_tallies(self.engine, self.config, now)
        found = conditions(self.config, health, status, clamps, tallies)
        found += system_conditions(health, self.probe())
        if self.started_at is not None and now - self.started_at < RESTART_SHOWN:
            found.append(Condition("api_restarted", API))
        if self.root is not None:
            backup = backup_condition(self.root / BACKUP_STATUS, now)
            if backup is not None:
                found.append(backup)
        notices = self.tracker.update(found, now)
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
