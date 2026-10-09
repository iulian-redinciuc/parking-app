import pytest
from typer.testing import CliRunner

from parking import __version__
from parking.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"parking {__version__}"
    assert __version__ == "0.1.0"


def test_sub_apps_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("models", "worker", "db", "push", "admin"):
        assert name in result.output


def _write_lot(tmp_path, monkeypatch):
    """A tiny repo: config/lot.yaml + slot file, an image and a detection sidecar."""
    import json

    import cv2
    import numpy as np

    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(
        """
version: 1
lot: {id: main, name: P, location: {lat: ${LOT_LAT}, lon: 0}, timezone: UTC}
zones:
  - {id: ground, name: {en: Ground}, method: slots}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: file:img.jpg
    slots_file: config/slots/cam-ground.json
    detector: {model: models/none, conf: 0.5}
"""
    )
    rect = [[0, 0], [100, 0], [100, 100], [0, 100]]
    shifted = [[100, 0], [200, 0], [200, 100], [100, 100]]
    (tmp_path / "config" / "slots" / "cam-ground.json").write_text(
        json.dumps(
            {
                "version": 1,
                "camera_id": "cam-ground",
                "image_size": [200, 100],
                "slots": [
                    {"id": "G01", "zone": "ground", "polygon": rect},
                    {"id": "G02", "zone": "ground", "polygon": shifted},
                ],
            }
        )
    )
    cv2.imwrite(str(tmp_path / "img.jpg"), np.full((100, 200, 3), 90, np.uint8))
    dets = [
        {"cls": "car", "conf": 0.9, "box": [0, 0, 100, 100], "mask": rect},
        {"cls": "car", "conf": 0.2, "box": [100, 0, 200, 100]},  # below conf 0.5
    ]
    (tmp_path / "img.json").write_text(json.dumps({"detections": dets}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOT_LAT", "45")


def test_analyze_with_fake_detector(tmp_path, monkeypatch):
    import json

    _write_lot(tmp_path, monkeypatch)
    result = runner.invoke(
        app, ["analyze", "--image", "img.jpg", "--camera", "cam-ground", "--fake-detector"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith("ground: 1 free / 2 (1 taken) in ")
    obs = json.loads((tmp_path / "out" / "analyze" / "img.json").read_text())
    assert [s["taken"] for s in obs["slots"]] == [True, False]
    assert obs["detections"] == 1
    assert obs["totals"] == {"ground": {"free": 1, "taken": 1, "capacity": 2}}
    assert (tmp_path / "out" / "analyze" / "img.png").stat().st_size > 0


def test_analyze_flags_override_config(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    monkeypatch.setenv("PARKING_FAKE_DETECTOR", "1")
    # box_bottom covers 35% of G01: free with --threshold 0.5
    args = ["analyze", "--image", "img.jpg", "--camera", "cam-ground", "--out", "o"]
    result = runner.invoke(app, [*args, "--mode", "box_bottom", "--threshold", "0.5"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("ground: 2 free / 2 (0 taken)")
    assert (tmp_path / "o" / "img.json").is_file()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--camera", "nope"], "camera 'nope' is not in"),
        (["--camera", "cam-ground", "--image", "missing.jpg"], "can't read image"),
        (["--camera", "cam-ground"], "not found; run `parking models export`"),
    ],
)
def test_analyze_errors(tmp_path, monkeypatch, extra, message):
    _write_lot(tmp_path, monkeypatch)
    args = ["analyze", "--image", "img.jpg", "--camera", "cam-ground"]
    result = runner.invoke(app, [*args, *extra])
    assert result.exit_code == 1
    assert message in result.output


def test_analyze_rejects_bad_threshold(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    args = ["analyze", "--image", "img.jpg", "--camera", "cam-ground", "--threshold", "1.5"]
    assert runner.invoke(app, args).exit_code == 2


def _write_labels(tmp_path, taken, camera="cam-ground"):
    import json

    (tmp_path / "data" / "labels").mkdir(parents=True, exist_ok=True)
    labels = {
        "version": 1,
        "camera_id": camera,
        "images": {"img.jpg": {"conditions": ["day"], "taken": taken, "unsure": []}},
    }
    (tmp_path / "data" / "labels" / "cam-ground.json").write_text(json.dumps(labels))


def test_evaluate_with_fake_detector(tmp_path, monkeypatch):
    import json

    _write_lot(tmp_path, monkeypatch)
    _write_labels(tmp_path, ["G01"])
    args = ["evaluate", "--images", ".", "--camera", "cam-ground", "--fake-detector"]
    result = runner.invoke(app, [*args, "--sweep", "0.1:0.5:0.2", "--mode", "both"])
    assert result.exit_code == 0, result.output
    assert "overall: slot accuracy 100.0% (2/2), free-precision 100.0%" in result.output
    assert "count error 0.00" in result.output
    assert "best threshold (mask): 0.30" in result.output
    assert "best threshold (box_bottom): 0.30" in result.output  # 35% box bottom vs 0.5
    [report] = (tmp_path / "out" / "eval").glob("cam-ground-*.json")
    data = json.loads(report.read_text())
    assert set(data["modes"]) == {"mask", "box_bottom"}
    assert data["modes"]["mask"]["overall"]["accuracy"] == 1.0
    assert [r["threshold"] for r in data["modes"]["mask"]["sweep"]] == [0.1, 0.3, 0.5]
    assert data["modes"]["box_bottom"]["sweep"][2]["accuracy"] == 0.5
    assert not list((tmp_path / "out" / "eval").glob("*.png"))  # no mistakes


def test_evaluate_lists_mistakes(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    _write_labels(tmp_path, ["G01", "G02"])
    (tmp_path / "other.jpg").write_bytes((tmp_path / "img.jpg").read_bytes())
    args = ["evaluate", "--images", ".", "--camera", "cam-ground", "--fake-detector"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "not labelled, skipped: other.jpg" in result.output
    assert "G02 (taken, said free)" in result.output
    assert "slot accuracy 50.0% (1/2), free-precision 0.0%" in result.output
    assert "best threshold" not in result.output  # only with --sweep
    assert (tmp_path / "out" / "eval" / "cam-ground-img-mask.png").stat().st_size > 0


@pytest.mark.parametrize(
    ("taken", "camera", "extra", "message"),
    [
        (["G99"], "cam-ground", [], "not in the slot file: img.jpg: G99"),
        (["G01"], "cam-x", [], "are for camera 'cam-x'"),
        (["G01"], "cam-ground", ["--images", "nope"], "image folder"),
        (["G01"], "cam-ground", ["--labels", "missing.json"], "file not found"),
    ],
)
def test_evaluate_errors(tmp_path, monkeypatch, taken, camera, extra, message):
    _write_lot(tmp_path, monkeypatch)
    _write_labels(tmp_path, taken, camera)
    args = ["evaluate", "--images", ".", "--camera", "cam-ground", "--fake-detector"]
    result = runner.invoke(app, [*args, *extra])
    assert result.exit_code == 1
    assert message in result.output


def test_evaluate_uses_detection_cache(tmp_path, monkeypatch):
    """Without --fake-detector a matching cache file means the model is never loaded."""
    import json

    _write_lot(tmp_path, monkeypatch)
    _write_labels(tmp_path, ["G01"])
    from parking.vision.evaluate import _cache_key, cache_path

    settings = {"conf": 0.5, "classes": ["car", "motorcycle", "bus", "truck"], "use_masks": False}
    path = cache_path(tmp_path / "out" / "cache", tmp_path / "img.jpg", "models/none", 640)
    path.parent.mkdir(parents=True)
    dets = [{"cls": "car", "conf": 0.9, "box": [0, 0, 100, 100], "mask": None}]
    key = _cache_key(tmp_path / "img.jpg", settings)
    path.write_text(json.dumps({"key": key, "frame_size": [200, 100], "detections": dets}))
    result = runner.invoke(app, ["evaluate", "--images", ".", "--camera", "cam-ground"])
    assert result.exit_code == 0, result.output
    assert "0 detected, 1 from cache" in result.output
    assert "slot accuracy 100.0%" in result.output


@pytest.mark.parametrize(
    "extra", [["--sweep", "0.5:0.1:0.1"], ["--mode", "all"], ["--threshold", "0"]]
)
def test_evaluate_rejects_bad_options(tmp_path, monkeypatch, extra):
    _write_lot(tmp_path, monkeypatch)
    _write_labels(tmp_path, ["G01"])
    args = ["evaluate", "--images", ".", "--camera", "cam-ground", "--fake-detector", *extra]
    assert runner.invoke(app, args).exit_code == 2


def test_analyze_appearance_loads_no_model(tmp_path, monkeypatch):
    import json

    _write_lot(tmp_path, monkeypatch)  # models/none doesn't exist
    args = ["analyze", "--image", "img.jpg", "--camera", "cam-ground", "--method", "appearance"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("ground: 2 free / 2 (0 taken)")
    obs = json.loads((tmp_path / "out" / "analyze" / "img.json").read_text())
    assert obs["detections"] == 0


def test_analyze_appearance_reference_errors(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    lot = tmp_path / "config" / "lot.yaml"
    lot.write_text(
        lot.read_text()
        + "    occupancy: {method: appearance, appearance: {reference_empty: nope.jpg}}\n"
    )
    result = runner.invoke(app, ["analyze", "--image", "img.jpg", "--camera", "cam-ground"])
    assert result.exit_code == 1
    assert "can't read reference_empty image" in result.output
    # a readable reference works
    (tmp_path / "nope.jpg").write_bytes((tmp_path / "img.jpg").read_bytes())
    result = runner.invoke(app, ["analyze", "--image", "img.jpg", "--camera", "cam-ground"])
    assert result.exit_code == 0, result.output


def test_evaluate_appearance_single_image(tmp_path, monkeypatch):
    import json

    _write_lot(tmp_path, monkeypatch)
    _write_labels(tmp_path, [])
    args = ["evaluate", "--images", "img.jpg", "--camera", "cam-ground", "--method", "appearance"]
    result = runner.invoke(app, [*args, "--sweep", "0.1:0.5:0.2"])
    assert result.exit_code == 0, result.output
    assert "appearance scoring (no detector)" in result.output
    assert "slot accuracy 100.0% (2/2)" in result.output
    assert "best threshold (appearance)" in result.output
    assert not (tmp_path / "out" / "cache").exists()
    [report] = (tmp_path / "out" / "eval").glob("cam-ground-*.json")
    assert set(json.loads(report.read_text())["modes"]) == {"appearance"}


def test_bad_method_rejected(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    for cmd in (["analyze", "--image", "img.jpg"], ["evaluate"]):
        result = runner.invoke(app, [*cmd, "--camera", "cam-ground", "--method", "x"])
        assert result.exit_code == 2


def _bootstrap(*args):
    return runner.invoke(
        app, ["bootstrap-slots", "--image", "img.jpg", "--camera", "cam-ground", *args]
    )


def test_bootstrap_slots_with_fake_detector(tmp_path, monkeypatch):
    from parking.config import load_slots

    _write_lot(tmp_path, monkeypatch)
    # --conf (not the camera's 0.5) decides which detections count
    result = _bootstrap("--out", "draft.json", "--fake-detector", "--conf", "0.15")
    assert result.exit_code == 0, result.output
    assert "2 slot(s) in zone 'ground' -> draft.json" in result.output
    sf = load_slots(tmp_path / "draft.json")
    assert sf.camera_id == "cam-ground" and sf.image_size == (200, 100)
    assert sf.reference_image == "img.jpg"
    assert [s.id for s in sf.slots] == ["G01", "G02"]


def test_bootstrap_slots_never_overwrites_without_force(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    target = tmp_path / "config" / "slots" / "cam-ground.json"
    before = target.read_text()
    result = _bootstrap("--fake-detector")  # default --out is the camera's slots_file
    assert result.exit_code == 1
    assert "exists; pass --force" in result.output
    assert target.read_text() == before

    result = _bootstrap("--fake-detector", "--force", "--footprint", "box")
    assert result.exit_code == 0, result.output
    assert target.read_text() != before


def test_bootstrap_slots_errors(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    assert _bootstrap("--out", "d.json", "--footprint", "mask").exit_code == 2
    assert _bootstrap("--out", "d.json", "--conf", "1.5").exit_code == 2
    assert _bootstrap("--out", "d.json", "--imgsz", "0").exit_code == 2

    result = _bootstrap("--out", "d.json", "--fake-detector", "--conf", "0.95")
    assert result.exit_code == 1 and "no vehicles found" in result.output
    assert not (tmp_path / "d.json").exists()

    (tmp_path / "img.json").unlink()
    result = _bootstrap("--out", "d.json", "--fake-detector")
    assert result.exit_code == 1 and "no sidecar" in result.output

    result = _bootstrap("--out", "d.json")  # real detector, model missing
    assert result.exit_code == 1 and "not found" in result.output


def test_benchmark_appearance_only(tmp_path, monkeypatch):
    import json

    _write_lot(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["benchmark", "--image", "img.jpg", "--runs", "2", "--warmup", "0", "--runtimes", ""],
    )
    assert result.exit_code == 0, result.output
    assert "| appearance scorer | appearance | full frame |" in result.output
    assert "CPU temperature" in result.output
    (report,) = (tmp_path / "out" / "benchmark").glob("benchmark-*.json")
    data = json.loads(report.read_text())
    assert data["runs"] == 2 and data["results"][0]["case"]["runtime"] == "appearance"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--runtimes", "onnx"], "unknown"),
        (["--imgsz", "big"], "integers"),
        (["--runs", "0"], "at least 1"),
        (["--runtimes", "", "--camera", ""], "nothing to benchmark"),
        (["--image", "missing.jpg"], "can't read image"),
        (["--camera", "cam-x"], "not in"),
    ],
)
def test_benchmark_errors(tmp_path, monkeypatch, extra, message):
    _write_lot(tmp_path, monkeypatch)
    args = ["benchmark", "--image", "img.jpg", *extra]
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert message in result.output


def test_api_runs_the_factory(tmp_path, monkeypatch):
    import uvicorn

    lot = tmp_path / "config" / "lot.yaml"
    lot.parent.mkdir()
    lot.write_text("version: 1\n")
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: calls.append((target, kw)))
    monkeypatch.setenv("PARKING_CONFIG", "unset")  # undone after the test
    result = runner.invoke(
        app, ["api", "--config", str(lot), "--host", "0.0.0.0", "--port", "8123", "--reload"]
    )
    assert result.exit_code == 0, result.output
    ((target, kw),) = calls
    assert target == "parking.api.app:create_app_from_env"
    assert kw["factory"] is True and kw["reload"] is True and kw["port"] == 8123
    assert kw["host"] == "0.0.0.0"
    import os

    assert os.environ["PARKING_CONFIG"] == str(lot.resolve())


def test_health_stats(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta

    from parking.vision.health import HealthMetrics
    from parking.vision.health_stats import format_metrics

    _write_lot(tmp_path, monkeypatch)
    t0 = datetime(2026, 10, 9, tzinfo=UTC)
    lines = [
        "x DEBUG "
        + format_metrics("cam-ground", t0 + timedelta(hours=i), HealthMetrics(m, 100, 2), None)
        for i, m in enumerate([40, 50, 60, 2])
    ]
    lines.append(format_metrics("other", t0, HealthMetrics(1, 1, None), "black"))
    (tmp_path / "w.log").write_text("\n".join(lines) + "\n")
    args = ["health-stats", "w.log", "--camera", "cam-ground"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Suggested health" in result.output and "03h" in result.output

    # --until drops the lens-covered frame at 03:00
    result = runner.invoke(app, [*args, "--json", "--until", "2026-10-09T02:30:00"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["all"]["frames"] == 3 and out["all"]["mean"]["min"] == 40
    assert out["current"]["black_mean_max"] == 12 and out["suggested"]["black_mean_max"] > 12

    result = runner.invoke(
        app, ["health-stats", "w.log", "--camera", "cam-ground", "--since", "2027-01-01"]
    )
    assert result.exit_code == 1 and "LOG_LEVEL=DEBUG" in result.output
    assert runner.invoke(app, [*args, "--since", "nope"]).exit_code == 2


def test_grab_saves_frame(tmp_path, monkeypatch):
    import cv2

    _write_lot(tmp_path, monkeypatch)
    res = CliRunner().invoke(app, ["grab", "--camera", "cam-ground"])
    assert res.exit_code == 0, res.output
    saved = tmp_path / "data" / "reference" / "cam-ground.jpg"
    assert "200x100" in res.output
    assert cv2.imread(str(saved)).shape == (100, 200, 3)

    res = CliRunner().invoke(app, ["grab", "--camera", "cam-ground"])
    assert res.exit_code == 1 and "--force" in res.output
    res = CliRunner().invoke(app, ["grab", "--camera", "cam-ground", "--force"])
    assert res.exit_code == 0, res.output


def test_grab_errors(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    args = ["grab", "--camera", "cam-ground", "--out", "o.jpg"]
    res = CliRunner().invoke(app, [*args, "--source", "file:missing.jpg", "--timeout", "0.3"])
    assert res.exit_code == 1 and "no frame" in res.output
    res = CliRunner().invoke(app, [*args, "--source", "bogus:x"])
    assert res.exit_code == 1 and "unknown source scheme" in res.output
    res = CliRunner().invoke(app, ["grab", "--camera", "nope"])
    assert res.exit_code == 1 and "not in" in res.output
