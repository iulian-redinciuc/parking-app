"""The replay feed built from every image source (vision/replay.py, `parking build-replay`)."""

import json

import cv2
import numpy as np
from typer.testing import CliRunner

from parking.cli import app
from parking.config import HealthCfg, ImageLabels, load_labels
from parking.vision import replay as rp
from parking.vision.shift import ShiftDetector
from tests.unit.test_shift import SLOT_POLY, moved, scene, slots, with_cars
from tests.unit.test_simulate import BASE_TAKEN, IDS, SLOT_FILE, base_image
from tests.unit.test_simulate import repo as repo  # noqa: F401 (fixture)


def detector() -> ShiftDetector:
    return ShiftDetector(scene(), slots([SLOT_POLY]), HealthCfg(shift_max_px=8))


def test_same_view_with_other_cars_is_aligned():
    small = cv2.resize(with_cars(scene(), seed=5), (480, 270), interpolation=cv2.INTER_AREA)
    c = rp.check_view(small, detector())
    assert c.status == "aligned" and c.usable
    assert c.image.shape[:2] == (540, 960)  # on the reference framing


def test_a_small_shift_is_warped_back():
    det = detector()
    c = rp.check_view(moved(with_cars(scene(), seed=5), dx=22, dy=-14, angle=1.0), det)
    assert c.status == "warped" and "warped onto the reference" in c.reason
    assert c.measure.displacement_px > 8
    assert det.measure(c.image).displacement_px < 2


def test_another_view_is_not_forced():
    det = detector()
    far = rp.check_view(moved(scene(), dx=150), det)  # over 10% of the width
    assert far.status == "different" and not far.usable and far.image is None
    shape = rp.check_view(scene(w=960, h=960), det)
    assert shape.status == "different" and "shape" in shape.reason
    assert rp.check_view(None, det).status == "unreadable"


def test_too_few_features_is_unmeasured_not_different():
    flat = np.full((540, 960, 3), 120, np.uint8)
    c = rp.check_view(flat, detector())
    assert c.status == "unmeasured" and c.usable and "by eye" in c.reason


def test_stills_fill_up_and_empty_again():
    labels = {f"{n}.jpg": ImageLabels(taken=IDS[:n]) for n in (5, 0, 9, 2, 7)}
    order = rp.up_and_down(labels, labels)
    assert order == ["0.jpg", "5.jpg", "9.jpg", "7.jpg", "2.jpg"]


def test_overlay_draws_on_a_copy():
    img = base_image()
    out = rp.overlay(img, SLOT_FILE)
    assert out.shape == img.shape and not np.array_equal(out, img)
    assert np.array_equal(img, base_image())


# --- CLI ---


def generated(repo, taken: list[str]) -> np.ndarray:  # noqa: F811
    """The base view with other cars: what a generated image of the lot is."""
    from parking.vision import simulate as sim

    return sim.Simulator(base_image(), SLOT_FILE, BASE_TAKEN, seed=2).render(taken)


def test_cli_builds_the_feed_from_all_sources(repo):  # noqa: F811
    runner = CliRunner()
    args = ["simulate-feed", "--camera", "cam-ground", "--base", "data/samples/base.png"]
    assert runner.invoke(app, [*args, "--frames", "4", "--quality", "95"]).exit_code == 0
    (repo / "data/reference").mkdir(exist_ok=True)
    cv2.imwrite(str(repo / "data/reference/cam-ground.png"), base_image())
    gen = repo / "samples/generated"
    gen.mkdir(parents=True)
    full, some = IDS, ["G01", "G07", "B03"]
    cv2.imwrite(str(gen / "full.png"), generated(repo, full))
    cv2.imwrite(str(gen / "some.png"), moved(generated(repo, some), dx=1))
    cv2.imwrite(str(gen / "none.png"), generated(repo, []))
    cv2.imwrite(str(gen / "new.png"), generated(repo, ["G03"]))  # nobody labelled it yet
    cv2.imwrite(str(gen / "wide.png"), np.zeros((300, 1200, 3), np.uint8))  # another view
    images = {"full.png": {"taken": full}, "some.png": {"taken": some}, "none.png": {}}
    (gen / "labels.json").write_text(
        json.dumps({"version": 1, "camera_id": "cam-ground", "images": images})
    )

    build = ["build-replay", "--camera", "cam-ground", "--hold", "2"]
    build += ["--reference", "data/reference/cam-ground.png", "--review", "out/review"]
    result = runner.invoke(app, build)
    assert result.exit_code == 0, result.output
    assert "new.png: not in the feed, not labelled" in result.output
    assert "wide.png: not usable with this slot file" in result.output
    assert "4 simulated, 1 photo(s) and 3 generated image(s) x 2; 2 image(s) left out" in (
        result.output.replace("\n", " ")
    )

    feed = repo / "data/replay/ground-feed"
    frames = sorted(p.name for p in feed.iterdir())
    assert frames[:4] == [f"a-sim-frame-{i:04}.jpg" for i in range(1, 5)]
    assert frames[4:6] == ["b-photo-01-base-01.jpg", "b-photo-01-base-02.jpg"]
    # the stills fill up and empty again: none, full, some
    assert [f[:-7] for f in frames[6::2]] == ["c-gen-01-none", "c-gen-02-full", "c-gen-03-some"]
    labels = load_labels(repo / "data/labels/cam-ground-feed.json")
    assert sorted(labels.images) == frames
    assert labels.images["c-gen-03-some-02.jpg"].taken == some
    assert labels.images["b-photo-01-base-01.jpg"].taken == BASE_TAKEN
    assert labels.images["a-sim-frame-0001.jpg"].conditions == ["simulated"]
    assert sorted(p.name for p in (repo / "out/review").iterdir()) == [
        "base.jpg",
        "full.jpg",
        "new.jpg",
        "none.jpg",
        "some.jpg",
    ]

    # the whole feed reads as its labels
    ev = ["evaluate", "--camera", "cam-ground", "--images", "data/replay/ground-feed"]
    ev += ["--labels", "data/labels/cam-ground-feed.json", "--out", "out/eval"]
    result = runner.invoke(app, ev)
    assert result.exit_code == 0, result.output
    assert f"slot accuracy 100.0% ({12 * len(IDS)}/{12 * len(IDS)})" in result.output

    # a rebuild with fewer sources leaves nothing of the earlier one, and no foreign file goes
    (feed / "mine.jpg").write_bytes(b"x")
    (gen / "full.png").unlink()
    assert runner.invoke(app, build).exit_code == 0
    assert len(list(feed.iterdir())) == 4 + 2 + 2 * 2 + 1 and (feed / "mine.jpg").exists()


def test_cli_errors(repo):  # noqa: F811
    runner = CliRunner()
    base = ["build-replay", "--camera", "cam-ground"]
    result = runner.invoke(app, base)  # a labelled photo, but no reference to check it against
    assert result.exit_code == 1 and "can't read the reference image" in result.output
    cv2.imwrite(str(repo / "ref.png"), base_image())
    (repo / "data/labels/cam-ground.json").write_text(
        json.dumps({"version": 1, "camera_id": "cam-ground", "images": {}})
    )
    result = runner.invoke(app, [*base, "--reference", "ref.png"])
    assert result.exit_code == 1 and "nothing to put in the feed" in result.output
    assert runner.invoke(app, [*base, "--hold", "0"]).exit_code == 2
    (repo / "data/labels/cam-ground.json").write_text(
        json.dumps(
            {"version": 1, "camera_id": "cam-ground", "images": {"base.png": {"taken": ["X9"]}}}
        )
    )
    result = runner.invoke(app, [*base, "--reference", "ref.png"])
    assert result.exit_code == 1 and "unknown slot(s) X9" in result.output
