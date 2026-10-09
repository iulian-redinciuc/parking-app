"""Per-slot classifier scoring (vision.md §9) with a stand-in model; no onnxruntime needed."""

import numpy as np
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.config import OccupancyCfg, Slot, SlotFile
from parking.vision import slot_classifier as sc
from parking.vision.pipeline import analyze_frame
from parking.workers.base import WorkerError
from parking.workers.occupancy_worker import _make_classifier

from .test_cli import _write_labels, _write_lot
from .test_pipeline import camera, rect


class BrightIsTaken:
    """P(taken) = the crop's mean brightness / 255; records the crops it saw."""

    crop_size = 32

    def __init__(self):
        self.seen = []

    def predict(self, crops):
        self.seen.append([c.shape for c in crops])
        return np.array([c.mean() / 255 for c in crops], np.float32)


SLOTS = SlotFile(
    version=1,
    camera_id="cam-ground",
    image_size=(200, 100),
    slots=[
        Slot(id="G01", zone="ground", polygon=rect(0, 0, 50, 50)),
        Slot(id="G02", zone="ground", polygon=rect(50, 0, 100, 50)),
    ],
)


def frame():
    f = np.zeros((200, 400, 3), np.uint8)  # 2× the slot file
    f[0:100, 0:100] = 230  # G01 bright
    return f


def test_slot_square_warps_corners_in_order():
    img = np.zeros((100, 100, 3), np.uint8)
    img[10:30, 10:50] = (255, 0, 0)  # top half of the slot
    img[30:50, 10:50] = (0, 0, 255)  # bottom half
    crop = sc.slot_square(img, rect(10, 10, 50, 50), 16)
    assert crop.shape == (16, 16, 3)
    assert crop[2, 8].tolist() == [255, 0, 0] and crop[13, 8].tolist() == [0, 0, 255]
    # more than 4 points: the minimum-area rectangle
    assert sc.slot_square(img, [(10, 10), (50, 10), (50, 50), (30, 55), (10, 50)], 16).shape == (
        16,
        16,
        3,
    )


def test_preprocess_is_rgb_nchw_imagenet():
    crop = np.zeros((4, 4, 3), np.uint8)
    crop[..., 2] = 255  # red in BGR
    batch = sc.preprocess([crop, crop])
    assert batch.shape == (2, 3, 4, 4) and batch.dtype == np.float32
    assert batch[0, 0, 0, 0] == pytest.approx((1 - 0.485) / 0.229)
    assert batch[0, 2, 0, 0] == pytest.approx(-0.406 / 0.225)


@pytest.mark.parametrize(
    ("score", "prob"), [(0, 0), (0.15, 0.25), (0.3, 0.5), (0.65, 0.75), (1, 1), (2, 1)]
)
def test_appearance_prob_puts_threshold_at_half(score, prob):
    assert sc.appearance_prob(score, 0.3) == pytest.approx(prob)


def test_score_slots_classifier_and_ensemble(monkeypatch):
    clf = BrightIsTaken()
    res = sc.score_slots_classifier(frame(), SLOTS.slots, (400, 200), (200, 100), clf)
    assert [(r.id, r.taken) for r in res] == [("G01", True), ("G02", False)]
    p_g01 = res[0].score
    assert 0.8 < p_g01 <= 230 / 255  # the warp's last row/column is the polygon edge
    assert clf.seen == [[(32, 32, 3), (32, 32, 3)]]

    # ensemble: mean of P(taken) and the mapped appearance score
    fake_app = [sc.SlotResult("G01", 0.0, False), sc.SlotResult("G02", 0.3, True)]
    monkeypatch.setattr(sc, "score_slots_appearance", lambda *a, **k: fake_app)
    res = sc.score_slots_classifier(
        frame(), SLOTS.slots, (400, 200), (200, 100), clf, ensemble=True, appearance_threshold=0.3
    )
    assert res[0].score == pytest.approx(p_g01 / 2)  # < 0.5: the scorer's "free" wins
    assert res[1].score == pytest.approx(0.25)
    assert [r.taken for r in res] == [False, False]


def test_occupancy_cfg_methods():
    occ = OccupancyCfg(method="ensemble", threshold=0.3)
    assert occ.uses_classifier and occ.uses_appearance and not occ.uses_detector
    assert occ.decision_threshold == 0.5
    assert OccupancyCfg(method="appearance", threshold=0.3).decision_threshold == 0.3


def test_analyze_frame_with_classifier():
    cam = camera(method="classifier")
    with pytest.raises(ValueError, match="slot classifier"):
        analyze_frame(frame(), cam, SLOTS, None)
    r = analyze_frame(frame(), cam, SLOTS, None, classifier=BrightIsTaken())
    assert [s.taken for s in r.slots] == [True, False]
    assert r.detections == [] and r.timings["detect_ms"] == 0
    assert r.totals["ground"].free == 1


def test_worker_needs_the_model(tmp_path):
    cam = camera(method="classifier", classifier={"model": "models/none.onnx"})
    with pytest.raises(WorkerError, match="none.onnx not found"):
        _make_classifier(cam, tmp_path)
    assert _make_classifier(camera(method="appearance"), tmp_path) is None


runner = CliRunner()


def test_cli_classifier_methods(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    _write_labels(tmp_path, [])
    args = ["evaluate", "--images", "img.jpg", "--camera", "cam-ground", "--method", "classifier"]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "slot_classifier.onnx not found" in result.output

    loaded = []
    monkeypatch.setattr(sc, "load_classifier", lambda p: loaded.append(p) or BrightIsTaken())
    result = runner.invoke(app, [*args, "--classifier", "m.onnx", "--sweep", "0.1:0.5:0.2"])
    assert result.exit_code == 0, result.output
    assert loaded == [(tmp_path / "m.onnx").resolve()]
    assert "slot classifier m.onnx (no detector)" in result.output
    assert "== mode classifier, threshold 0.50 ==" in result.output
    assert "best threshold (classifier)" in result.output

    args = ["analyze", "--image", "img.jpg", "--camera", "cam-ground", "--method", "ensemble"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("ground: ")
