"""Flow worker (workers/flow_worker.py) on a generated ramp clip: real gate and counter, a
colour-blob backend instead of YOLO + ByteTrack."""

import io
import json
import uuid

import cv2
import httpx
import numpy as np
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.config import Settings
from parking.messages import FlowEventBatch
from parking.vision.detector import Detection
from parking.vision.sources import Frame
from parking.workers import flow_worker
from parking.workers.api_client import ApiClient
from parking.workers.base import WorkerError
from parking.workers.flow_worker import FlowWorker
from tests.unit.test_motion import FPS, H, W, lines, noisy, with_car

# grey only, so the red car is the one red thing in the frame
BG = cv2.cvtColor(np.tile(np.linspace(40, 160, W, dtype=np.uint8), (H, 1)), cv2.COLOR_GRAY2BGR)

LOT = """
version: 1
lot: {{id: main, name: P, location: {{lat: 0, lon: 0}}, timezone: UTC}}
zones:
  - {{id: underground, name: {{en: U}}, method: flow, capacity: 10}}
  - {{id: ground, name: {{en: G}}, method: count, capacity: 10}}
cameras:
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "{source}"
    lines_file: config/lines/cam-ramp.json
    detector: {{model: models/none}}
  - id: cam-yard
    role: occupancy
    zones: [ground]
    source: "file:x.jpg"
    slots_file: config/slots/cam-yard.json
    detector: {{model: models/none}}
"""


class BlobBackend:
    """'Detects' the red car drawn by `with_car` and gives it id 1 (again after a reset)."""

    def __init__(self, *_, **__):
        self.calls = 0

    def track(self, image):
        self.calls += 1
        b, g, r = cv2.split(image)
        ys, xs = np.nonzero((r > 150) & (g < 90) & (b < 90))
        if len(xs) < 200:
            return []
        box = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
        return [Detection("car", 0.87, box, track_id=1)]

    def idle(self, image):
        pass

    def reset(self):
        pass


def ramp_clip(path):
    """40 s at 10 fps: a car drives down across A then B (IN), 15 s quiet, one drives up (OUT)."""
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    assert w.isOpened()
    frames = [noisy(BG, i) for i in range(50)]  # 5 s to learn the background
    frames += [noisy(with_car(BG, 260, y), i) for i, y in enumerate(range(90, 300, 6))]
    frames += [noisy(BG, i) for i in range(150)]
    frames += [noisy(with_car(BG, 260, y), i) for i, y in enumerate(range(300, 90, -6))]
    frames += [noisy(BG, i) for i in range(400 - len(frames))]
    for img in frames:
        w.write(img)
    w.release()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config" / "lines").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(
        LOT.format(source="video:ramp.avi?realtime=false")
    )
    (tmp_path / "config/lines/cam-ramp.json").write_text(lines().model_dump_json())
    ramp_clip(tmp_path / "ramp.avi")
    monkeypatch.chdir(tmp_path)
    for var in ("WORKER_TOKEN", "API_INTERNAL_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def make_worker(root, api=None, **kw):
    out = io.StringIO()
    api = api or ApiClient(None, None, "cam-ramp", print_mode=True, out=out)
    w = FlowWorker(
        root / "config" / "lot.yaml",
        "cam-ramp",
        api=api,
        settings=Settings(_env_file=None),
        control_port=None,
        backend_factory=lambda cam: BlobBackend(),
        **kw,
    )
    return w, out


def printed(out: io.StringIO):
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_counts_one_in_and_one_out(repo):
    video = repo / "out" / "flow.mp4"
    w, out = make_worker(repo, debug_video=video)
    w.run(handle_signals=False)
    lines_out = printed(out)
    events = [e for m in lines_out if "events" in m for e in m["events"]]
    assert [e["direction"] for e in events] == ["in", "out"]
    assert len({e["track_id"] for e in events}) == 2  # a new id after the idle reset
    for e in events:
        uuid.UUID(e["event_id"])
        assert e["camera_id"] == "cam-ramp" and e["cls"] == "car" and e["confidence"] == 0.87
    assert events[0]["ts"] < events[1]["ts"]
    assert w.counts == {"in": 1, "out": 1} and w.frames == 400
    assert lines_out[-1]["state"] == "down"  # final health
    assert video.stat().st_size > 0
    cap = cv2.VideoCapture(str(video))
    assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 400
    cap.release()


def test_events_go_through_the_outbox(repo):
    bodies = []

    def handler(request):
        bodies.append(FlowEventBatch.model_validate_json(request.content))
        n = len(bodies[-1].events) if request.url.path.endswith("flow-events") else 0
        return httpx.Response(200, json={"accepted": n, "duplicates": 0})

    api = ApiClient(
        "http://api",
        "t",
        "cam-ramp",
        outbox_dir=repo / "outbox",
        transport=httpx.MockTransport(
            lambda r: handler(r) if "flow" in r.url.path else httpx.Response(204)
        ),
    )
    w, _ = make_worker(repo, api=api)
    w.run(handle_signals=False)
    sent = [e.direction for b in bodies for e in b.events]
    assert sent == ["in", "out"]
    assert not (repo / "outbox" / "cam-ramp.jsonl").exists()


def test_health_stats_cover_the_last_10_s(repo, monkeypatch):
    w, _ = make_worker(repo)
    assert w.window_stats(0.0) == {"fps": None, "inference_ms_avg": None, "gate_active_ratio": None}
    w._started_at = 0.0
    # 100 frames over the last 10 s, a quarter gated in at 80 ms; an old one drops out
    w._recent.extend([(-5.0, True, 999.0)])
    w._recent.extend((100 + i * 0.1, i % 4 == 0, 80.0 if i % 4 == 0 else 1.0) for i in range(100))
    stats = w.window_stats(110.0)
    assert stats == {"fps": 10.0, "inference_ms_avg": 80.0, "gate_active_ratio": 0.25}
    msg = w.health_message()
    assert msg.camera_id == "cam-ramp" and msg.state == "ok"
    assert w.health_message(final=True).state == "down"
    w.api.close()


def test_no_frames_reports_connect_failed(repo, monkeypatch):
    (repo / "ramp.avi").unlink()
    monkeypatch.setattr(flow_worker, "NO_FRAME_S", 0.0)
    w, _ = make_worker(repo)
    assert w.step() is False
    assert w.health.last_issue == "connect_failed"
    w.api.close()


def test_gap_in_the_frames_resets_gate_and_tracker(repo):
    w, _ = make_worker(repo)
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 10, 10, tzinfo=UTC)
    seq = iter(range(1, 100))

    class Src:
        replay, exhausted = False, False
        frames = [t0, t0 + timedelta(seconds=0.1), t0 + timedelta(seconds=30)]

        def read(self):
            return Frame(BG, self.frames.pop(0), seq=next(seq)) if self.frames else None

        def close(self):
            pass

    w._setup.source = Src()
    before = w._setup.tracker.resets
    assert w.step() and w.step() and w._setup.tracker.resets == before
    assert w.step() and w._setup.tracker.resets == before + 1
    assert w.step() is False  # no new frame
    w.api.close()


def test_reload_rereads_the_line_file_and_keeps_ids_rising(repo):
    w, _ = make_worker(repo)
    w._setup.tracker._max_id = 7
    (repo / "config/lines/cam-ramp.json").write_text(lines(in_direction="b_to_a").model_dump_json())
    assert w.reload() == {"camera_id": "cam-ramp", "in_direction": "b_to_a"}
    assert w._setup.counter.lines.in_direction == "b_to_a"
    assert w._setup.tracker._offset == 7
    (repo / "config/lines/cam-ramp.json").write_text("{}")
    with pytest.raises(ValueError, match="line file"):
        w.reload()
    assert w._setup.lines.in_direction == "b_to_a"  # the old setup keeps running
    w.api.close()


def test_snapshot_and_save_reference(repo):
    w, _ = make_worker(repo)
    assert w.snapshot(True) is None
    with pytest.raises(ValueError):
        w.save_reference()
    w.run(max_frames=5, handle_signals=False)
    for annotated in (False, True):
        jpg = w.snapshot(annotated)
        assert jpg is not None and jpg[:2] == b"\xff\xd8"
    assert w.save_reference()["saved"] == "data/reference/cam-ramp.jpg"
    assert (repo / "data/reference/cam-ramp.jpg").is_file()


def test_setup_errors(repo):
    with pytest.raises(WorkerError, match="occupancy camera, not flow"):
        FlowWorker(repo / "config/lot.yaml", "cam-yard", print_mode=True, control_port=None)
    with pytest.raises(WorkerError, match="model .* not found"):
        FlowWorker(repo / "config/lot.yaml", "cam-ramp", print_mode=True, control_port=None)
    (repo / "config/lines/cam-ramp.json").unlink()
    with pytest.raises(WorkerError, match="line file"):
        make_worker(repo)


def test_cli_worker_flow(repo, monkeypatch):
    from parking.vision import tracking

    (repo / "models" / "none").mkdir(parents=True)
    monkeypatch.setattr(tracking, "UltralyticsTracker", BlobBackend)
    args = ["worker", "flow", "--camera", "cam-ramp", "--config", "config/lot.yaml"]
    result = CliRunner().invoke(app, [*args, "--print", "--control-port", "0"])
    assert result.exit_code == 0, result.output
    msgs = [json.loads(x) for x in result.stdout.splitlines() if x.startswith("{")]
    assert [e["direction"] for m in msgs if "events" in m for e in m["events"]] == ["in", "out"]
    result = CliRunner().invoke(app, [*args, "--print", "--max-frames", "-1"])
    assert result.exit_code == 2


def test_loop_beats_the_heartbeat_file(repo, tmp_path):
    from parking.workers.heartbeat import LoopHeartbeat, check

    w, _ = make_worker(repo)
    path = tmp_path / "heartbeat"
    w.loop_heartbeat = LoopHeartbeat(path)
    w.run(handle_signals=False)
    assert check(path)[0] and path.read_text() == "20\n"
    assert w.health_message().started_at == w.started_at
    assert w.health_message().disk_pct is not None
