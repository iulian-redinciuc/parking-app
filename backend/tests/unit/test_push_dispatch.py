"""`StatusNotifier.drain` (push/dispatch.py): returns once the pending dispatches are done."""

import asyncio
import subprocess
import sys

from parking.core.clock import FakeClock
from parking.push.dispatch import StatusNotifier

SPIN = """
import asyncio
from parking.core.clock import FakeClock
from parking.push.dispatch import StatusNotifier

async def scenario():
    notifier = StatusNotifier(None, None, None, FakeClock(), "")
    task = asyncio.get_running_loop().create_task(asyncio.sleep(0))
    await task
    notifier._tasks.add(task)  # as before its `discard` callback ran
    await notifier.drain()
    print(len(notifier._tasks))

asyncio.run(scenario())
"""


def test_drain_returns_when_a_dispatch_finished_but_its_callback_has_not_run():
    """A task that is done while its done callback is still queued (the API shutting down
    right after a status change): `drain` must not wait for that callback, because gathering
    finished tasks never yields to let it run. In a subprocess, so a regression (a loop that
    never yields) fails on the timeout instead of hanging the test run."""
    result = subprocess.run(
        [sys.executable, "-c", SPIN], capture_output=True, text=True, timeout=20, check=True
    )
    assert result.stdout.strip() == "0"


async def test_drain_waits_for_dispatches_started_meanwhile():
    notifier = StatusNotifier(None, None, None, FakeClock(), "")
    done = []

    async def second():
        await asyncio.sleep(0.01)
        done.append("second")

    async def first():
        await asyncio.sleep(0.01)
        start(second())
        done.append("first")

    def start(coro):  # as `status_changed` does
        task = asyncio.get_running_loop().create_task(coro)
        notifier._tasks.add(task)
        task.add_done_callback(notifier._tasks.discard)

    start(first())
    await notifier.drain()
    assert done == ["first", "second"] and not notifier._tasks
