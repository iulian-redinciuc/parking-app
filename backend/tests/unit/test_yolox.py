"""YOLOX detector (vision.md §1, detector-training.md) with a stand-in session; no model file."""

import json
from types import SimpleNamespace

import numpy as np
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.config import DetectorCfg
from parking.vision import yolox
from parking.vision.detector import build_detector
from parking.workers.base import WorkerError
from parking.workers.occupancy_worker import _make_detector

from .test_cli import _write_lot
from .test_pipeline import camera

SIZE = 64  # model input: grids of 8×8, 4×4 and 2×2 cells = 84 rows
ROWS = 84


class Session:
    """Answers with fixed raw rows; remembers the blobs it was given."""

    def __init__(self, rows, shape=(1, 3, SIZE, SIZE), classes=80):
        self.shape = list(shape)
        self.out = np.zeros((1, ROWS, 5 + classes), np.float32)
        for index, row in rows.items():
            self.out[0, index, : len(row)] = row
        self.blobs = []

    def get_inputs(self):
        return [SimpleNamespace(name="images", shape=self.shape)]

    def run(self, _outputs, feed):
        self.blobs.append(feed["images"])
        return [self.out]


def cell(obj, cls_id, score=1.0, dx=0.5, dy=0.5, w=0.0, h=0.0, classes=80):
    row = [dx, dy, w, h, obj] + [0.0] * classes
    row[5 + cls_id] = score
    return row


def test_letterbox_keeps_the_aspect_ratio_in_the_top_left():
    frame = np.full((100, 200, 3), 200, np.uint8)
    blob, ratio = yolox.letterbox(frame, (64, 64))
    assert blob.shape == (1, 3, 64, 64) and blob.dtype == np.float32
    assert ratio == pytest.approx(0.32)
    assert blob[0, :, 31, 63].tolist() == [200, 200, 200]  # the frame, not normalised
    assert blob[0, :, 32, 0].tolist() == [114, 114, 114]  # grey below it


def test_decode_places_cells_on_each_grid():
    pred = np.zeros((ROWS, 6), np.float32)
    pred[9] = [0.5, 0.5, 0, np.log(2), 1, 1]  # stride 8, cell (1, 1)
    pred[64 + 5] = [0.25, 0.75, 0, 0, 1, 1]  # stride 16, cell (1, 1)
    pred[83] = [0, 0, 0, 0, 1, 1]  # stride 32, cell (1, 1)
    out = yolox.decode(pred, (SIZE, SIZE))
    assert out[9, :4].tolist() == pytest.approx([12, 12, 8, 16])
    assert out[69, :4].tolist() == pytest.approx([20, 28, 16, 16])
    assert out[83, :4].tolist() == pytest.approx([32, 32, 32, 32])
    with pytest.raises(ValueError, match="85 cells, expected 84"):
        yolox.decode(np.zeros((85, 6), np.float32), (SIZE, SIZE))


def test_detect_scales_boxes_back_to_the_frame_and_filters_classes():
    session = Session(
        {
            64 + 5: cell(0.9, 2),  # a car: 16×16 at (24, 24) in model pixels
            64 + 6: cell(0.9, 0),  # a person: not asked for
            83: cell(0.5, 7, score=0.5),  # a truck at 0.25: below conf
        }
    )
    det = yolox.YoloxDetector("m.onnx", SIZE, 0.3, ["car", "truck"], session=session)
    frame = np.zeros((128, 128, 3), np.uint8)  # 2× the model input
    found = det.detect(frame)
    assert [(d.cls, round(d.conf, 2)) for d in found] == [("car", 0.9)]
    assert found[0].box == pytest.approx((32, 32, 64, 64))
    assert found[0].mask is None
    assert session.blobs[0].shape == (1, 3, SIZE, SIZE)


def test_nms_is_per_class_and_boxes_are_clipped():
    rows = {
        64 + 5: cell(0.9, 2),  # a car, 16×16 around (24, 24)
        64 + 6: cell(0.8, 2, dx=-0.4),  # nearly the same car again, from the next cell
        64 + 9: cell(0.7, 7, dy=-0.5),  # the same box from the cell below, as a truck
        0: cell(0.6, 2, w=np.log(4)),  # 32 px wide around (4, 4): sticks out of the frame
    }
    det = yolox.YoloxDetector("m.onnx", SIZE, 0.3, ["car", "truck"], session=Session(rows))
    found = det.detect(np.zeros((SIZE, SIZE, 3), np.uint8))
    by_cls = sorted((d.cls, round(d.conf, 2)) for d in found)
    assert by_cls == [("car", 0.6), ("car", 0.9), ("truck", 0.7)]
    edge = next(d for d in found if d.conf == pytest.approx(0.6))
    assert edge.box == pytest.approx((0, 0, 20, 8))


def test_sidecar_names_the_classes_of_a_fine_tuned_model(tmp_path):
    model = tmp_path / "parking.onnx"
    session = Session({64 + 5: cell(0.9, 0, classes=1)}, classes=1)
    with pytest.raises(ValueError, match="no class"):  # COCO names, but a one-class output
        yolox.YoloxDetector(str(model), SIZE, 0.3, ["vehicle"], session=session)
    det = yolox.YoloxDetector(str(model), SIZE, 0.3, ["car"], session=session)
    with pytest.raises(ValueError, match=r"1 class\(es\) but 80 name\(s\).*parking.json"):
        det.detect(np.zeros((SIZE, SIZE, 3), np.uint8))

    yolox.meta_path(model).write_text(json.dumps({"classes": ["car"]}))
    det = yolox.YoloxDetector(str(model), SIZE, 0.3, ["car"], session=session)
    assert [d.cls for d in det.detect(np.zeros((SIZE, SIZE, 3), np.uint8))] == ["car"]


def test_sidecar_decoded_output_is_used_as_is(tmp_path):
    model = tmp_path / "decoded.onnx"
    yolox.meta_path(model).write_text(json.dumps({"classes": ["car"], "decoded": True}))
    session = Session({0: [32, 32, 20, 10, 0.9, 1.0]}, classes=1)
    det = yolox.YoloxDetector(str(model), SIZE, 0.3, ["car"], session=session)
    found = det.detect(np.zeros((SIZE, SIZE, 3), np.uint8))
    assert found[0].box == pytest.approx((22, 27, 42, 37))


def test_input_size_must_match_a_fixed_model():
    with pytest.raises(ValueError, match="takes 64x64 input; set detector.imgsz to 64"):
        yolox.YoloxDetector("m.onnx", 416, 0.3, ["car"], session=Session({}))
    dynamic = Session({}, shape=(1, 3, "height", "width"))
    assert yolox.YoloxDetector("m.onnx", SIZE, 0.3, ["car"], session=dynamic).size == (64, 64)
    with pytest.raises(ValueError, match="multiple of 32"):
        yolox.YoloxDetector("m.onnx", 100, 0.3, ["car"], session=dynamic)


def test_config_rules_for_yolox():
    cfg = DetectorCfg(type="yolox", runtime="onnx", model="models/yolox_nano.onnx", imgsz=416)
    assert cfg.type == "yolox" and DetectorCfg(model="m").type == "yolo"
    with pytest.raises(ValueError, match="set runtime: onnx"):
        DetectorCfg(type="yolox", model="m.onnx")
    with pytest.raises(ValueError, match="no masks"):
        DetectorCfg(type="yolox", runtime="onnx", model="m.onnx", use_masks=True)


def test_build_detector_picks_the_class(monkeypatch):
    made = []
    monkeypatch.setattr(yolox, "_session", lambda path, threads: made.append(path) or Session({}))
    cfg = DetectorCfg(type="yolox", runtime="onnx", model="m.onnx", imgsz=SIZE, classes=["car"])
    det = build_detector(cfg, "/models/m.onnx")
    assert isinstance(det, yolox.YoloxDetector) and made == ["/models/m.onnx"]


def test_worker_reports_a_model_that_does_not_fit(tmp_path, monkeypatch):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "m.onnx").write_bytes(b"")
    monkeypatch.setattr(yolox, "_session", lambda path, threads: Session({}))
    det = {"type": "yolox", "runtime": "onnx", "model": "models/m.onnx", "imgsz": 416}
    cam = camera().model_copy(update={"detector": DetectorCfg(**det)})
    with pytest.raises(WorkerError, match="set detector.imgsz to 64"):
        _make_detector(cam, tmp_path, fake=False)
    cam = camera().model_copy(update={"detector": DetectorCfg(**{**det, "imgsz": SIZE})})
    assert isinstance(_make_detector(cam, tmp_path, fake=False), yolox.YoloxDetector)


def test_analyze_runs_through_yolox(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    lot = tmp_path / "config" / "lot.yaml"
    lot.write_text(
        lot.read_text().replace(
            "detector: {model: models/none, conf: 0.5}",
            "detector: {type: yolox, runtime: onnx, model: models/m.onnx, imgsz: 64, conf: 0.5}\n"
            "    occupancy: {mode: box_bottom}",
        )
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "m.onnx").write_bytes(b"")
    # the 200×100 image is scaled by 0.32: the first stride-32 cell is G01 (0–100 px)
    session = Session({80: cell(0.9, 2)})
    monkeypatch.setattr(yolox, "_session", lambda path, threads: session)
    result = CliRunner().invoke(app, ["analyze", "--image", "img.jpg", "--camera", "cam-ground"])
    assert result.exit_code == 0, result.output
    assert "1 free / 2 (1 taken)" in result.output and len(session.blobs) == 1
