"""Barrier worker (workers/barrier_worker.py, barrier.md) on replayed contacts and a fake GPIO."""

import io
import json
import uuid

import httpx
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.config import Settings
from parking.messages import FlowEventBatch
from parking.workers import barrier_worker
from parking.workers.api_client import ApiClient
from parking.workers.barrier_worker import BarrierWorker
from parking.workers.base import WorkerError
from tests.unit.test_contacts import FakeGpiod

LOT = """
version: 1
lot: {{id: main, name: P, location: {{lat: 0, lon: 0}}, timezone: UTC}}
zones:
  - {{id: underground, name: {{en: U}}, method: flow, capacity: 10}}
cameras:
  - id: barrier-ramp
    role: barrier
    zones: [underground]
    source: "{source}"
    barrier: {barrier}
"""
# two cars in (the second one's relay chatters), one out
PULSES = "0,in,closed\n0.3,in,open\n5,in,closed\n5.02,in,open\n5.04,in,closed\n9,out,closed\n"
# one car a -> b, one that backs out, one b -> a
LOOPS = (
    "0,a,closed\n1,b,closed\n2,a,open\n3,b,open\n"
    "10,a,closed\n11,b,closed\n12,b,open\n13,a,open\n"
    "20,b,closed\n21,a,closed\n22,b,open\n23,a,open\n"
)


def write_lot(root, source="contacts:contacts.csv?realtime=false", barrier="{}"):
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "lot.yaml").write_text(LOT.format(source=source, barrier=barrier))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    write_lot(tmp_path)
    (tmp_path / "contacts.csv").write_text(PULSES)
    monkeypatch.chdir(tmp_path)
    for var in ("WORKER_TOKEN", "API_INTERNAL_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def make_worker(root, api=None, **kw):
    out = io.StringIO()
    api = api or ApiClient(None, None, "barrier-ramp", print_mode=True, out=out)
    w = BarrierWorker(
        root / "config" / "lot.yaml",
        "barrier-ramp",
        api=api,
        settings=Settings(_env_file=None),
        **kw,
    )
    return w, out


def printed(out: io.StringIO):
    return [json.loads(line) for line in out.getvalue().splitlines()]


def events_of(out):
    return [e for m in printed(out) if "events" in m for e in m["events"]]


def test_pulses_become_barrier_events(repo):
    w, out = make_worker(repo)
    assert w.control_port is None
    w.run(handle_signals=False)
    events = events_of(out)
    assert [e["direction"] for e in events] == ["in", "in", "out"]
    assert [e["track_id"] for e in events] == [1, 2, 3]
    for e in events:
        uuid.UUID(e["event_id"])
        assert (e["source"], e["camera_id"]) == ("barrier", "barrier-ramp")
        assert (e["cls"], e["confidence"]) == ("vehicle", 1.0)
    assert events[0]["ts"] < events[1]["ts"] < events[2]["ts"]
    assert w.counts == {"in": 2, "out": 1}
    health = [m for m in printed(out) if "state" in m]
    assert health[0]["state"] == "ok" and health[0]["fps"] is None
    assert health[-1]["state"] == "down" and health[-1]["issue"] is None  # final health


def test_pair_mode(repo):
    write_lot(repo, barrier="{mode: pair, in_direction: b_to_a}")
    (repo / "contacts.csv").write_text(LOOPS)
    w, out = make_worker(repo)
    w.run(handle_signals=False)
    assert [e["direction"] for e in events_of(out)] == ["out", "in"]


def test_max_events_stops_the_loop(repo):
    w, out = make_worker(repo)
    w.run(1, handle_signals=False)
    assert len(events_of(out)) >= 1 and w.events >= 1


def test_events_go_through_the_outbox(repo):
    bodies = []

    def handler(request):
        if request.url.path == "/internal/flow-events":
            batch = FlowEventBatch.model_validate_json(request.content)
            bodies.append(batch)
            return httpx.Response(200, json={"accepted": len(batch.events), "duplicates": 0})
        return httpx.Response(204)

    api = ApiClient(
        "http://api.test",
        "token",
        "barrier-ramp",
        outbox_dir=repo / "data" / "outbox",
        transport=httpx.MockTransport(handler),
    )
    w, _ = make_worker(repo, api=api)
    w.run(handle_signals=False)
    sent = [e for b in bodies for e in b.events]
    assert [(e.source, e.direction) for e in sent] == [
        ("barrier", "in"),
        ("barrier", "in"),
        ("barrier", "out"),
    ]
    assert not (repo / "data" / "outbox" / "barrier-ramp.jsonl").exists()


def test_wrong_configuration_stops_the_start(repo):
    write_lot(repo, barrier="{mode: pair}")  # the file has in/out, not a/b
    with pytest.raises(WorkerError, match="barrier 'barrier-ramp' source: mode pair needs"):
        make_worker(repo)
    write_lot(repo, source="rtsp://camera")
    with pytest.raises(WorkerError, match="unknown contact source"):
        make_worker(repo)
    write_lot(repo, source="contacts:contacts.csv?realtime=false")
    (repo / "contacts.csv").write_text("0,in,ajar\n")
    w, _ = make_worker(repo)  # a file that can't be read is "not now", like a missing chip
    assert w.source is None and "line 1" in w.error


def test_gpio_that_cannot_be_opened_is_down_then_recovers(repo, monkeypatch, caplog):
    monkeypatch.setattr(barrier_worker, "RETRY_S", 0.01)
    monkeypatch.setattr(barrier_worker, "READ_TIMEOUT_S", 0.01)
    write_lot(repo, source="gpio:/dev/gpiochip0?in=17&out=27")
    fake = FakeGpiod(PermissionError())
    caplog.set_level("INFO", logger="parking.workers.barrier_worker")
    w, out = make_worker(repo, gpiod=fake)
    assert w.source is None
    msg = w.health_message()
    assert (msg.state, msg.issue) == ("down", "connect_failed")
    assert "no permission to open /dev/gpiochip0" in caplog.text

    rounds = 0
    real_alive = w.alive

    def alive(interval=0.0):
        nonlocal rounds
        rounds += 1
        if rounds == 3:
            fake.error = None  # the device is there now
            fake.values[27] = fake.line.Value.ACTIVE
        if rounds == 5:
            fake.edge(17, True)
        if rounds == 7:
            fake.fail_read = OSError(19, "No such device")
        if rounds == 9:
            w.stop_event.set()
        real_alive(interval)

    w.alive = alive
    w.loop()
    assert caplog.text.count("no permission") == 1  # the same reason is logged once
    assert "contacts open (in, out), closed now: out" in caplog.text
    assert [e["direction"] for e in events_of(out)] == ["in"]
    assert "reading /dev/gpiochip0 failed" in caplog.text
    assert fake.released


def test_cli(repo):
    args = ["worker", "barrier", "--camera", "barrier-ramp", "--config", "config/lot.yaml"]
    result = CliRunner().invoke(app, [*args, "--print"])
    assert result.exit_code == 0, result.output
    msgs = [json.loads(x) for x in result.stdout.splitlines() if x.startswith("{")]
    events = [e for m in msgs if "events" in m for e in m["events"]]
    assert [e["direction"] for e in events] == ["in", "in", "out"]
    assert CliRunner().invoke(app, [*args, "--print", "--max-events", "-1"]).exit_code == 2
    result = CliRunner().invoke(app, ["worker", "flow", "--camera", "barrier-ramp", "--print"])
    assert result.exit_code == 1 and "is a barrier camera, not flow" in result.output
