"""P2.7: `/internal/*` -> StateStore -> DB, auth, bad payloads, restore, cameras going down."""

import json
import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from parking.api.app import create_app
from parking.config import Settings
from parking.core.clock import FakeClock
from parking.db.engine import make_engine, session_scope
from parking.db.models import CameraHealth, FlowEvent, SlotState, ZoneState

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
T0 = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
SLOTS = ["G01", "G02", "G03", "G04"]
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]

LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 1, lon: 2}, timezone: UTC}
zones:
  - {id: ground, name: {en: Ground}, method: slots}
  - {id: underground, name: {en: Underground}, method: flow, capacity: 3}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: "file:x.jpg"
    slots_file: config/slots/cam-ground.json
    detector: {model: models/x}
    smoothing: {consistent_readings: 2}
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "file:y.jpg"
    lines_file: config/lines/cam-ramp.json
    detector: {model: models/x}
"""


@pytest.fixture
def lot(tmp_path):
    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    slots = [{"id": s, "zone": "ground", "polygon": SQUARE} for s in SLOTS]
    slot_file = {"version": 1, "camera_id": "cam-ground", "image_size": [10, 10], "slots": slots}
    (tmp_path / "config" / "slots" / "cam-ground.json").write_text(json.dumps(slot_file))
    return tmp_path


@pytest.fixture
def clock():
    return FakeClock(T0)


def make_client(lot, clock, token=TOKEN):
    settings = Settings(_env_file=None, worker_token=token)
    app = create_app(
        lot / "config" / "lot.yaml",
        settings,
        clock=clock,
        db_url=f"sqlite:///{lot}/data/db/parking.sqlite",
        tick=False,
    )
    return TestClient(app)


def db_rows(lot, model):
    engine = make_engine(f"sqlite:///{lot}/data/db/parking.sqlite")
    with session_scope(engine) as session:
        rows = [r.model_dump() for r in session.exec(select(model))]
    engine.dispose()
    return rows


def obs(taken):
    slots = [{"id": s, "score": 0.9 if s in taken else 0.1, "taken": s in taken} for s in SLOTS]
    return {
        "v": 1,
        "camera_id": "cam-ground",
        "ts": "2026-10-08T18:00:00.000Z",
        "frame_size": [10, 10],
        "inference_ms": 5,
        "detections": 0,
        "slots": slots,
    }


def health(camera="cam-ground", state="ok", issue=None):
    return {
        "v": 1,
        "camera_id": camera,
        "ts": "2026-10-08T18:00:00Z",
        "state": state,
        "issue": issue,
    }


def flow(event_id, direction="in"):
    return {
        "v": 1,
        "event_id": event_id,
        "camera_id": "cam-ramp",
        "ts": "2026-10-08T18:00:00Z",
        "direction": direction,
        "track_id": 1,
        "cls": "car",
        "confidence": 0.8,
    }


def runtime(client):
    return client.app.state.runtime


def test_observation_updates_state_db_and_logs(lot, clock, caplog):
    caplog.set_level(logging.INFO, logger="parking.api.ingest")
    with make_client(lot, clock) as client:
        r = client.post("/internal/observations", json=obs({"G01", "G02"}), headers=AUTH)
        assert r.status_code == 204
        ground = runtime(client).store.status().zones[0]
        assert (ground.occupied, ground.free, ground.stale) == (2, 2, False)
        # smoothing: one contrary reading doesn't flip, the second does
        client.post("/internal/observations", json=obs({"G01"}), headers=AUTH)
        assert runtime(client).store.status().zones[0].occupied == 2
        client.post("/internal/observations", json=obs({"G01"}), headers=AUTH)
        assert runtime(client).store.status().zones[0].occupied == 1
    assert "zone ground: 2 free, 2 occupied" in caplog.text
    zones = [(r["zone_id"], r["occupied"], r["source"]) for r in db_rows(lot, ZoneState)]
    assert zones == [("ground", 2, "observation"), ("ground", 1, "observation")]
    flips = [(r["slot_id"], r["taken"]) for r in db_rows(lot, SlotState)]
    assert flips[-1] == ("G02", False) and len(flips) == 5


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}],
)
def test_missing_or_wrong_token_is_401(lot, clock, headers):
    with make_client(lot, clock) as client:
        for path, body in [
            ("/internal/observations", obs(set())),
            ("/internal/health", health()),
            ("/internal/flow-events", {"events": [flow("e1")]}),
            ("/internal/health", "not json"),
        ]:
            r = client.post(path, json=body, headers=headers)
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "unauthorized"
        assert runtime(client).store.health == {}


def test_no_token_configured_rejects_everything(lot, clock):
    with make_client(lot, clock, token=None) as client:
        r = client.post("/internal/health", json=health(), headers={"Authorization": "Bearer "})
        assert r.status_code == 401


def test_bad_payloads_are_422_and_counted(lot, clock):
    with make_client(lot, clock) as client:
        bad = [
            ("/internal/observations", {"camera_id": "cam-ground"}),
            ("/internal/observations", {**obs(set()), "camera_id": "cam-nope"}),
            ("/internal/observations", {**obs(set()), "camera_id": "cam-ramp"}),
            ("/internal/health", {**health(), "state": "sleepy"}),
            ("/internal/health", health(camera="cam-nope")),
            ("/internal/flow-events", {"events": []}),
            ("/internal/flow-events", {"events": [{**flow("e1"), "camera_id": "cam-ground"}]}),
        ]
        for path, body in bad:
            r = client.post(path, json=body, headers=AUTH)
            assert r.status_code == 422, (path, body)
            assert r.json()["error"]["code"] == "bad_request"
        r = client.post("/internal/health", content=b"{not json", headers=AUTH)
        assert r.status_code == 422
        stats = client.get("/healthz").json()
        assert stats["ingest"]["rejected"] == len(bad) + 1
        assert stats["status"] == "ok" and stats["db"] is True
        # still works afterwards
        assert client.post("/internal/health", json=health(), headers=AUTH).status_code == 204


def test_flow_events_counted_once(lot, clock):
    with make_client(lot, clock) as client:
        batch = {"events": [flow("a"), flow("b"), flow("a")]}
        r = client.post("/internal/flow-events", json=batch, headers=AUTH)
        assert r.json() == {"accepted": 2, "duplicates": 1}
        r = client.post(
            "/internal/flow-events",
            json={"events": [flow("b"), flow("c"), flow("d")]},
            headers=AUTH,
        )
        assert r.json() == {"accepted": 2, "duplicates": 1}
        assert runtime(client).store.flow["underground"].occupied == 3  # d clamped at capacity
    rows = {r["event_id"]: r["applied"] for r in db_rows(lot, FlowEvent)}
    assert rows == {"a": True, "b": True, "c": True, "d": False}
    # after a restart, an id older than the in-memory window is still a duplicate (DB key)
    with make_client(lot, clock) as client:
        assert runtime(client).store.flow["underground"].occupied == 3
        r = client.post("/internal/flow-events", json={"events": [flow("a", "out")]}, headers=AUTH)
        assert r.json() == {"accepted": 0, "duplicates": 1}


def test_health_upserted_and_camera_down_after_30s(lot, clock, caplog):
    with make_client(lot, clock) as client:
        client.post("/internal/observations", json=obs({"G01"}), headers=AUTH)
        client.post("/internal/health", json=health(state="degraded", issue="blurry"), headers=AUTH)
        assert client.get("/healthz").json()["cameras"] == {
            "cam-ground": "degraded",
            "cam-ramp": "unknown",
        }
        ingestor = runtime(client).ingestor
        clock.advance(30)
        client.portal.call(ingestor.tick)
        assert runtime(client).store.health["cam-ground"].state == "degraded"
        caplog.set_level(logging.WARNING, logger="parking.api.ingest")
        clock.advance(1)
        client.portal.call(ingestor.tick)
        assert runtime(client).store.health["cam-ground"].state == "down"
        assert runtime(client).store.status().zones[0].stale is True
        assert "no health message for 31 s -> down" in caplog.text
    (row,) = db_rows(lot, CameraHealth)
    assert (row["camera_id"], row["state"]) == ("cam-ground", "down")


def test_restart_restores_state_as_stale(lot, clock):
    with make_client(lot, clock) as client:
        client.post("/internal/observations", json=obs({"G01", "G03"}), headers=AUTH)
    clock.advance(5)
    with make_client(lot, clock) as client:
        store = runtime(client).store
        ground = store.status().zones[0]
        assert (ground.occupied, ground.stale) == (2, True)
        assert ground.slots == {"G01": True, "G02": False, "G03": True, "G04": False}
        assert store.has_data
        # fresh data makes it live; no duplicate zone_state row for the unchanged count
        client.post("/internal/observations", json=obs({"G01", "G03"}), headers=AUTH)
        assert store.status().zones[0].stale is False
    assert len(db_rows(lot, ZoneState)) == 1


def test_tick_task_runs(lot, clock, monkeypatch):
    import parking.api.app as app_module

    monkeypatch.setattr(app_module, "TICK_S", 0.01)
    settings = Settings(_env_file=None, worker_token=TOKEN)
    app = create_app(
        lot / "config" / "lot.yaml",
        settings,
        clock=clock,
        db_url=f"sqlite:///{lot}/data/db/parking.sqlite",
    )
    calls = []
    with TestClient(app) as client:
        ingestor = client.app.state.runtime.ingestor
        original = ingestor.tick

        async def counting():
            calls.append(1)
            await original()

        ingestor.tick = counting
        import time

        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
    assert calls
