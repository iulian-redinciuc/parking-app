"""`Broadcaster`: fans every published `LotStatus` out to the SSE clients (api.md §3).

Each client gets its own `asyncio.Queue(maxsize=10)`. A full queue drops its oldest event, so
one slow phone never blocks the others or the ingest path. The status is serialised once per
publish and every event carries a monotonic `id`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from parking.messages import LotStatus

QUEUE_SIZE = 10
MAX_CLIENTS = 1000
RETRY_MS = 3000


@dataclass(frozen=True)
class StatusEvent:
    id: int
    data: str  # LotStatus JSON


class Broadcaster:
    def __init__(self, max_clients: int = MAX_CLIENTS, queue_size: int = QUEUE_SIZE):
        self.max_clients = max_clients
        self.queue_size = queue_size
        self.latest: StatusEvent | None = None
        self.published = 0
        self.dropped = 0  # events dropped from full client queues
        self._queues: set[asyncio.Queue[StatusEvent]] = set()

    @property
    def clients(self) -> int:
        return len(self._queues)

    @property
    def full(self) -> bool:
        return self.clients >= self.max_clients

    def subscribe(self) -> asyncio.Queue[StatusEvent]:
        """A new client queue; the caller checks `full` first (503 above `max_clients`)."""
        queue: asyncio.Queue[StatusEvent] = asyncio.Queue(maxsize=self.queue_size)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[StatusEvent]) -> None:
        self._queues.discard(queue)

    def publish(self, status: LotStatus) -> None:
        """Remember `status` as the latest and queue it for every client (never blocks)."""
        self.published += 1
        event = StatusEvent(self.published, status.model_dump_json())
        self.latest = event
        for queue in self._queues:
            if queue.full():
                queue.get_nowait()
                self.dropped += 1
            queue.put_nowait(event)
