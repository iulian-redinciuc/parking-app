"""Injectable clock (testing.md §2): code that needs the time takes a `Clock`, tests pass a
`FakeClock` and move it forward instead of sleeping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock that only moves when told to. Naive start times are taken as UTC."""

    def __init__(self, start: datetime | None = None):
        start = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._now = start if start.tzinfo else start.replace(tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, **delta: float) -> datetime:
        """Move forward by `seconds` (plus any `timedelta` keywords) and return the new time."""
        self._now += timedelta(seconds=seconds, **delta)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when if when.tzinfo else when.replace(tzinfo=UTC)
