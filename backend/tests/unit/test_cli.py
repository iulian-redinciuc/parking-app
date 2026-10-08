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
