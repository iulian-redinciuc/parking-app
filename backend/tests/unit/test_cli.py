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
