"""When a push is due (notifications.md §4-5): on-my-way, schedules, quiet hours and almost-full
alerts. Pure functions, no I/O.

"Watched" means the zones in `prefs.zones` (every zone when unset): their summed free count
and the level of that sum (`level_for` with the lot's thresholds) are what the user sees, and
what `last_sent_free` / `last_sent_level` remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from parking.config import Levels
from parking.core.fusion import level_for
from parking.db.models import PushSubscription
from parking.messages import LotStatus, ZoneStatus
from parking.push.payload import type_free

MIN_GAP = timedelta(minutes=2)  # between on-my-way pushes, except "became full"
MAX_PER_WINDOW = 6  # pushes per on-my-way window, the immediate one included
MIN_FREE_DELTA = 3
FREE_DELTA_RATIO = 0.10  # of the watched capacity
SCHEDULE_GAP = timedelta(minutes=10)  # no reminder if any push went out this recently
SCHEDULE_CATCH_UP = timedelta(minutes=5)  # a reminder later than this is dropped
ALMOST_FULL_COOLDOWN = timedelta(hours=2)
LEVEL_RANK = {"plenty": 0, "filling": 1, "almost_full": 2, "full": 3}


@dataclass(frozen=True)
class Watched:
    free: int
    capacity: int
    level: str


def watched_zones(sub: PushSubscription, status: LotStatus) -> list[ZoneStatus]:
    """The zones in `prefs.zones`, or all of them when unset (or none of them still exist)."""
    wanted = (sub.prefs or {}).get("zones")
    return [z for z in status.zones if not wanted or z.id in wanted] or status.zones


def watched(sub: PushSubscription, status: LotStatus, levels: Levels | None = None) -> Watched:
    levels = levels or Levels()
    zones = watched_zones(sub, status)
    free = sum(z.free for z in zones)
    capacity = sum(z.capacity for z in zones)
    return Watched(free, capacity, level_for(free, capacity, levels.plenty, levels.filling))


def became_full(sub: PushSubscription, old: LotStatus | None, new: LotStatus) -> bool:
    """A watched zone is `full` now and wasn't in `old` (no `old` = nothing to compare)."""
    if old is None:
        return False
    before = {z.id: z.level for z in old.zones}
    return any(
        z.level == "full" and before.get(z.id, "full") != "full" for z in watched_zones(sub, new)
    )


def special_flipped(sub: PushSubscription, old: LotStatus | None, new: LotStatus) -> bool:
    """One of `prefs.space_types` went between "none free" and "some free" in the watched
    zones (the last accessible space was taken, an EV charger came free)."""
    wanted = (sub.prefs or {}).get("space_types")
    if old is None or not wanted:
        return False
    before, after = type_free(watched_zones(sub, old)), type_free(watched_zones(sub, new))
    return any(t in before and t in after and (before[t] == 0) != (after[t] == 0) for t in wanted)


def free_threshold(capacity: int) -> float:
    """`max(3, 10% of capacity)`."""
    return max(MIN_FREE_DELTA, FREE_DELTA_RATIO * capacity)


def should_send_on_my_way(
    sub: PushSubscription,
    old_status: LotStatus | None,
    new_status: LotStatus,
    now: datetime,
    levels: Levels | None = None,
) -> bool:
    """notifications.md §4: during the window, push when the watched level changed, the
    watched free count moved by `max(3, 10% of capacity)` since the last push, a watched
    zone became full, or a special space type the user follows ran out or came back; at most
    6 pushes per window, 2 min apart except for those last two. Quiet hours don't apply (the
    user asked for these)."""
    until = sub.on_my_way_until
    if until is None or until <= now:
        return False
    if sub.on_my_way_sent >= MAX_PER_WINDOW:
        return False
    if became_full(sub, old_status, new_status):
        return True
    if special_flipped(sub, old_status, new_status):
        return True
    if sub.last_sent_at is not None and now - sub.last_sent_at < MIN_GAP:
        return False
    w = watched(sub, new_status, levels)
    if sub.last_sent_level is None or sub.last_sent_free is None:
        return True  # the last push had no data ("No data yet")
    if w.level != sub.last_sent_level:
        return True
    return abs(w.free - sub.last_sent_free) >= free_threshold(w.capacity)


# --- Tier 3 (notifications.md §5) ---


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ValueError, KeyError):  # a tz that went away from the tz database
        return ZoneInfo("UTC")


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def in_quiet_hours(sub: PushSubscription, now: datetime) -> bool:
    """`prefs.quiet_hours` `{from, to}` in the subscription's tz, `from` included, `to` not;
    `from > to` spans midnight (22:00–07:00), `from == to` is no quiet time at all."""
    quiet = (sub.prefs or {}).get("quiet_hours")
    if not quiet:
        return False
    start, end = _hhmm(quiet["from"]), _hhmm(quiet["to"])
    local = now.astimezone(_zone(sub.tz)).time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def schedule_instants(sub: PushSubscription, start: datetime, end: datetime) -> list[datetime]:
    """UTC instants in `(start, end]` at which one of `prefs.schedules` is due. A schedule is
    `HH:MM` on ISO weekdays `days` (1 = Monday) in the subscription's tz: on the autumn DST
    change an hour repeats and only its first pass counts (`fold=0`); in spring a time inside
    the skipped hour fires one hour later on the wall clock (the same UTC instant as just
    before the change)."""
    schedules = (sub.prefs or {}).get("schedules") or []
    if not schedules:
        return []
    zone = _zone(sub.tz)
    first = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date() + timedelta(days=1)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    found = set()
    for schedule in schedules:
        at = _hhmm(schedule["time"])
        for day in days:
            if day.isoweekday() not in schedule["days"]:
                continue
            instant = _local_instant(day, at, zone)
            if start < instant <= end:
                found.add(instant)
    return sorted(found)


def _local_instant(day: date, at: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)


def should_send_schedule(
    sub: PushSubscription, start: datetime, end: datetime, now: datetime
) -> bool:
    """A reminder fell due in `(start, end]`, it isn't quiet hours, and no push went out to
    this subscription in the last 10 minutes."""
    if not schedule_instants(sub, start, end):
        return False
    if in_quiet_hours(sub, now):
        return False
    return sub.last_sent_at is None or now - sub.last_sent_at >= SCHEDULE_GAP


def almost_full_zones(
    sub: PushSubscription, old: LotStatus | None, new: LotStatus
) -> list[ZoneStatus]:
    """Watched zones whose level got worse into `almost_full` or `full` (`filling →
    almost_full`, `almost_full → full`, …; getting better or a stale zone doesn't count)."""
    if old is None:
        return []
    before = {z.id: z.level for z in old.zones}
    return [
        z
        for z in watched_zones(sub, new)
        if z.level in ("almost_full", "full")
        and not z.stale
        and z.id in before
        and LEVEL_RANK[z.level] > LEVEL_RANK[before[z.id]]
    ]


def should_send_almost_full(
    sub: PushSubscription,
    old: LotStatus | None,
    new: LotStatus,
    now: datetime,
    last_alert_at: datetime | None,
) -> bool:
    """`prefs.alert_when_almost_full`, a watched zone became almost full or full, not in quiet
    hours, and the last almost-full alert to this subscription is over 2 h ago."""
    if not (sub.prefs or {}).get("alert_when_almost_full"):
        return False
    if not almost_full_zones(sub, old, new):
        return False
    if in_quiet_hours(sub, now):
        return False
    return last_alert_at is None or now - last_alert_at >= ALMOST_FULL_COOLDOWN
