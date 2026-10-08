"""Temporal smoothing of worker readings (vision.md §3), applied in the API.

`SlotSmoother`: a slot flips only after `k` consecutive readings agree on the other state; the
first reading of a slot (or a `restore()`d state) is taken as-is, so a restart shows numbers at
once. `CountSmoother`: a count zone reports the median of its last `k` counts (`median_low`
while fewer than `k`/an even number of readings exist, so the result is always a real reading).
"""

from __future__ import annotations

from collections import deque
from statistics import median_low


class SlotSmoother:
    """A slot changes state only after `k` consecutive readings agree."""

    def __init__(self, k: int = 3):
        if k < 1:
            raise ValueError("k must be >= 1")
        self.k = k
        self._state: dict[str, bool] = {}
        self._pending: dict[str, tuple[bool, int]] = {}

    def update(self, slot_id: str, taken_now: bool) -> bool:
        """Feed one raw reading; return the smoothed state."""
        taken_now = bool(taken_now)
        state = self._state.get(slot_id)
        if state is None:
            self._state[slot_id] = taken_now
            return taken_now
        if taken_now == state:
            self._pending.pop(slot_id, None)
            return state
        candidate, count = self._pending.get(slot_id, (taken_now, 0))
        count = count + 1 if candidate == taken_now else 1
        if count >= self.k:
            self._state[slot_id] = taken_now
            self._pending.pop(slot_id, None)
            return taken_now
        self._pending[slot_id] = (taken_now, count)
        return state

    def update_many(self, readings: dict[str, bool]) -> dict[str, bool]:
        return {slot_id: self.update(slot_id, taken) for slot_id, taken in readings.items()}

    def restore(self, states: dict[str, bool]) -> None:
        """Seed smoothed states at start-up (e.g. from the DB); pending flips are dropped."""
        for slot_id, taken in states.items():
            self._state[slot_id] = bool(taken)
            self._pending.pop(slot_id, None)

    def state(self, slot_id: str) -> bool | None:
        """Smoothed state, or None for a slot never seen."""
        return self._state.get(slot_id)

    @property
    def states(self) -> dict[str, bool]:
        return dict(self._state)


class CountSmoother:
    """A count zone reports the median of its last `k` counts."""

    def __init__(self, k: int = 3):
        if k < 1:
            raise ValueError("k must be >= 1")
        self.k = k
        self._window: dict[str, deque[int]] = {}

    def update(self, zone: str, count: int) -> int:
        """Feed one raw count; return the smoothed count."""
        window = self._window.setdefault(zone, deque(maxlen=self.k))
        window.append(int(count))
        return median_low(window)

    def restore(self, counts: dict[str, int]) -> None:
        """Seed each zone's window with one count (start-up)."""
        for zone, count in counts.items():
            self._window[zone] = deque([int(count)], maxlen=self.k)

    def value(self, zone: str) -> int | None:
        window = self._window.get(zone)
        return median_low(window) if window else None
