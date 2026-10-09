"""When a status change is worth a push (notifications.md §4-5). Pure functions, no I/O.

"Watched" means the zones in `prefs.zones` (every zone when unset): their summed free count
and the level of that sum (`level_for` with the lot's thresholds) are what the user sees, and
what `last_sent_free` / `last_sent_level` remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from parking.config import Levels
from parking.core.fusion import level_for
from parking.db.models import PushSubscription
from parking.messages import LotStatus, ZoneStatus

MIN_GAP = timedelta(minutes=2)  # between on-my-way pushes, except "became full"
MAX_PER_WINDOW = 6  # pushes per on-my-way window, the immediate one included
MIN_FREE_DELTA = 3
FREE_DELTA_RATIO = 0.10  # of the watched capacity


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
    watched free count moved by `max(3, 10% of capacity)` since the last push, or a watched
    zone became full; at most 6 pushes per window, 2 min apart unless a zone became full.
    Quiet hours don't apply (the user asked for these)."""
    until = sub.on_my_way_until
    if until is None or until <= now:
        return False
    if sub.on_my_way_sent >= MAX_PER_WINDOW:
        return False
    if became_full(sub, old_status, new_status):
        return True
    if sub.last_sent_at is not None and now - sub.last_sent_at < MIN_GAP:
        return False
    w = watched(sub, new_status, levels)
    if sub.last_sent_level is None or sub.last_sent_free is None:
        return True  # the last push had no data ("No data yet")
    if w.level != sub.last_sent_level:
        return True
    return abs(w.free - sub.last_sent_free) >= free_threshold(w.capacity)
