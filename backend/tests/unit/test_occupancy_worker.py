"""Occupancy worker (workers/occupancy_worker.py) on a tiny generated lot."""

import io
import json
import threading

import cv2
import httpx
import numpy as np
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.config import Settings
from parking.workers.api_client import ApiClient
from parking.workers.base import WorkerError
from parking.workers.occupancy_worker import OccupancyWorker

RECT = [[0, 0], [100, 0], [100, 100], [0, 100]]
RIGHT = [[100, 0], [200, 0], [200, 100], [100, 100]]

LOT = """
version: 1
lot: {{id: main, name: P, location: {{lat: 45, lon: 0}}, timezone: UTC}}
zones:
  - {{id: ground, name: {{en: Ground}}, method: slots}}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: "{source}"
    sample_every_s: 5
    slots_file: config/slots/cam-ground.json
    detector: {{model: models/none, conf: 0.5}}
    occupancy: {{method: detector, mode: mask, threshold: 0.5}}
"""


def textured(seed: int) -> np.ndarray:
    """A sharp random image (passes the blur and black checks)."""
    return np.random.default_rng(seed).integers(40, 220, (100, 200, 3), dtype=np.uint8)


def write_slots(root, slots):
    (root / "config" / "slots" / "cam-ground.json").write_text(
        json.dumps(
            {"version": 1, "camera_id": "cam-ground", "image_size": [200, 100], "slots": slots}
        )
    )


@pytest.fixture
def lot(tmp_path, monkeypatch):
    """config/lot.yaml + slots; frames/a.jpg (car in G01) and frames/b.jpg (no sidecar)."""
    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "frames").mkdir()
    (tmp_path / "config" / "lot.yaml").write_text(
        LOT.format(source="folder:frames?interval=0.01&loop=false")
    )
    write_slots(
        tmp_path,
        [
            {"id": "G01", "zone": "ground", "polygon": RECT},
            {"id": "G02", "zone": "ground", "polygon": RIGHT},
        ],
    )
    cv2.imwrite(str(tmp_path / "frames" / "a.jpg"), textured(1))
    cv2.imwrite(str(tmp_path / "frames" / "b.jpg"), textured(2))
    dets = [{"cls": "car", "conf": 0.9, "box": [0, 0, 100, 100], "mask": RECT}]
    (tmp_path / "frames" / "a.json").write_text(json.dumps({"detections": dets}))
    monkeypatch.chdir(tmp_path)
    for var in ("WORKER_TOKEN", "API_INTERNAL_URL", "PARKING_FAKE_DETECTOR"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def make_worker(root, **kw):
    out = io.StringIO()
    api = ApiClient(None, None, "cam-ground", print_mode=True, out=out)
    kw.setdefault("fake_detector", True)
    kw.setdefault("control_port", None)
    w = OccupancyWorker(
        root / "config" / "lot.yaml", "cam-ground", api=api, settings=Settings(), **kw
    )
    return w, out


def lines(out):
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_step_sends_observations_from_sidecars(lot):
    w, out = make_worker(lot)
    assert w.interval == 0.01  # the folder source's interval wins over sample_every_s
    a = w.step()
    b = w.step()
    assert [(s.id, s.taken) for s in a.slots] == [("G01", True), ("G02", False)]
    assert a.detections == 1 and a.frame_size == (200, 100)
    assert b.detections == 0  # no sidecar for b.jpg
    printed = lines(out)
    assert [p["slots"][0]["taken"] for p in printed] == [True, False]
    assert w.observations == 2


def test_unhealthy_frames_are_skipped_and_reported(lot):
    cv2.imwrite(str(lot / "frames" / "a.jpg"), np.zeros((100, 200, 3), np.uint8))  # black
    w, out = make_worker(lot)
    assert w.step() is None
    assert out.getvalue() == ""
    h = w.health_message()
    assert (h.state, h.issue, h.unhealthy_ratio) == ("degraded", "black", 1.0)  # not down yet
    assert w.step() is not None
    assert w.health_message().state == "ok"  # 1 of 2 unhealthy: not over 50%
    assert w.health_message(final=True).state == "down"


def test_missing_frames_are_connect_failed_and_down_after_stale(lot):
    for f in (lot / "frames").iterdir():
        f.unlink()
    w, _ = make_worker(lot)
    assert w.step() is None
    w.health._started_at = w.health._started_at.replace(year=2000)
    h = w.health_message()
    assert (h.state, h.issue, h.last_frame_age_s, h.fps) == ("down", "connect_failed", None, None)


def test_health_message_stats(lot):
    w, _ = make_worker(lot)
    w.step()
    w.step()
    h = w.health_message()
    assert h.state == "ok" and h.issue is None
    assert h.inference_ms_avg is not None and h.last_frame_age_s is not None
    assert h.fps is None or h.fps > 0


def test_loop_stops_when_folder_is_exhausted(lot):
    w, out = make_worker(lot)
    w.run(handle_signals=False)
    printed = lines(out)
    obs = [p for p in printed if "slots" in p]
    health = [p for p in printed if "state" in p]
    assert len(obs) == 2
    assert health[0]["state"] == "ok" and health[-1]["state"] == "down"
    assert w.stop_event.is_set()


def test_loop_max_frames_and_stop(lot):
    (lot / "config" / "lot.yaml").write_text(LOT.format(source="folder:frames?interval=0.01"))
    w, out = make_worker(lot)
    w.loop(max_frames=5)
    assert w.frames == 5  # loops over the two images
    w2, _ = make_worker(lot)
    w2.stop()
    w2.loop()
    assert w2.frames == 0


def test_appearance_needs_no_model_and_file_source(lot):
    (lot / "config" / "lot.yaml").write_text(
        LOT.format(source="file:frames/a.jpg").replace("method: detector", "method: appearance")
    )
    w, _ = make_worker(lot, fake_detector=False)  # models/none doesn't exist: not loaded
    assert w.interval == 5
    assert w.health.check_frozen is False  # replay source
    obs = w.step()
    assert obs.detections == 0 and len(obs.slots) == 2


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda root: (root / "config" / "slots" / "cam-ground.json").unlink(), "slot file"),
        (
            lambda root: (root / "config" / "lot.yaml").write_text(LOT.format(source="rtsp:x")),
            "Phase 4",
        ),
    ],
)
def test_config_errors(lot, edit, message):
    edit(lot)
    with pytest.raises(WorkerError, match=message):
        make_worker(lot)


def test_real_detector_needs_the_model(lot):
    with pytest.raises(WorkerError, match="models export"):
        make_worker(lot, fake_detector=False)


def test_wrong_camera(lot):
    with pytest.raises(WorkerError, match="not in"):
        OccupancyWorker(lot / "config" / "lot.yaml", "cam-x", print_mode=True, settings=Settings())


def test_reload_picks_up_new_slots_and_keeps_old_on_error(lot):
    w, _ = make_worker(lot)
    source = w._setup.source
    write_slots(lot, [{"id": "G01", "zone": "ground", "polygon": RECT}])
    assert w.reload() == {"camera_id": "cam-ground", "slots": 1, "interval_s": 0.01}
    assert w._setup.source is source  # unchanged source is kept (its position too)
    assert [s.id for s in w.step().slots] == ["G01"]
    (lot / "config" / "slots" / "cam-ground.json").write_text("{broken")
    with pytest.raises(ValueError, match="slot file"):
        w.reload()
    assert len(w._setup.slot_file.slots) == 1


def test_snapshot_and_save_reference(lot):
    w, _ = make_worker(lot)
    assert w.snapshot(annotated=False) is None
    with pytest.raises(ValueError, match="no frame"):
        w.save_reference()
    w.step()
    for annotated in (False, True):
        jpeg = w.snapshot(annotated)
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape == (100, 200, 3)
    assert w.save_reference()["saved"] == "data/reference/cam-ground.jpg"
    assert cv2.imread(str(lot / "data" / "reference" / "cam-ground.jpg")).shape == (100, 200, 3)


def test_run_serves_control_while_running(lot):
    (lot / "config" / "lot.yaml").write_text(LOT.format(source="folder:frames?interval=0.05"))
    out = io.StringIO()
    api = ApiClient(None, None, "cam-ground", print_mode=True, out=out)
    w = OccupancyWorker(
        lot / "config" / "lot.yaml",
        "cam-ground",
        fake_detector=True,
        api=api,
        settings=Settings(worker_token="tok"),
        control_port=0,
        control_host="127.0.0.1",
    )
    t = threading.Thread(target=w.run, kwargs={"handle_signals": False})
    t.start()
    try:
        for _ in range(100):
            if w.control is not None and w.observations:
                break
            threading.Event().wait(0.02)
        url = f"http://127.0.0.1:{w.control.port}/control/snapshot"
        r = httpx.get(url, headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    finally:
        w.stop()
        t.join(timeout=10)
    assert not t.is_alive()
    assert w.control is None  # shut down


def test_cli_worker_occupancy_print(lot):
    (lot / "config" / "lot.yaml").write_text(LOT.format(source="folder:frames?interval=0.01"))
    args = ["worker", "occupancy", "--camera", "cam-ground", "--print", "--fake-detector"]
    result = CliRunner().invoke(app, [*args, "--control-port", "0", "--max-frames", "3"])
    assert result.exit_code == 0, result.output
    obs = [json.loads(x) for x in result.stdout.splitlines() if '"slots"' in x]
    assert len(obs) == 3
    assert obs[0]["camera_id"] == "cam-ground"


def test_cli_worker_occupancy_errors(lot):
    runner = CliRunner()
    r = runner.invoke(app, ["worker", "occupancy", "--camera", "cam-x", "--print"])
    assert r.exit_code == 1 and "not in" in r.output
    r = runner.invoke(app, ["worker", "occupancy", "--camera", "cam-ground", "--control-port", "0"])
    assert r.exit_code == 1 and "API_INTERNAL_URL" in r.output
    r = runner.invoke(app, ["worker", "occupancy", "--camera", "cam-ground", "--max-frames", "-1"])
    assert r.exit_code == 2


def test_signal_handler_never_blocks_on_the_stop_event(lot):
    # the handler interrupts the main thread, which may hold the stop event's lock
    w, _ = make_worker(lot)
    with w.stop_event._cond:  # what the main thread holds inside Event.wait/set
        w._on_signal(15, None)
        w._on_signal(15, None)  # a second signal (uv forwarding + timeout) is fine too
        assert not w.stop_event.is_set()
    assert w.stop_event.wait(2)
