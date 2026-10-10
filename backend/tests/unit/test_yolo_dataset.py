"""Detector training set export (detector-training.md): boxes from slots, splits, COCO."""

import json

import cv2
import numpy as np
import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.config import LabelFile, Slot, SlotFile
from parking.vision import yolo_dataset as yd
from parking.vision.validation import held_out

from .test_cli import _write_lot
from .test_pipeline import rect

runner = CliRunner()

SLOTS = SlotFile(
    version=1,
    camera_id="cam-ground",
    image_size=(200, 100),
    slots=[
        Slot(id="G01", zone="ground", polygon=rect(0, 0, 50, 50)),
        Slot(id="G02", zone="ground", polygon=[(60, 10), (100, 20), (90, 50), (55, 40)]),
        Slot(id="G03", zone="ground", polygon=rect(150, 50, 200, 100)),
    ],
)
NAMES = [f"f{i:02d}.jpg" for i in range(30)]


def write_frames(folder, names=NAMES, size=(400, 200)):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        cv2.imwrite(str(folder / name), np.full((size[1], size[0], 3), 90, np.uint8))


def labels(images):
    return LabelFile(version=1, camera_id="cam-ground", images=images)


def test_slot_box_is_the_polygon_bounds_padded_and_clipped():
    poly = [(60, 10), (100, 20), (90, 50), (55, 40)]
    assert yd.slot_box(poly, (200, 100), 0.0) == (55, 10, 100, 50)
    assert yd.slot_box(poly, (200, 100), 0.1) == pytest.approx((50.5, 6, 104.5, 54))
    assert yd.slot_box(rect(150, 50, 200, 100), (200, 100), 0.2) == (140, 40, 200, 100)


def test_yolo_lines_round_trip():
    line = yd.yolo_line((55, 10, 100, 50), (200, 100))
    assert line == "0 0.387500 0.300000 0.225000 0.400000"
    [(cls, box)] = yd.parse_yolo(line + "\n\n", (200, 100))
    assert cls == 0 and box == pytest.approx((55, 10, 100, 50))
    with pytest.raises(ValueError, match="line 2: expected 'class cx cy w h'"):
        yd.parse_yolo(line + "\n0 0.5 0.5\n", (200, 100))


def test_export_writes_boxes_in_frame_pixels_and_splits_by_name(tmp_path):
    write_frames(tmp_path / "in")
    lab = labels({n: {"taken": ["G01", "G03"]} for n in NAMES} | {"gone.jpg": {"taken": []}})
    out = tmp_path / "ds"
    stats = yd.export_frames(tmp_path / "in", lab, SLOTS, out, holdout=0.3)

    val = sorted(n for n in NAMES if held_out(n, 0.3))
    assert 0 < len(val) < len(NAMES)
    assert sorted(p.name for p in (out / "images" / "val").iterdir()) == val
    assert (stats.train, stats.val) == (len(NAMES) - len(val), len(val))
    assert stats.boxes == 2 * len(NAMES) and stats.missing == ("gone.jpg",)

    stem = val[0].removesuffix(".jpg")
    rows = yd.parse_yolo((out / "labels" / "val" / f"{stem}.txt").read_text(), (400, 200))
    # the frame is 2× the slot file's image_size
    assert [cls for cls, _ in rows] == [0, 0]
    assert rows[0][1] == pytest.approx((0, 0, 100, 100))
    assert rows[1][1] == pytest.approx((300, 100, 400, 200))

    held = json.loads((out / "holdout.json").read_text())
    assert held["camera_id"] == "cam-ground" and sorted(held["images"]) == val
    LabelFile.model_validate(held)  # what `parking evaluate --labels` reads
    assert f"path: {out.resolve()}" in (out / "data.yaml").read_text()
    assert "names:\n  0: car" in (out / "data.yaml").read_text()

    coco = json.loads((out / "annotations" / "val.json").read_text())
    assert coco["categories"] == [{"id": 1, "name": "car"}]
    assert [i["file_name"] for i in coco["images"]] == val
    assert coco["images"][0]["width"] == 400 and coco["images"][0]["height"] == 200
    assert coco["annotations"][1]["bbox"] == [300.0, 100.0, 100.0, 100.0]
    assert coco["annotations"][1]["category_id"] == 1 and coco["annotations"][1]["image_id"] == 1


def test_free_frames_get_an_empty_label_file_and_unsure_slots_are_painted_out(tmp_path):
    write_frames(tmp_path / "in", ["a.jpg", "b.jpg"])
    lab = labels({"a.jpg": {"taken": []}, "b.jpg": {"taken": ["G01"], "unsure": ["G03"]}})
    out = tmp_path / "ds"
    stats = yd.export_frames(tmp_path / "in", lab, SLOTS, out, holdout=0.0, review=True)
    assert (stats.train, stats.val, stats.boxes, stats.masked) == (2, 0, 1, 1)
    assert (out / "labels" / "train" / "a.txt").read_text() == ""
    assert len((out / "labels" / "train" / "b.txt").read_text().splitlines()) == 1

    b = cv2.imread(str(out / "images" / "train" / "b.jpg"))
    assert abs(int(b[150, 350, 0]) - 114) <= 2  # G03 (unsure) is grey
    assert abs(int(b[50, 50, 0]) - 90) <= 2  # G01 (taken) is untouched
    a = cv2.imread(str(out / "images" / "train" / "a.jpg"))
    assert abs(int(a[150, 350, 0]) - 90) <= 2

    review = cv2.imread(str(out / "review" / "b.jpg"))
    assert review[0, 50].tolist()[2] > 200 and review[0, 50].tolist()[0] < 60  # red outline


def test_coco_is_rebuilt_from_corrected_label_files(tmp_path):
    write_frames(tmp_path / "in", ["a.jpg"])
    out = tmp_path / "ds"
    yd.export_frames(tmp_path / "in", labels({"a.jpg": {"taken": ["G01"]}}), SLOTS, out, 0.0)
    label = out / "labels" / "train" / "a.txt"
    label.write_text("0 0.5 0.5 0.25 0.5\n0 0.1 0.1 0.1 0.1\n")  # corrected by hand
    assert yd.write_coco(out) == {"train": 2, "val": 0}
    coco = json.loads((out / "annotations" / "train.json").read_text())
    assert coco["annotations"][0]["bbox"] == [150.0, 50.0, 100.0, 100.0]
    label.write_text("3 0.5 0.5 0.25 0.5\n")
    with pytest.raises(ValueError, match="a.txt: class 3 is not one of"):
        yd.write_coco(out)


def _dataset_repo(tmp_path, monkeypatch, taken=("G01",)):
    _write_lot(tmp_path, monkeypatch)
    folder = tmp_path / "data" / "validation" / "cam-ground"
    write_frames(folder, ["a.jpg", "b.jpg"], size=(200, 100))
    (tmp_path / "data" / "labels").mkdir(parents=True)
    lab = {
        "version": 1,
        "camera_id": "cam-ground",
        "images": {"a.jpg": {"taken": list(taken)}, "b.jpg": {"taken": []}},
    }
    (tmp_path / "data" / "labels" / "cam-ground-validation.json").write_text(json.dumps(lab))


def test_cli_exports_then_refuses_to_overwrite(tmp_path, monkeypatch):
    _dataset_repo(tmp_path, monkeypatch)
    args = ["export-yolo", "--camera", "cam-ground", "--holdout", "0", "--review"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "2 train + 0 val frame(s), 1 box(es), 0 unsure space(s) painted out" in result.output
    out = tmp_path / "data" / "yolo" / "cam-ground"
    assert (out / "labels" / "train" / "a.txt").read_text().startswith("0 0.25")
    assert (out / "review" / "a.jpg").is_file()

    result = runner.invoke(app, args)
    assert result.exit_code == 1 and "pass --force to replace it" in result.output
    (out / "labels" / "train" / "stale.txt").write_text("")
    assert runner.invoke(app, [*args, "--force"]).exit_code == 0
    assert not (out / "labels" / "train" / "stale.txt").exists()

    (out / "labels" / "train" / "b.txt").write_text("0 0.5 0.5 0.2 0.2\n")
    result = runner.invoke(app, ["export-yolo", "--camera", "cam-ground", "--coco"])
    assert result.exit_code == 0, result.output
    assert "train 2 box(es), val 0 box(es)" in result.output


@pytest.mark.parametrize(
    ("extra", "taken", "message"),
    [
        (["--images", "nowhere"], ["G01"], "no image folder"),
        (["--labels", "nowhere.json"], ["G01"], "nowhere.json: file not found"),
        ([], ["G09"], "slot(s) not in the slot file: G09"),
        (["--out", "empty", "--coco"], ["G01"], "run export-yolo without --coco first"),
        (["--images", "config"], ["G01"], "none of the 2 labelled image(s) are in"),
    ],
)
def test_cli_errors(tmp_path, monkeypatch, extra, taken, message):
    _dataset_repo(tmp_path, monkeypatch, taken)
    result = runner.invoke(app, ["export-yolo", "--camera", "cam-ground", *extra])
    assert result.exit_code == 1 and message in result.output


def test_cli_rejects_bad_options(tmp_path, monkeypatch):
    _dataset_repo(tmp_path, monkeypatch)
    for extra in (["--holdout", "1"], ["--pad", "0.6"]):
        assert runner.invoke(app, ["export-yolo", "--camera", "cam-ground", *extra]).exit_code == 2
