"""P9.3: barrier events through `/internal/flow-events` (barrier.md §4): a barrier alone, a
barrier next to the flow camera (one counts, the other is compared), and the mismatch alert."""

from datetime import timedelta

import pytest

from parking.db.models import FlowEvent
from parking.messages import CameraHealthMsg
from parking.push.admin_alerts import (
    Condition,
    FlowTally,
    Notice,
    alert_payload,
    clamp_counts,
    conditions,
    flow_tallies,
    mismatch_condition,
)
from tests.integration.test_ingest import (
    AUTH,
    LOT_YAML,
    T0,
    clock,  # noqa: F401
    db_rows,
    flow,
    health,
    lot,  # noqa: F401
    make_client,
    obs,
    runtime,
)

BARRIER = """  - id: barrier-ramp
    role: barrier
    zones: [underground]
    source: "gpio:/dev/gpiochip0?in=17&out=27"
"""


def car(event_id, direction="in", minutes=0.0):
    ts = (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return flow(event_id, direction) | {
        "source": "barrier",
        "camera_id": "barrier-ramp",
        "cls": "vehicle",
        "confidence": 1.0,
        "ts": ts,
    }


def post(client, *events):
    r = client.post("/internal/flow-events", json={"events": list(events)}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def underground(client):
    return runtime(client).store.status().zones[1]


@pytest.fixture
def both(lot):  # noqa: F811
    (lot / "config" / "lot.yaml").write_text(
        LOT_YAML.replace("capacity: 3", "capacity: 9") + BARRIER
    )
    return lot


@pytest.fixture
def alone(lot):  # noqa: F811
    yaml = LOT_YAML.split("  - id: cam-ramp")[0] + BARRIER
    (lot / "config" / "lot.yaml").write_text(yaml)
    return lot


def test_barrier_alone_counts_the_zone(alone, clock):  # noqa: F811
    with make_client(alone, clock) as client:
        client.post("/internal/observations", json=obs({"G01"}), headers=AUTH)
        client.post("/internal/health", json=health("barrier-ramp"), headers=AUTH)
        assert underground(client).stale is False
        assert post(client, car("a"), car("b"), car("a")) == {"accepted": 2, "duplicates": 1}
        assert post(client, car("c", "out")) == {"accepted": 1, "duplicates": 0}
        zone = underground(client)
        assert (zone.occupied, zone.free, zone.method) == (1, 2, "flow")
        assert client.get("/healthz").json()["cameras"]["barrier-ramp"] == "ok"
        # the zone follows the barrier's health, as it follows a flow camera's
        client.post("/internal/health", json=health("barrier-ramp", "down"), headers=AUTH)
        assert underground(client).stale is True
    rows = {
        r["event_id"]: (r["source"], r["applied"], r["counted"]) for r in db_rows(alone, FlowEvent)
    }
    assert rows == {k: ("barrier", True, True) for k in "abc"}
    with make_client(alone, clock) as client:  # restored after a restart
        assert runtime(client).store.flow["underground"].occupied == 1


def test_the_source_must_match_the_config(both, clock):  # noqa: F811
    with make_client(both, clock) as client:
        for event, message in [
            (car("a") | {"source": "camera"}, "unknown flow camera 'barrier-ramp'"),
            (flow("b") | {"source": "barrier"}, "unknown barrier 'cam-ramp'"),
            (car("c") | {"camera_id": "cam-ground"}, "unknown barrier 'cam-ground'"),
            (car("d") | {"source": "loop"}, "invalid FlowEventBatch: events.0.source"),
        ]:
            r = client.post("/internal/flow-events", json={"events": [event]}, headers=AUTH)
            assert r.status_code == 422 and message in r.json()["error"]["message"], r.text
        assert post(client, flow("old")) == {
            "accepted": 1,
            "duplicates": 0,
        }  # no `source`: a camera
    assert db_rows(both, FlowEvent)[0]["source"] == "camera"


def test_with_both_the_barrier_counts_and_the_camera_is_recorded(both, clock):  # noqa: F811
    with make_client(both, clock) as client:
        store = runtime(client).store
        assert store.counts_flow("barrier-ramp") and not store.counts_flow("cam-ramp")
        client.post("/internal/health", json=health("cam-ramp"), headers=AUTH)
        assert underground(client).updated_at is None  # the camera alone doesn't make it live
        client.post("/internal/health", json=health("barrier-ramp"), headers=AUTH)
        assert underground(client).stale is False
        assert post(client, car("b1"), car("b2")) == {"accepted": 2, "duplicates": 0}
        assert post(client, flow("c1"), flow("c1"), flow("c2", "out")) == {
            "accepted": 2,
            "duplicates": 1,
        }
        assert post(client, flow("c1")) == {"accepted": 0, "duplicates": 1}
        assert underground(client).occupied == 2
        assert store.flow["underground"].events_since_correction == 2
        # the camera going down doesn't make the zone stale; the barrier going down does
        client.post("/internal/health", json=health("cam-ramp", "down"), headers=AUTH)
        assert underground(client).stale is False
        client.post("/internal/health", json=health("barrier-ramp", "down"), headers=AUTH)
        assert underground(client).stale is True
    rows = {
        r["event_id"]: (r["source"], r["applied"], r["counted"]) for r in db_rows(both, FlowEvent)
    }
    assert rows == {
        "b1": ("barrier", True, True),
        "b2": ("barrier", True, True),
        "c1": ("camera", False, False),
        "c2": ("camera", False, False),
    }


def test_counted_by_puts_the_camera_in_charge(both, clock):  # noqa: F811
    path = both / "config" / "lot.yaml"
    path.write_text(path.read_text().replace("capacity: 9}", "capacity: 9, counted_by: cam-ramp}"))
    with make_client(both, clock) as client:
        post(client, car("b1"), flow("c1"), flow("c2"))
        assert underground(client).occupied == 2
    counted = {r["event_id"]: r["counted"] for r in db_rows(both, FlowEvent)}
    assert counted == {"b1": False, "c1": True, "c2": True}


def ok(camera):
    return CameraHealthMsg(camera_id=camera, ts=T0, state="ok")


def test_tallies_and_the_mismatch_alert(both, clock):  # noqa: F811
    both_ok = {"barrier-ramp": ok("barrier-ramp"), "cam-ramp": ok("cam-ramp")}
    with make_client(both, clock) as client:
        rt = runtime(client)
        now = clock.now()
        assert flow_tallies(rt.engine, rt.config, now) == {
            "underground": (FlowTally("barrier-ramp", "barrier"), FlowTally("cam-ramp", "camera"))
        }
        # the barrier saw 5 in and 1 out, the camera missed two of the cars coming in
        post(client, *[car(f"b{i}") for i in range(5)], car("bo", "out"))
        post(client, *[flow(f"c{i}") for i in range(3)], flow("co", "out"))
        barrier, camera = flow_tallies(rt.engine, rt.config, now)["underground"]
        assert (barrier.cars_in, barrier.cars_out, barrier.net) == (5, 1, 4)
        assert (camera.cars_in, camera.cars_out, camera.net) == (3, 1, 2)
        assert mismatch_condition("underground", (barrier, camera), both_ok) is None  # 2 apart
        post(client, car("b5"))
        tallies = flow_tallies(rt.engine, rt.config, now)
        found = conditions(rt.config, both_ok, rt.store.status(), {}, tallies)
        assert found == [
            Condition(
                "flow_mismatch", "underground", "barrier +5 (in 6, out 1), camera +2 (in 3, out 1)"
            )
        ]
        # not while one of the two is down or has never reported
        down = both_ok | {"cam-ramp": CameraHealthMsg(camera_id="cam-ramp", ts=T0, state="down")}
        assert mismatch_condition("underground", tallies["underground"], down) is None
        assert (
            mismatch_condition("underground", tallies["underground"], {"cam-ramp": ok("c")}) is None
        )
        # the camera's uncounted events are not clamps
        assert clamp_counts(rt.engine, rt.config, now) == {"underground": 0}
        # a correction starts the comparison again
        clock.advance(60)
        client.portal.call(rt.ingestor.correct, "underground", 5, "admin-token", "counted")
        assert flow_tallies(rt.engine, rt.config, clock.now())["underground"][0].net == 0
        post(client, car("late", minutes=2))
        assert flow_tallies(rt.engine, rt.config, clock.now())["underground"][0].net == 1

    notice = Notice(found[0], T0)
    alert = alert_payload(notice, rt.config, "https://p.example/#/", "UTC")
    assert alert["title"] == "Admin: Underground: camera and barrier disagree"
    assert alert["body"] == (
        "Today: barrier +5 (in 6, out 1), camera +2 (in 3, out 1). "
        "Check the one that is off, then correct the count"
    )
    assert alert["tag"] == "admin-flow_mismatch:underground"
    done = alert_payload(Notice(found[0], T0, T0 + timedelta(minutes=9)), rt.config, "u", "UTC")
    assert done["title"] == "Admin: Underground: camera and barrier agree again"
    down = Notice(Condition("camera_down", "barrier-ramp", "connect_failed"), T0)
    assert alert_payload(down, rt.config, "u")["title"] == "Admin: Barrier barrier-ramp is down"
    back = Notice(down.condition, T0, T0)
    assert alert_payload(back, rt.config, "u")["title"] == "Admin: Barrier barrier-ramp is back up"


def test_no_tallies_for_a_zone_with_one_source(alone, clock):  # noqa: F811
    with make_client(alone, clock) as client:
        rt = runtime(client)
        post(client, car("a"))
        assert flow_tallies(rt.engine, rt.config, clock.now()) == {}
