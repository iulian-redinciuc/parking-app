import json
from datetime import UTC, datetime

import cv2
import numpy as np
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.vision import lighting
from parking.vision.health import FrameHealth, blur_score, small_gray
from parking.vision.sources import Frame


@pytest.fixture
def scene():
    """A textured daytime frame: pavement blocks and a few dark 'cars'."""
    rng = np.random.default_rng(0)
    img = np.full((480, 360, 3), (150, 160, 170), np.uint8)
    img += rng.integers(0, 40, img.shape, dtype=np.uint8)
    for y in range(40, 440, 80):
        cv2.rectangle(img, (30, y), (130, y + 50), (40, 40, 50), -1)
    return img


def metrics(img):
    g = small_gray(img)
    return float(g.mean()), blur_score(g)


@pytest.mark.parametrize(
    ("hour", "phase"),
    [(12, "day"), (7.2, "dusk"), (18.3, "dusk"), (19, "night"), (2, "night"), (6.5, "night")],
)
def test_phase(hour, phase):
    assert lighting.phase(hour) == phase


def test_light_darker_and_softer_towards_night(scene):
    noon, how = lighting.light(scene, 12)
    dusk, _ = lighting.light(scene, 18.5)
    night, how_n = lighting.light(scene, 2)
    rain, how_r = lighting.light(scene, 2, rain=True)
    assert how.phase == "day" and how_n.phase == "night" and how_r.rain
    assert metrics(dusk)[0] < metrics(noon)[0] / 2
    assert 50 < metrics(night)[0] < 90  # the IR picture
    assert np.ptp(night.astype(int), axis=2).max() == 0  # grey
    assert metrics(night)[1] < metrics(noon)[1]
    assert metrics(rain)[1] < metrics(night)[1]
    again, _ = lighting.light(scene, 2, rain=True)
    assert np.array_equal(rain, again)  # same seed and index, same frame


def test_covered_lens_is_black_for_the_default_threshold(scene):
    health = FrameHealth(check_frozen=False)
    assert health.check(Frame(lighting.covered(scene.shape), datetime.now(UTC))) == "black"
    assert health.last_metrics.mean < 8


def test_light_feed_command(tmp_path, scene):
    src = tmp_path / "src"
    src.mkdir()
    cv2.imwrite(str(src / "a.jpg"), scene)
    out = tmp_path / "lit"
    args = ["light-feed", "--source", str(src), "--out", str(out), "--frames", "8"]
    result = CliRunner().invoke(app, [*args, "--start-hour", "0", "--rain", "3-6"])
    assert result.exit_code == 0, result.output
    assert len(list(out.glob("frame-*.jpg"))) == 8
    notes = json.loads((out / "lighting.json").read_text())
    assert notes["frame-0001.jpg"] == {"hour": 0.0, "phase": "night", "rain": False}
    assert notes["frame-0002.jpg"]["rain"] and notes["frame-0005.jpg"]["phase"] == "day"
    assert (tmp_path / "lit-cover.jpg").is_file()
    bad = CliRunner().invoke(app, [*args, "--rain", "3"])
    assert bad.exit_code != 0
