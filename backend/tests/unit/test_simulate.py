"""The simulated feed (vision.md §10) on a synthetic base image: no real photo in the repo."""

import json

import cv2
import numpy as np
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.config import Slot, SlotFile, load_labels
from parking.vision import simulate as sim
from parking.vision.appearance import score_slots_appearance

W, H = 1040, 420
SLOT_W, SLOT_H, X0, Y0 = 120, 220, 40, 30
N = 8  # full slots in the top row (G..); the bottom row (B..) is cut off by the image edge
CUT_Y0, CUT_N = 300, 4
PAVEMENT = (115, 135, 155)  # BGR, brownish grey
BASE_TAKEN = ["B02", "G02", "G05"]


def rect(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


SLOTS = [
    Slot(
        id=f"G{i + 1:02}",
        zone="ground",
        polygon=rect(X0 + i * SLOT_W, Y0, X0 + (i + 1) * SLOT_W, Y0 + SLOT_H),
    )
    for i in range(N)
] + [
    Slot(
        id=f"B{i + 1:02}",
        zone="ground",
        polygon=rect(X0 + i * SLOT_W, CUT_Y0, X0 + (i + 1) * SLOT_W, H - 1),
    )
    for i in range(CUT_N)
]
SLOT_FILE = SlotFile(version=1, camera_id="cam-ground", image_size=(W, H), slots=SLOTS)
IDS = [s.id for s in SLOTS]


def car(img, x, y1, y2, colour):
    cv2.rectangle(img, (x + 15, y1), (x + SLOT_W - 15, y2), colour, -1)
    cv2.rectangle(
        img, (x + 25, y1 + 30), (x + SLOT_W - 25, y1 + 60), (60, 60, 60), -1
    )  # windscreen


def base_image() -> np.ndarray:
    """Textured pavers, painted lines, two cars in the top row and one cut-off car below."""
    rng = np.random.default_rng(0)
    img = np.empty((H, W, 3), np.float32)
    img[:] = PAVEMENT
    img += rng.normal(0, 6, (H, W, 1))
    img[::12, :] *= 0.8
    img[:, ::24] *= 0.8
    img = np.clip(img, 0, 255).astype(np.uint8)
    for i in range(N + 1):
        x = X0 + i * SLOT_W
        cv2.line(img, (x, Y0), (x, Y0 + SLOT_H), (235, 235, 235), 5)
        if i <= CUT_N:
            cv2.line(img, (x, CUT_Y0), (x, H), (235, 235, 235), 5)
    car(img, X0 + 1 * SLOT_W, Y0 + 20, Y0 + SLOT_H - 20, (30, 30, 35))  # G02
    car(img, X0 + 4 * SLOT_W, Y0 + 20, Y0 + SLOT_H - 20, (190, 110, 40))  # G05
    car(img, X0 + 1 * SLOT_W, CUT_Y0 + 15, H + 80, (40, 40, 180))  # B02, runs out of the image
    return img


@pytest.fixture(scope="module")
def simulator() -> sim.Simulator:
    return sim.Simulator(base_image(), SLOT_FILE, BASE_TAKEN, seed=1)


def read_taken(frame, reference) -> set[str]:
    results = score_slots_appearance(frame, SLOTS, (W, H), reference=reference)
    return {r.id for r in results if r.taken}


def slot_mask(slot: Slot, shrink: int = 4) -> np.ndarray:
    mask = np.zeros((H, W), np.uint8)
    cv2.fillPoly(mask, [np.array(slot.polygon, np.int32)], 1)
    return cv2.erode(mask, np.ones((2 * shrink + 1, 2 * shrink + 1), np.uint8)).astype(bool)


# --- rendering ---


def test_base_state_renders_the_base_image(simulator):
    assert np.array_equal(simulator.render(simulator.base_state()), simulator.base)
    assert np.array_equal(simulator.render(BASE_TAKEN), simulator.base)
    assert sorted(simulator.base_state()) == BASE_TAKEN


def test_every_frame_reads_as_its_labels(simulator):
    """Emptied slots look like pavement and filled ones like a car to the appearance scorer."""
    reference = simulator.render({})
    assert read_taken(reference, reference) == set()
    assert read_taken(simulator.render(IDS), reference) == set(IDS)
    states = sim.sequence(simulator, 40, seed=3, day=True)
    for i, state in enumerate(states):
        frame = sim.vary(simulator.render(state), i, seed=3, shift=2)
        assert read_taken(frame, reference) == set(state), f"frame {i}"


def test_only_the_asked_slots_change(simulator):
    for slot, wanted in (
        ("G07", BASE_TAKEN + ["G07"]),
        ("G02", ["B02", "G05"]),
        ("B03", BASE_TAKEN + ["B03"]),
    ):
        img = simulator.render(wanted)
        changed = np.any(img != simulator.base, axis=2)
        assert changed[slot_mask(next(s for s in SLOTS if s.id == slot))].mean() > 0.3
        for other in SLOTS:
            if other.id != slot:
                assert not changed[slot_mask(other)].any(), f"{slot} changed {other.id}"


def test_a_leaving_base_car_is_replaced_by_pavement(simulator):
    img = simulator.render(["G05"])  # G02 and B02 leave
    for slot in (s for s in SLOTS if s.id in ("G02", "B02")):
        inside = img[slot_mask(slot, 12)].astype(float)
        assert np.abs(inside.mean(axis=0) - PAVEMENT).max() < 20


def test_a_car_keeps_its_look_and_looks_differ(simulator):
    rng = np.random.default_rng(5)
    a = sim.Look(donor="G02")
    b = sim.Look(donor="G05", flip_y=True, gain=1.1, hue=6)
    assert np.array_equal(simulator.render({"G07": a}), simulator.render({"G07": a}))
    assert not np.array_equal(simulator.render({"G07": a}), simulator.render({"G07": b}))
    assert simulator.new_look("G07", rng).donor in ("G02", "G05")


def test_donors_have_a_similar_shape(simulator):
    """A slot cut off by the image edge only gets cut-off cars, never mirrored across the cut."""
    rng = np.random.default_rng(0)
    assert simulator.patches["B01"].cut == {"bottom"}
    assert simulator.patches["G01"].cut == set()
    for _ in range(30):
        cut = simulator.new_look("B04", rng)
        assert cut.donor == "B02" and not cut.flip_y
        assert simulator.new_look("G08", rng).donor in ("G02", "G05")
    assert {simulator.new_look("G08", rng).flip_y for _ in range(30)} == {True, False}
    assert {simulator.new_look("B04", rng).flip_x for _ in range(30)} == {True, False}


def test_polygons_follow_the_image_size():
    small = cv2.resize(base_image(), (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    s = sim.Simulator(small, SLOT_FILE, BASE_TAKEN)
    reference = s.render({})
    results = score_slots_appearance(
        s.render(["G01", "G08", "B04"]), SLOTS, (W // 2, H // 2), (W, H), reference=reference
    )
    assert {r.id for r in results if r.taken} == {"G01", "G08", "B04"}


def test_unsure_slots_are_left_alone():
    s = sim.Simulator(base_image(), SLOT_FILE, ["G02", "G05"], unsure=["B02"])
    assert "B02" not in s.movable
    states = sim.sequence(s, 30, seed=2)
    assert all("B02" not in state for state in states)
    with pytest.raises(ValueError, match="can't be changed"):
        s.render(["B02"])
    unsure = slot_mask(next(x for x in SLOTS if x.id == "B02"))
    assert np.array_equal(s.render({})[unsure], s.base[unsure])


def test_bad_input():
    with pytest.raises(ValueError, match="not in the slot file"):
        sim.Simulator(base_image(), SLOT_FILE, ["X01"])
    with pytest.raises(ValueError, match="at least one taken and one free"):
        sim.Simulator(base_image(), SLOT_FILE, [])
    with pytest.raises(ValueError, match="at least one taken and one free"):
        sim.Simulator(base_image(), SLOT_FILE, IDS)


# --- sequence ---


def test_same_seed_same_frames(simulator):
    a, b = sim.sequence(simulator, 30, seed=1), sim.sequence(simulator, 30, seed=1)
    assert a == b
    assert a != sim.sequence(simulator, 30, seed=2)
    assert a[0] == simulator.base_state()
    for i in (0, 7, 29):
        fa = sim.vary(simulator.render(a[i]), i, seed=1, shift=2)
        fb = sim.vary(simulator.render(b[i]), i, seed=1, shift=2)
        assert np.array_equal(fa, fb)


def test_walk_changes_at_most_two_slots_and_lets_cars_stay():
    rng = np.random.default_rng(4)
    events = list(sim.walk(IDS, 300, rng, BASE_TAKEN, min_dwell=4))
    assert events[0] == []
    assert max(len(e) for e in events) == 2
    taken, last = set(BASE_TAKEN), {}
    counts = []
    for i, frame in enumerate(events):
        for slot, arrived in frame:
            assert (slot in taken) != arrived
            assert i - last.get(slot, -4) >= 4
            last[slot] = i
            (taken.add if arrived else taken.discard)(slot)
        counts.append(len(taken))
    assert 60 < sum(1 for e in events if e) < 240
    assert min(counts) < len(IDS) / 2 < max(counts)


def test_day_curve_has_a_nearly_empty_and_a_nearly_full_stretch(simulator):
    counts = [len(s) for s in sim.sequence(simulator, 200, seed=1, day=True)]
    n = len(IDS)
    assert max(counts[:15]) <= 0.25 * n and max(counts[-10:]) <= 0.25 * n
    assert sum(c >= 0.85 * n for c in counts[80:120]) >= 20
    assert sum(c <= 0.15 * n for c in counts) >= 10
    assert sim.day_curve(0) == sim.day_curve(1) == sim.DAY_LOW
    assert sim.day_curve(0.5) == sim.DAY_HIGH


# --- frame-wide variation ---


def test_vary():
    img = base_image()
    assert np.array_equal(sim.vary(img, 5, seed=1, drift=0, noise=0, shift=0), img)
    a = sim.vary(img, 5, seed=1, shift=3)
    assert a.shape == img.shape and a.dtype == np.uint8
    assert np.array_equal(a, sim.vary(img, 5, seed=1, shift=3))
    assert not np.array_equal(a, sim.vary(img, 6, seed=1, shift=3))
    means = [sim.vary(img, i, seed=1, noise=0).mean() / img.mean() for i in range(0, 240, 8)]
    assert 0.9 < min(means) < 0.96 and 1.04 < max(means) < 1.1  # slow drift, both ways
    assert max(abs(a - b) for a, b in zip(means, means[1:], strict=False)) < 0.03
    noisy = sim.vary(img, 0, seed=1, drift=0, noise=2.0).astype(float) - img
    assert 1.5 < noisy.std() < 2.5


def test_frame_names():
    assert sim.frame_name(0) == "frame-0001.jpg"
    assert sim.frame_name(199) == "frame-0200.jpg"


# --- CLI ---


LOT_YAML = """
version: 1
lot: {id: main, name: P, location: {lat: 0, lon: 0}, timezone: UTC}
zones:
  - {id: ground, name: {en: Ground}, method: slots}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: "folder:data/replay/ground-sim?interval=5&loop=true"
    slots_file: config/slots/cam-ground.json
    detector: {model: models/none}
    occupancy:
      method: appearance
      appearance: {reference_empty: data/reference/cam-ground-empty.jpg}
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    (tmp_path / "config" / "slots" / "cam-ground.json").write_text(SLOT_FILE.model_dump_json())
    (tmp_path / "data" / "samples").mkdir(parents=True)
    (tmp_path / "data" / "labels").mkdir()
    cv2.imwrite(str(tmp_path / "data" / "samples" / "base.png"), base_image())
    labels = {
        "version": 1,
        "camera_id": "cam-ground",
        "images": {"base.png": {"taken": BASE_TAKEN}},
    }
    (tmp_path / "data" / "labels" / "cam-ground.json").write_text(json.dumps(labels))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_writes_frames_labels_and_the_empty_lot(repo):
    runner = CliRunner()
    args = ["simulate-feed", "--camera", "cam-ground", "--base", "data/samples/base.png"]
    args += ["--frames", "12", "--seed", "1", "--quality", "95"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "12 frame(s) from base.png" in result.output

    frames = sorted(p.name for p in (repo / "data/replay/ground-sim").iterdir())
    assert frames == [f"frame-{i:04}.jpg" for i in range(1, 13)]
    labels = load_labels(repo / "data/labels/cam-ground-sim.json")
    assert labels.camera_id == "cam-ground" and sorted(labels.images) == frames
    assert labels.images["frame-0001.jpg"].taken == BASE_TAKEN
    assert all(v.conditions == ["simulated"] for v in labels.images.values())
    assert len({tuple(v.taken) for v in labels.images.values()}) > 3

    # `parking evaluate` agrees with every label, using the empty lot the command wrote
    assert (repo / "data/reference/cam-ground-empty.jpg").is_file()
    ev = ["evaluate", "--camera", "cam-ground", "--images", "data/replay/ground-sim"]
    ev += ["--labels", "data/labels/cam-ground-sim.json", "--out", "out/eval"]
    result = runner.invoke(app, ev)
    assert result.exit_code == 0, result.output
    assert f"slot accuracy 100.0% ({12 * len(IDS)}/{12 * len(IDS)})" in result.output

    # a shorter run leaves none of the earlier frames behind; the same seed gives the same frames
    first = (repo / "data/replay/ground-sim/frame-0003.jpg").read_bytes()
    assert (
        runner.invoke(
            app, [*args[:-4], "--frames", "3", "--seed", "1", "--quality", "95"]
        ).exit_code
        == 0
    )
    assert len(list((repo / "data/replay/ground-sim").iterdir())) == 3
    assert (repo / "data/replay/ground-sim/frame-0003.jpg").read_bytes() == first


def test_cli_errors(repo):
    runner = CliRunner()
    base = ["simulate-feed", "--camera", "cam-ground"]
    result = runner.invoke(app, [*base, "--base", "data/samples/missing.png"])
    assert result.exit_code == 1 and "can't read image" in result.output
    cv2.imwrite(str(repo / "data/samples/other.png"), base_image())
    result = runner.invoke(app, [*base, "--base", "data/samples/other.png"])
    assert result.exit_code == 1 and "isn't labelled" in result.output
    result = runner.invoke(app, [*base, "--base", "data/samples/base.png", "--shift", "9"])
    assert result.exit_code == 2
    result = runner.invoke(app, [*base, "--base", "data/samples/base.png", "--frames", "0"])
    assert result.exit_code == 2
