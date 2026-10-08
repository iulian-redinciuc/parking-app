"""P2.8: `/api/stream` over a real uvicorn server: retry + current status, new events, pings,
disconnect cleanup and the client cap."""

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from parking.api.app import create_app
from parking.config import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLOTS = ["G01", "G02"]
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]

LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 1, lon: 2}, timezone: UTC}
zones:
  - {id: ground, name: {en: Ground}, method: slots}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: "file:x.jpg"
    slots_file: config/slots/cam-ground.json
    detector: {model: models/x}
    smoothing: {consistent_readings: 1}
api: {sse_ping_s: 1}
"""


def obs(taken):
    return {
        "v": 1,
        "camera_id": "cam-ground",
        "ts": "2026-10-09T08:00:00.000Z",
        "frame_size": [10, 10],
        "inference_ms": 5,
        "detections": 0,
        "slots": [{"id": s, "score": 0.5, "taken": s in taken} for s in SLOTS],
    }


@pytest.fixture
def server(tmp_path):
    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    slots = [{"id": s, "zone": "ground", "polygon": SQUARE} for s in SLOTS]
    slot_file = {"version": 1, "camera_id": "cam-ground", "image_size": [10, 10], "slots": slots}
    (tmp_path / "config" / "slots" / "cam-ground.json").write_text(json.dumps(slot_file))
    app = create_app(
        tmp_path / "config" / "lot.yaml",
        Settings(_env_file=None, worker_token=TOKEN),
        db_url=f"sqlite:///{tmp_path}/parking.sqlite",
        tick=False,
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "uvicorn didn't start"
        time.sleep(0.05)
    yield app, f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(10)


def read_blocks(lines, n):
    """The next `n` SSE blocks (lines up to a blank line) from an `iter_lines()` iterator."""
    blocks, block = [], []
    for line in lines:
        if line:
            block.append(line)
            continue
        if block:
            blocks.append(block)
            block = []
            if len(blocks) == n:
                return blocks
    raise AssertionError(f"stream ended after {blocks}")


def field(block, name):
    return next(line.split(": ", 1)[1] for line in block if line.startswith(f"{name}: "))


def test_stream_sends_status_then_changes_with_pings(server):
    app, base = server
    with httpx.Client(base_url=base, timeout=5) as client:
        assert (
            client.post("/internal/observations", json=obs({"G01"}), headers=AUTH).status_code
            == 204
        )
        with client.stream("GET", "/api/stream") as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-cache"
            assert r.headers["x-accel-buffering"] == "no"
            lines = r.iter_lines()
            retry, current = read_blocks(lines, 2)
            assert retry == ["retry: 3000"]
            assert field(current, "event") == "status"
            status = json.loads(field(current, "data"))
            assert status["lot"] == "main" and status["zones"][0]["free"] == 1
            first_id = int(field(current, "id"))
            assert app.state.runtime.broadcaster.clients == 1

            client.post("/internal/observations", json=obs({"G01", "G02"}), headers=AUTH)
            (changed,) = read_blocks(lines, 1)
            assert int(field(changed, "id")) == first_id + 1
            assert json.loads(field(changed, "data"))["zones"][0]["free"] == 0

            (ping,) = read_blocks(lines, 1)  # sse_ping_s: 1
            assert ping == ["event: ping", "data: "]

        deadline = time.monotonic() + 5
        while app.state.runtime.broadcaster.clients:
            assert time.monotonic() < deadline, "client not unsubscribed after disconnect"
            time.sleep(0.05)
        assert client.get("/healthz").json()["stream"]["clients"] == 0


def test_stream_over_cap_is_503(server):
    app, base = server
    app.state.runtime.broadcaster.max_clients = 0
    r = httpx.get(f"{base}/api/stream", timeout=5)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "unavailable"
