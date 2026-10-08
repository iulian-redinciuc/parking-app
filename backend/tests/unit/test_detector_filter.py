import json
from pathlib import Path

import numpy as np
import pytest

from parking.vision.detector import (
    COCO_VEHICLES,
    FakeDetector,
    class_ids,
    to_detections,
)

REPO = Path(__file__).resolve().parents[3]
COCO_NAMES = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
    28: "suitcase",
}
VEHICLES = list(COCO_VEHICLES)
FRAME = np.zeros((10, 10, 3), np.uint8)


def write_sidecar(path: Path, detections: list[dict]) -> Path:
    path.write_text(json.dumps({"detections": detections}))
    return path


def test_class_ids_map_vehicle_names_to_coco_ids():
    assert class_ids(COCO_NAMES, VEHICLES) == [2, 3, 5, 7]
    assert {n: class_ids(COCO_NAMES, [n])[0] for n in VEHICLES} == COCO_VEHICLES


def test_class_ids_reject_unknown_names():
    with pytest.raises(ValueError, match="lorry"):
        class_ids(COCO_NAMES, ["car", "lorry"])


def test_to_detections_maps_ids_and_filters_class_and_conf():
    dets = to_detections(
        boxes=[[0, 0, 10, 10], [1, 1, 5, 5], [2, 2, 8, 8], [3, 3, 9, 9]],
        cls_ids=[2.0, 0.0, 7.0, 5.0],  # ultralytics returns float tensors
        confs=[0.9, 0.95, 0.2, 0.5],
        names=COCO_NAMES,
        classes=VEHICLES,
        conf=0.35,
    )
    assert [(d.cls, d.conf) for d in dets] == [("car", 0.9), ("bus", 0.5)]
    assert dets[0].box == (0.0, 0.0, 10.0, 10.0)
    assert all(d.mask is None and d.track_id is None for d in dets)


def test_to_detections_keeps_masks_as_float32_polygons():
    poly = [[0, 0], [10, 0], [10, 10], [0, 10]]
    dets = to_detections(
        [[0, 0, 10, 10], [0, 0, 4, 4]],
        [2, 3],
        [0.8, 0.7],
        COCO_NAMES,
        VEHICLES,
        masks=[np.array(poly), np.zeros((0, 2))],  # an empty mask becomes None
    )
    assert dets[0].mask.dtype == np.float32
    assert dets[0].mask.shape == (4, 2)
    assert dets[1].mask is None


def test_fake_detector_reads_sidecar(tmp_path):
    path = write_sidecar(
        tmp_path / "frame.json",
        [
            {
                "cls": "car",
                "conf": 0.91,
                "box": [10, 20, 110, 80],
                "mask": [[10, 20], [110, 20], [110, 80]],
            },
            {"cls": "truck", "conf": 0.6, "box": [200, 20, 400, 120]},
        ],
    )
    dets = FakeDetector(path).detect(FRAME)
    assert [d.cls for d in dets] == ["car", "truck"]
    assert dets[0].box == (10.0, 20.0, 110.0, 80.0)
    assert dets[0].mask.shape == (3, 2)
    assert dets[1].mask is None


def test_fake_detector_applies_the_class_and_conf_filter(tmp_path):
    path = write_sidecar(
        tmp_path / "frame.json",
        [
            {"cls": "car", "conf": 0.9, "box": [0, 0, 1, 1]},
            {"cls": "person", "conf": 0.99, "box": [0, 0, 1, 1]},
            {"cls": "motorcycle", "conf": 0.3, "box": [0, 0, 1, 1]},
            {"cls": "bus", "conf": 0.7, "box": [0, 0, 1, 1]},
        ],
    )
    assert [d.cls for d in FakeDetector(path, conf=0.35).detect(FRAME)] == ["car", "bus"]
    assert [d.cls for d in FakeDetector(path, classes=["car"]).detect(FRAME)] == ["car"]


def test_fake_detector_returns_a_copy(tmp_path):
    det = FakeDetector(
        write_sidecar(tmp_path / "f.json", [{"cls": "car", "conf": 1, "box": [0, 0, 1, 1]}])
    )
    det.detect(FRAME).clear()
    assert len(det.detect(FRAME)) == 1


def test_export_cli_rejects_unknown_runtime():
    from typer.testing import CliRunner

    from parking.cli import app

    result = CliRunner().invoke(app, ["models", "export", "--runtime", "hailo"])
    assert result.exit_code != 0


@pytest.mark.slow
def test_real_model_finds_a_vehicle():
    """Runs the exported seg model on Ultralytics' bundled street photo (not stored in the repo)."""
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("ultralytics")
    from ultralytics.utils import ASSETS

    from parking.vision.detector import YoloDetector

    model = REPO / "models" / "yolo11n-seg_ncnn_model"
    if not model.exists():
        pytest.skip("run `parking models export --model yolo11n-seg --imgsz 1280` first")
    frame = cv2.imread(str(ASSETS / "bus.jpg"))
    dets = YoloDetector(str(model), imgsz=1280, conf=0.25, classes=VEHICLES, use_masks=True).detect(
        frame
    )
    assert len(dets) >= 1
    assert all(d.cls in VEHICLES for d in dets)
    assert any(d.mask is not None for d in dets)
