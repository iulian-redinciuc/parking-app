import json

import cv2
import numpy as np
from typer.testing import CliRunner

from parking.cli import app
from parking.config import LineFile
from parking.vision.lines import check_lines, draw_lines

runner = CliRunner()

GOOD = {
    "version": 1,
    "camera_id": "cam-ramp",
    "image_size": [640, 360],
    "roi": [[60, 120], [600, 120], [600, 360], [60, 360]],
    "line_a": [[100, 220], [560, 220]],
    "line_b": [[100, 270], [560, 270]],
    "in_direction": "a_to_b",
}


def _lf(**changes) -> LineFile:
    return LineFile.model_validate({**GOOD, **changes})


def test_config_md_example_passes():
    res = check_lines(_lf(), (640, 360))
    assert res.ok and not res.warnings
    assert res.gap == 50 and res.angle == 0 and res.length_a == 460


def test_scaled_reference_is_a_warning_other_shape_an_error():
    assert check_lines(_lf(), (1280, 720)).ok
    assert "scaled" in check_lines(_lf(), (1280, 720)).warnings[0]
    assert "different shape" in check_lines(_lf(), (704, 576)).errors[0]


def test_bad_lines_are_reported():
    crossing = check_lines(_lf(line_b=[[200, 360], [460, 120]]))
    assert any("cross" in e for e in crossing.errors)
    assert any("parallel" in e for e in crossing.errors)
    assert any("long" in e for e in check_lines(_lf(line_a=[[300, 220], [360, 220]])).errors)
    assert any("apart" in e for e in check_lines(_lf(line_b=[[100, 228], [560, 228]])).errors)
    assert any("outside" in e for e in check_lines(_lf(line_a=[[100, 220], [700, 220]])).errors)
    assert any("ROI" in e for e in check_lines(_lf(line_a=[[0, 100], [560, 100]])).errors)
    far = check_lines(_lf(roi=None, line_a=[[20, 40], [20, 340]], line_b=[[620, 40], [620, 340]]))
    assert far.ok and "most of the frame" in far.warnings[0]


def test_draw_lines_scales_to_the_frame():
    img = np.zeros((720, 1280, 3), np.uint8)
    out = draw_lines(img, _lf(in_direction="b_to_a"))
    assert out.shape == img.shape and not img.any()
    assert (out[440, 640] != 0).any() and (out[540, 640] != 0).any()  # line_a, line_b at 2×


def _write_repo(tmp_path, lines):
    (tmp_path / "config" / "lines").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(
        """
version: 1
lot: {id: main, name: P, location: {lat: 0, lon: 0}, timezone: UTC}
zones:
  - {id: underground, name: {en: U}, method: flow, capacity: 10}
cameras:
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: file:x.jpg
    lines_file: config/lines/cam-ramp.json
    detector: {model: models/none}
"""
    )
    (tmp_path / "config" / "lines" / "cam-ramp.json").write_text(json.dumps(lines))
    return ["lines-check", "--camera", "cam-ramp", "--config", str(tmp_path / "config/lot.yaml")]


def test_cli_lines_check(tmp_path):
    args = _write_repo(tmp_path, GOOD)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "no reference frame" in result.output and "ok" in result.output

    (tmp_path / "data" / "reference").mkdir(parents=True)
    cv2.imwrite(str(tmp_path / "data/reference/cam-ramp.jpg"), np.zeros((360, 640, 3), np.uint8))
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out/lines/cam-ramp.jpg").is_file()

    _write_repo(tmp_path / "bad", {**GOOD, "line_b": GOOD["line_a"]})
    result = runner.invoke(app, [*args[:-1], str(tmp_path / "bad/config/lot.yaml")])
    assert result.exit_code == 1 and "cross" in result.output


def test_cli_lines_check_missing_file(tmp_path):
    args = _write_repo(tmp_path, GOOD)
    (tmp_path / "config/lines/cam-ramp.json").unlink()
    result = runner.invoke(app, args)
    assert result.exit_code == 1 and "lines mode" in result.output
