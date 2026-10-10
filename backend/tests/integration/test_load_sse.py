"""scripts/load/sse.py against a real uvicorn server (P8.10): a short run with a few clients
while a feeder flips a slot, and what the run reports when the API goes away."""

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path

import httpx
import pytest

from tests.integration.test_stream import AUTH, obs
from tests.integration.test_stream import server as server  # noqa: F401 (fixture)

_SPEC = importlib.util.spec_from_file_location(
    "load_sse", Path(__file__).parents[3] / "scripts" / "load" / "sse.py"
)
load = importlib.util.module_from_spec(_SPEC)
sys.modules["load_sse"] = load
_SPEC.loader.exec_module(load)
load.GRACE_S = 0.5


@pytest.fixture
def feeder(server):  # noqa: F811
    """Flips slot G01 every 0.1 s: one status change (one SSE event) each time."""
    _, base = server
    stop = threading.Event()

    def feed():
        with httpx.Client(base_url=base, timeout=5, headers=AUTH) as client:
            taken = False
            while not stop.wait(0.1):
                taken = not taken
                client.post("/internal/observations", json=obs({"G01"} if taken else set()))

    thread = threading.Thread(target=feed, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(5)


def _args(base, *extra):
    return load.parse_args(
        [base, "--clients", "5", "--connect-rate", "600", "--duration", "2", "--sample", "0.2"]
        + list(extra)
    )


def test_run_measures_the_delay_on_every_client(server, feeder):  # noqa: F811
    _, base = server
    result = asyncio.run(load.run(_args(base, "--min-events", "5")))
    assert result["clients"]["connected"] == 5
    assert result["clients"]["server_max"] == 5
    assert result["events"] >= 5
    assert result["deliveries"] == 5 * result["events"]
    assert 0 <= result["delay_s"]["p95"] < 1
    assert result["errors"] == {}
    # everything but the memory, which only docker stats can tell
    assert len(result["reasons"]) == 1 and "API memory not measured" in result["reasons"][0]


def test_run_reports_a_server_that_goes_away(server, feeder):  # noqa: F811
    app, base = server

    async def scenario():
        task = asyncio.create_task(load.run(_args(base, "--duration", "3")))
        await asyncio.sleep(1.5)
        for queue in list(app.state.runtime.broadcaster._queues):
            app.state.runtime.broadcaster.unsubscribe(queue)  # the server forgets its clients
        return await task

    result = asyncio.run(scenario())
    assert not result["passed"]
    assert result["errors"] == {"clients_behind": 5}
