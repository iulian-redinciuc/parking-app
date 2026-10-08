import json

import numpy as np
import pytest

from parking.config import ImageLabels, LabelFile, Slot, SlotFile
from parking.vision.annotate import MAGENTA, highlight_slots
from parking.vision.detector import Detection
from parking.vision.evaluate import (
    Frame,
    best_threshold,
    by_condition,
    cache_path,
    check_labels,
    detect_frames,
    evaluate_frames,
    evaluate_image,
    load_detections,
    parse_sweep,
    save_detections,
    summarize,
    sweep,
)
from parking.vision.occupancy import SlotResult


def results(taken: str, free: str = "") -> list[SlotResult]:
    """'G1 G2' taken, the rest free; score 0.9 / 0.0."""
    out = [SlotResult(s, 0.9, True) for s in taken.split()]
    return out + [SlotResult(s, 0.0, False) for s in free.split()]


def label(taken=(), unsure=(), conditions=("day",)):
    return ImageLabels(conditions=list(conditions), taken=list(taken), unsure=list(unsure))


# --- metric maths ---


def test_all_correct():
    e = evaluate_image("a.jpg", results("G1", "G2 G3"), label(["G1"]))
    assert (e.labelled, e.correct, e.true_free, e.pred_free, e.count_error) == (3, 3, 2, 2, 0)
    assert e.said_free == () and e.said_taken == ()


def test_both_kinds_of_mistake():
    # truth: G1, G2 taken; prediction: G1, G3 taken -> G2 said free, G3 said taken
    e = evaluate_image("a.jpg", results("G1 G3", "G2 G4"), label(["G1", "G2"]))
    assert e.correct == 2 and e.labelled == 4
    assert e.said_free == ("G2",) and e.said_taken == ("G3",)
    assert e.true_free == 2 and e.pred_free == 2 and e.count_error == 0  # errors cancel out
    assert e.free_tp == 1
    assert e.notes() == {"G2": "should be taken", "G3": "should be free"}


def test_unsure_slots_are_excluded():
    e = evaluate_image("a.jpg", results("G1", "G2 G3"), label(["G3"], unsure=["G1"]))
    assert e.labelled == 2 and e.correct == 1
    assert e.said_free == ("G3",) and e.said_taken == ()
    assert e.true_free == 1 and e.pred_free == 2


def test_summary_maths():
    evals = [
        # 4 slots, truth taken G1: perfect
        evaluate_image("a.jpg", results("G1", "G2 G3 G4"), label(["G1"])),
        # truth taken G1 G2: predicted all free -> 2 said free, count error 2
        evaluate_image("b.jpg", results("", "G1 G2 G3 G4"), label(["G1", "G2"], conditions=[])),
        # truth all free: G4 predicted taken -> 1 said taken, count error 1
        evaluate_image("c.jpg", results("G4", "G1 G2 G3"), label([], conditions=["night"])),
    ]
    s = summarize(evals)
    assert (s.images, s.labelled, s.correct) == (3, 12, 9)
    assert s.accuracy == pytest.approx(9 / 12)
    # free: TP = 3 + 2 + 3 = 8, FP (said free) = 2, FN (said taken) = 1
    assert (s.free_tp, s.free_fp, s.free_fn) == (8, 2, 1)
    assert s.free_precision == pytest.approx(8 / 10)
    assert s.free_recall == pytest.approx(8 / 9)
    assert s.count_errors == (0, 2, 1)
    assert s.mean_count_error == pytest.approx(1.0)
    assert s.count_within_1 == pytest.approx(2 / 3)
    d = s.to_dict()
    assert d["accuracy"] == pytest.approx(0.75) and d["free_fp"] == 2


def test_summary_without_data_is_none():
    s = summarize([])
    assert s.accuracy is None and s.free_precision is None and s.mean_count_error is None
    # nothing predicted free: precision undefined, recall 0
    e = evaluate_image("a.jpg", results("G1 G2"), label(["G1"]))
    s = summarize([e])
    assert s.free_precision is None and s.free_recall == 0.0


def test_by_condition():
    evals = [
        evaluate_image("a.jpg", results("G1"), label(["G1"], conditions=["day", "dry"])),
        evaluate_image("b.jpg", results("", "G1"), label(["G1"], conditions=["night"])),
    ]
    conds = by_condition(evals)
    assert list(conds) == ["day", "dry", "night"]
    assert conds["day"].accuracy == 1.0 and conds["night"].accuracy == 0.0


def test_check_labels_catches_unknown_slots():
    labels = LabelFile(version=1, camera_id="c", images={"a.jpg": label(["G1", "X9"], ["X8"])})
    check_labels(LabelFile(version=1, camera_id="c", images={"a.jpg": label(["G1"])}), ["G1"])
    with pytest.raises(ValueError, match="a.jpg: X8, X9"):
        check_labels(labels, ["G1"])


def test_label_cannot_be_taken_and_unsure():
    with pytest.raises(ValueError, match="both taken and unsure: G1"):
        label(["G1"], unsure=["G1"])


# --- sweep ---


def test_parse_sweep():
    assert parse_sweep("0.1:0.6:0.05") == pytest.approx(
        [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6]
    )
    assert parse_sweep("0.3:0.3:0.1") == [0.3]
    assert parse_sweep("0.1:0.35:0.1") == pytest.approx([0.1, 0.2, 0.3])  # stop not on a step
    for bad in ("0.1:0.6", "a:b:c", "0.5:0.1:0.1", "0.1:0.5:0", "0:0.5:0.1", "0.5:1:0.1"):
        with pytest.raises(ValueError):
            parse_sweep(bad)


def rect(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


SLOT_FILE = SlotFile(
    version=1,
    camera_id="c",
    image_size=(200, 100),
    slots=[
        Slot(id="G1", zone="ground", polygon=rect(0, 0, 100, 100)),
        Slot(id="G2", zone="ground", polygon=rect(100, 0, 200, 100)),
    ],
)


def car(x1, y1, x2, y2):
    return Detection("car", 0.9, (x1, y1, x2, y2), np.asarray(rect(x1, y1, x2, y2), np.float32))


def test_sweep_and_best_threshold():
    # frame at twice the slot file's size; the car covers 40% of G1, a mirror 10% of G2
    frames = [Frame("a.jpg", (400, 200), [car(0, 0, 80, 200), car(200, 0, 220, 200)])]
    labels = LabelFile(version=1, camera_id="c", images={"a.jpg": label(["G1"])})
    rows = sweep(frames, SLOT_FILE, labels, "mask", [0.05, 0.2, 0.3, 0.5])
    acc = {t: s.accuracy for t, s in rows}
    assert acc == {0.05: 0.5, 0.2: 1.0, 0.3: 1.0, 0.5: 0.5}
    assert best_threshold(rows, prefer=0.3) == 0.3  # tie between 0.2 and 0.3 -> closest
    assert best_threshold(rows, prefer=0.1) == 0.2
    [e] = evaluate_frames(frames, SLOT_FILE, labels, "mask", 0.5)
    assert e.said_free == ("G1",)
    with pytest.raises(ValueError):
        best_threshold([], 0.3)


def test_best_threshold_prefers_free_precision_on_equal_accuracy():
    def s(correct, tp, fp, fn):
        from parking.vision.evaluate import Summary

        return Summary(1, 10, correct, tp, fp, fn, (0,))

    rows = [(0.2, s(8, 5, 2, 0)), (0.4, s(8, 6, 0, 2))]
    assert best_threshold(rows, prefer=0.2) == 0.4


# --- detection cache ---


class CountingDetector:
    def __init__(self):
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return [car(0, 0, 50, 50), Detection("truck", 0.5, (1, 2, 3, 4))]


def test_cache_round_trip(tmp_path):
    frame = Frame("a.jpg", (640, 480), CountingDetector().detect(None))
    path = tmp_path / "a.json"
    key = {"conf": 0.35}
    save_detections(path, frame, key)
    back = load_detections(path, "a.jpg", key)
    assert back.frame_size == (640, 480)
    assert [d.cls for d in back.detections] == ["car", "truck"]
    assert back.detections[0].mask.tolist() == frame.detections[0].mask.tolist()
    assert back.detections[1].mask is None and back.detections[1].box == (1, 2, 3, 4)
    assert load_detections(path, "a.jpg", {"conf": 0.5}) is None  # other settings
    assert load_detections(tmp_path / "missing.json", "a.jpg", key) is None
    path.write_text("{broken")
    assert load_detections(path, "a.jpg", key) is None


def test_detect_frames_uses_the_cache(tmp_path):
    images = []
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"x")
        images.append(tmp_path / name)
    det, made = CountingDetector(), []

    def factory():
        made.append(1)
        return det

    def read(path):
        return np.zeros((480, 640, 3), np.uint8)

    cache = tmp_path / "cache"
    args = (read, factory, cache, "models/yolo11n-seg_ncnn_model", 1280, {"conf": 0.35})
    frames, ran = detect_frames(images, *args)
    assert ran == 2 and det.calls == 2 and len(made) == 1
    assert (cache / "a.jpg.yolo11n-seg_ncnn_model.1280.json").is_file()
    assert cache_path(cache, images[0], "m", 640).name == "a.jpg.m.640.json"

    frames2, ran2 = detect_frames(images, *args)
    assert ran2 == 0 and det.calls == 2 and len(made) == 1  # model not even loaded
    assert [f.frame_size for f in frames2] == [(640, 480)] * 2

    (tmp_path / "b.jpg").write_bytes(b"changed")  # a changed image is detected again
    _, ran3 = detect_frames(images, *args)
    assert ran3 == 1

    _, ran4 = detect_frames(images, read, factory, None, "m", 640, {})  # cache off
    assert ran4 == 2

    with pytest.raises(ValueError, match="can't read image"):
        detect_frames(images, lambda p: None, factory, None, "m", 640, {})


def test_highlight_slots():
    frame = np.full((100, 200, 3), 128, np.uint8)
    out = highlight_slots(frame, SLOT_FILE.slots, {"G2": "should be free"}, (200, 100))
    assert (frame == 128).all()  # input untouched
    magenta = np.all(out == MAGENTA, axis=2)
    assert magenta[:, 100:].any() and not magenta[:, :95].any()


def test_cache_file_is_fake_detector_format(tmp_path):
    path = tmp_path / "c.json"
    save_detections(path, Frame("a.jpg", (10, 10), [car(0, 0, 5, 5)]), {})
    data = json.loads(path.read_text())
    assert set(data["detections"][0]) == {"cls", "conf", "box", "mask"}


def test_load_labels(tmp_path):
    from parking.config import load_labels

    path = tmp_path / "labels.json"
    data = {
        "version": 1,
        "camera_id": "cam-ground",
        "images": {"a.jpg": {"conditions": ["day"], "taken": ["G01"], "unsure": ["G09"]}},
    }
    path.write_text(json.dumps(data))
    labels = load_labels(path)
    assert labels.images["a.jpg"].taken == ["G01"]
    data["images"]["a.jpg"]["tken"] = []  # typo
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="tken"):
        load_labels(path)
