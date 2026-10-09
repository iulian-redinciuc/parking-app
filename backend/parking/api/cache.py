"""A small in-memory response cache (P7.6: history and forecast for 60 s).

Per API process, keyed by the request's own parameters, timed by the app's clock (so tests
can move it). Oldest entries go first once `max_entries` is reached.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from datetime import datetime, timedelta
from typing import Any

from parking.core.clock import Clock


class TtlCache:
    def __init__(self, clock: Clock, ttl_s: float = 60, max_entries: int = 256):
        self.clock = clock
        self.ttl = timedelta(seconds=ttl_s)
        self.max_entries = max_entries
        self._items: OrderedDict[Hashable, tuple[datetime, Any]] = OrderedDict()

    def get(self, key: Hashable) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if self.clock.now() >= expires:
            del self._items[key]
            return None
        return value

    def put(self, key: Hashable, value: Any) -> None:
        self._items.pop(key, None)
        self._items[key] = (self.clock.now() + self.ttl, value)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
