"""Entry/exit counter for one `flow` zone (vision.md §7.4), kept in the API.

Skeleton for Phase 2: the counting, clamping, idempotency, corrections and confidence rule are
here so `StateStore` can show flow zones; wiring flow events in through `/internal/flow-events`,
the DB and the admin corrections comes in Phase 5 (P5.7, P5.8).
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime

from parking.core.clock import Clock
from parking.messages import Direction

log = logging.getLogger(__name__)

SEEN_IDS = 1000


class FlowCounter:
    """`occupied = clamp(occupied ± 1, 0, capacity)`; duplicate `event_id`s are ignored."""

    def __init__(self, zone_id: str, capacity: int, clock: Clock, occupied: int = 0):
        self.zone_id = zone_id
        self.capacity = capacity
        self.clock = clock
        self.occupied = self._clamp(occupied)
        self.events_since_correction = 0
        self.corrected_at: datetime = clock.now()
        self._seen: deque[str] = deque(maxlen=SEEN_IDS)

    def _clamp(self, value: int) -> int:
        return min(max(int(value), 0), self.capacity)

    def apply(self, event_id: str, direction: Direction) -> bool | None:
        """Apply one crossing. Returns None for a duplicate, False if clamped, True if applied."""
        if event_id in self._seen:
            return None
        self._seen.append(event_id)
        self.events_since_correction += 1
        new = self.occupied + (1 if direction == "in" else -1)
        if new != self._clamp(new):
            log.warning(
                "clamped: zone %s %s at %d/%d",
                self.zone_id,
                direction,
                self.occupied,
                self.capacity,
            )
            return False
        self.occupied = new
        return True

    def correct(self, value: int) -> int:
        """Set the count (admin or scheduled reset) and reset the confidence counters."""
        self.occupied = self._clamp(value)
        self.events_since_correction = 0
        self.corrected_at = self.clock.now()
        return self.occupied

    def restore(
        self, occupied: int, corrected_at: datetime | None = None, events_since_correction: int = 0
    ) -> None:
        """Continue from the persisted value after a restart."""
        self.occupied = self._clamp(occupied)
        self.events_since_correction = events_since_correction
        if corrected_at is not None:
            self.corrected_at = corrected_at

    def confidence(self) -> float:
        """`max(0.3, 1 − 0.01 × events − 0.02 × hours)` since the last correction (vision.md §8)."""
        hours = max(0.0, (self.clock.now() - self.corrected_at).total_seconds() / 3600)
        return max(0.3, 1 - 0.01 * self.events_since_correction - 0.02 * hours)
