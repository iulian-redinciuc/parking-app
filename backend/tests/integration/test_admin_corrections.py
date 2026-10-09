"""P7.4: `POST /api/admin/zones/{id}/correct` (flow zones only, a `correction` + `zone_state`
row, the new status published) and the `GET /api/admin/corrections` audit log; P5.7: the
scheduled flow-zone reset (`zones[].reset`)."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from parking.api.app import create_app
from parking.config import Settings
from parking.core.clock import FakeClock
from parking.db.engine import make_engine, session_scope
from parking.db.models import Correction, ZoneState

ADMIN = "static-admin-token-0123456789abcdef"
WORKER = "worker-token"
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]

LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 1, lon: 2}, timezone: UTC}
zones:
  - {id: ground, name: {en: Ground}, method: slots}
  - {id: underground, name: {en: Underground, ro: Subteran}, method: flow, capacity: 60}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: "file:x.jpg"
    slots_file: config/slots/cam-ground.json
    detector: {model: models/x}
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
    slots = [{"id": "G01", "zone": "ground", "polygon": SQUARE}]
    slot_file = {"version": 1, "camera_id": "cam-ground", "image_size": [10, 10], "slots": slots}
    (tmp_path / "config" / "slots" / "cam-ground.json").write_text(json.dumps(slot_file))
    return tmp_path


@pytest.fixture
def clock():
    return FakeClock(T0)


def with_reset(lot, cron="0 3 * * *", value=2):
    zone = "{id: underground, name: {en: Underground, ro: Subteran}, method: flow, capacity: 60}"
    reset = (
        "  - {id: underground, name: {en: Underground}, method: flow, capacity: 60,\n"
        f'     reset: {{enabled: true, cron: "{cron}", value: {value}}}}}'
    )
    (lot / "config" / "lot.yaml").write_text(LOT_YAML.replace(f"  - {zone}", reset))


def make_client(lot, clock):
    settings = Settings(_env_file=None, admin_token=ADMIN, worker_token=WORKER)
    app = create_app(
        lot / "config" / "lot.yaml",
        settings,
        clock=clock,
        db_url=f"sqlite:///{lot}/parking.sqlite",
        tick=False,
    )
    return TestClient(app)


def auth(token=ADMIN):
    return {"Authorization": f"Bearer {token}"}


def rows(lot, model):
    engine = make_engine(f"sqlite:///{lot}/parking.sqlite")
    with session_scope(engine) as session:
        out = [r.model_dump() for r in session.exec(select(model))]
    engine.dispose()
    return out


def flow(event_id, direction="in", ts="2026-10-09T12:00:00Z"):
    return {
        "v": 1,
        "event_id": event_id,
        "camera_id": "cam-ramp",
        "ts": ts,
        "direction": direction,
        "track_id": 1,
        "cls": "car",
        "confidence": 0.8,
    }


def test_correct_sets_the_count_logs_it_and_publishes(lot, clock):
    with make_client(lot, clock) as client:
        rt = client.app.state.runtime
        r = client.post(
            "/internal/flow-events",
            json={"events": [flow(f"e{i}") for i in range(5)]},
            headers=auth(WORKER),
        )
        assert r.json()["accepted"] == 5
        published = rt.broadcaster.published
        clock.advance(60)
        r = client.post(
            "/api/admin/zones/underground/correct",
            json={"occupied": 37, "note": " manual count "},
            headers=auth(),
        )
        assert r.status_code == 200, r.text
        zone = r.json()
        assert (zone["id"], zone["occupied"], zone["free"]) == ("underground", 37, 23)
        assert zone["confidence"] == 1.0
        assert zone["updated_at"] == "2026-10-09T12:01:00.000Z"
        # every app gets it on the live stream at once
        assert rt.broadcaster.published == published + 1
        latest = rt.broadcaster.latest.status
        assert next(z for z in latest.zones if z.id == "underground").occupied == 37

        r = client.get("/api/admin/corrections", headers=auth())
        assert r.status_code == 200
        assert r.json() == [
            {
                "id": 1,
                "ts": "2026-10-09T12:01:00.000Z",
                "zone_id": "underground",
                "zone_name": "Underground",
                "old_occupied": 5,
                "new_occupied": 37,
                "actor": "admin-token",
                "note": "manual count",
            }
        ]
    (row,) = rows(lot, Correction)
    assert (row["old_occupied"], row["new_occupied"], row["actor"]) == (5, 37, "admin-token")
    last = max(rows(lot, ZoneState), key=lambda r: r["id"])
    assert (last["zone_id"], last["occupied"], last["source"]) == ("underground", 37, "correction")


def test_log_is_newest_first_with_limit_and_lang(lot, clock):
    with make_client(lot, clock) as client:
        for value in (10, 20, 30):
            clock.advance(1)
            r = client.post(
                "/api/admin/zones/underground/correct", json={"occupied": value}, headers=auth()
            )
            assert r.status_code == 200
        r = client.get("/api/admin/corrections?limit=2&lang=ro", headers=auth())
        log = r.json()
        assert [(c["old_occupied"], c["new_occupied"]) for c in log] == [(20, 30), (10, 20)]
        assert {c["zone_name"] for c in log} == {"Subteran"}
        assert all(c["note"] == "" for c in log)
        assert client.get("/api/admin/corrections?limit=0", headers=auth()).status_code == 422
        assert client.get("/api/admin/corrections?limit=201", headers=auth()).status_code == 422


def test_corrections_need_an_admin(lot, clock):
    with make_client(lot, clock) as client:
        for token in (None, "wrong", WORKER):
            headers = auth(token) if token else {}
            r = client.post(
                "/api/admin/zones/underground/correct", json={"occupied": 1}, headers=headers
            )
            assert r.status_code == 401
            assert client.get("/api/admin/corrections", headers=headers).status_code == 401
        assert client.app.state.runtime.store.flow["underground"].occupied == 0
    assert rows(lot, Correction) == []


@pytest.mark.parametrize(
    ("zone", "body", "status", "code"),
    [
        ("ground", {"occupied": 1}, 409, "conflict"),
        ("nowhere", {"occupied": 1}, 404, "not_found"),
        ("underground", {"occupied": 61}, 422, "bad_request"),
        ("underground", {"occupied": -1}, 422, "bad_request"),
        ("underground", {"occupied": "x"}, 422, "bad_request"),
        ("underground", {"occupied": 3, "extra": 1}, 422, "bad_request"),
        ("underground", {"occupied": 3, "note": "x" * 201}, 422, "bad_request"),
    ],
)
def test_bad_corrections_change_nothing(lot, clock, zone, body, status, code):
    with make_client(lot, clock) as client:
        r = client.post(f"/api/admin/zones/{zone}/correct", json=body, headers=auth())
        assert r.status_code == status, r.text
        assert r.json()["error"]["code"] == code
        assert client.app.state.runtime.store.flow["underground"].occupied == 0
    assert rows(lot, Correction) == []


def test_restart_keeps_the_count_and_the_confidence_counters(lot, clock):
    with make_client(lot, clock) as client:
        client.post("/api/admin/zones/underground/correct", json={"occupied": 20}, headers=auth())
        clock.advance(60)
        events = [flow(f"e{i}", ts="2026-10-09T12:01:00Z") for i in range(10)]
        client.post("/internal/flow-events", json={"events": events}, headers=auth(WORKER))
    clock.advance(3600 * 5 - 60)
    with make_client(lot, clock) as client:
        counter = client.app.state.runtime.store.flow["underground"]
        assert counter.occupied == 30
        assert counter.events_since_correction == 10
        assert counter.corrected_at == T0
        # 1 − 0.01 × 10 − 0.02 × 5 h
        assert round(counter.confidence(), 2) == 0.8


def test_scheduled_reset_sets_the_count_like_a_correction(lot, clock):
    with_reset(lot)
    with make_client(lot, clock) as client:
        runtime = client.app.state.runtime
        published = []
        runtime.ingestor.publish = published.append
        events = [flow(f"e{i}") for i in range(7)]
        client.post("/internal/flow-events", json={"events": events}, headers=auth(WORKER))
        clock.advance(3600)
        client.portal.call(runtime.jobs.run_reset, "underground")
        counter = runtime.store.flow["underground"]
        assert (counter.occupied, counter.events_since_correction) == (2, 0)
        assert counter.confidence() == 1.0
        assert published[-1].zones[1].occupied == 2
        log = client.get("/api/admin/corrections", headers=auth()).json()
        assert (log[0]["old_occupied"], log[0]["new_occupied"]) == (7, 2)
        assert log[0]["actor"] == "scheduled-reset"
    last = max(rows(lot, ZoneState), key=lambda r: r["id"])
    assert (last["occupied"], last["source"]) == (2, "reset")


def test_scheduled_reset_job_follows_the_cron_in_lot_time(lot, clock):
    with_reset(lot, cron="30 3 * * 1")
    with make_client(lot, clock) as client:
        jobs = client.app.state.runtime.jobs

        async def next_run():
            jobs.start()
            try:
                return jobs._scheduler.get_job("reset-underground").next_run_time
            finally:
                jobs.shutdown()

        when = client.portal.call(next_run)
    assert (when.weekday(), when.hour, when.minute) == (0, 3, 30)
