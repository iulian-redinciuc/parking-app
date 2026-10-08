"""P2.8: `Broadcaster` fan-out, drop-oldest, client count and the `/api/stream` event generator."""

import asyncio
import contextlib
from datetime import UTC, datetime

from parking.api.routes.public import _events
from parking.api.sse import Broadcaster
from parking.messages import LotStatus, Totals

T0 = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)


def status(free: int) -> LotStatus:
    total = Totals(
        capacity=10, occupied=10 - free, free=free, level="plenty", confidence=1.0, stale=False
    )
    return LotStatus(lot="main", updated_at=T0, total=total, zones=[])


def free_of(event) -> int:
    return LotStatus.model_validate_json(event.data).total.free


async def test_publish_reaches_every_client_in_order():
    b = Broadcaster()
    q1, q2 = b.subscribe(), b.subscribe()
    assert b.clients == 2
    b.publish(status(3))
    b.publish(status(4))
    for q in (q1, q2):
        events = [q.get_nowait(), q.get_nowait()]
        assert [e.id for e in events] == [1, 2]
        assert [free_of(e) for e in events] == [3, 4]
    assert b.latest.id == 2 and free_of(b.latest) == 4


async def test_full_queue_drops_oldest_and_never_blocks():
    b = Broadcaster(queue_size=3)
    slow, fast = b.subscribe(), b.subscribe()
    for free in range(5):
        b.publish(status(free))
        fast.get_nowait()
    assert [free_of(slow.get_nowait()) for _ in range(3)] == [2, 3, 4]
    assert slow.empty() and b.dropped == 2


async def test_unsubscribe_and_cap():
    b = Broadcaster(max_clients=2)
    q1 = b.subscribe()
    assert not b.full
    q2 = b.subscribe()
    assert b.full
    b.unsubscribe(q1)
    b.unsubscribe(q1)  # twice is harmless
    assert b.clients == 1 and not b.full
    b.publish(status(1))
    assert q1.empty() and q2.qsize() == 1


async def test_events_sends_retry_latest_then_new_and_cleans_up():
    b = Broadcaster()
    b.publish(status(7))
    gen = _events(b)
    first = await anext(gen)
    assert first.retry == 3000 and first.data is None
    assert b.clients == 1
    current = await anext(gen)
    assert (current.event, current.id, free_of(current)) == ("status", "1", 7)
    waiting = asyncio.ensure_future(anext(gen))
    await asyncio.sleep(0)
    assert not waiting.done()
    b.publish(status(6))
    new = await asyncio.wait_for(waiting, 1)
    assert (new.id, free_of(new)) == ("2", 6)
    await gen.aclose()
    assert b.clients == 0


async def test_events_without_status_skips_it():
    b = Broadcaster()
    gen = _events(b)
    await anext(gen)  # retry
    waiting = asyncio.ensure_future(anext(gen))
    await asyncio.sleep(0)
    assert not waiting.done()
    waiting.cancel()  # what sse-starlette does on a disconnect
    with contextlib.suppress(asyncio.CancelledError):
        await waiting
    assert b.clients == 0
