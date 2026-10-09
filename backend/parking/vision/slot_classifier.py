"""Per-slot classifier (docs/design/vision.md §9): a small CNN says free/taken per slot crop.

Each slot polygon is warped to a square crop, all crops go through one ONNX model in a single
batch (onnxruntime, CPU). The model is trained by `backend/scripts/train_slot_classifier.py`;
its input is RGB, ImageNet-normalised, NCHW; class order and crop size come from the
`<model>.json` sidecar (default free, taken at 128 px).

`ensemble` averages the classifier's probability with the appearance score (§2.1) mapped
through its threshold, so either one can tip a slot that the other is unsure about.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from parking.config import AppearanceCfg, Slot
from parking.vision.appearance import _corners, score_slots_appearance
from parking.vision.occupancy import Size, SlotResult, _scaled

log = logging.getLogger(__name__)

CROP_SIZE = 128
CLASSES = ("free", "taken")
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)  # ImageNet, RGB
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def slot_square(frame: np.ndarray, points: Sequence[Sequence[float]], size: int = CROP_SIZE):
    """The slot warped to a `size`×`size` BGR crop (4 corners in polygon order; polygons with
    more points use their minimum-area rectangle)."""
    src = _corners(points)
    dst = np.array([[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]], np.float32)
    return cv2.warpPerspective(frame, cv2.getPerspectiveTransform(src, dst), (size, size))


def preprocess(crops: Sequence[np.ndarray]) -> np.ndarray:
    """BGR uint8 crops -> float32 NCHW batch, RGB, ImageNet-normalised."""
    batch = np.stack([c[..., ::-1] for c in crops]).astype(np.float32) / 255.0
    batch = (batch - MEAN) / STD
    return np.ascontiguousarray(batch.transpose(0, 3, 1, 2))


class Classifier(Protocol):
    crop_size: int

    def predict(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        """P(taken) for each BGR crop."""
        ...


def meta_path(model: Path) -> Path:
    """The sidecar the training script writes next to the model (classes, crop size, metrics)."""
    return model.with_suffix(".json")


def read_meta(model: Path) -> dict:
    path = meta_path(model)
    return json.loads(path.read_text()) if path.is_file() else {}


class OnnxSlotClassifier:
    """The exported model run with onnxruntime (in the `vision` extra)."""

    def __init__(self, model: Path, threads: int = 2):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        meta = read_meta(model)
        self.crop_size = int(meta.get("crop_size", CROP_SIZE))
        classes = tuple(meta.get("classes", CLASSES))
        if "taken" not in classes:
            raise ValueError(f"{model}: no 'taken' class in {classes}")
        self.taken_index = classes.index("taken")

    def predict(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.zeros(0, dtype=np.float32)
        (logits,) = self.session.run(None, {self.input: preprocess(crops)})
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return (e / e.sum(axis=1, keepdims=True))[:, self.taken_index]


def load_classifier(model: Path) -> OnnxSlotClassifier:
    if not model.is_file():
        raise FileNotFoundError(
            f"slot classifier {model} not found; "
            "train one with backend/scripts/train_slot_classifier.py"
        )
    return OnnxSlotClassifier(model)


def classifier_probs(
    frame: np.ndarray,
    slots: Sequence[Slot],
    frame_size: Size,
    image_size: Size | None,
    clf: Classifier,
) -> np.ndarray:
    polys = [_scaled(s.polygon, image_size, frame_size) for s in slots]
    return clf.predict([slot_square(frame, p, clf.crop_size) for p in polys])


def appearance_prob(score: float, threshold: float) -> float:
    """The appearance score mapped piecewise-linearly so its threshold lands on 0.5."""
    if score < threshold:
        return 0.5 * score / threshold
    return min(0.5 + 0.5 * (score - threshold) / (1 - threshold), 1.0)


def score_slots_classifier(
    frame: np.ndarray,
    slots: Sequence[Slot],
    frame_size: Size,
    image_size: Size | None,
    clf: Classifier,
    threshold: float = 0.5,
    *,
    ensemble: bool = False,
    appearance_threshold: float = 0.30,
    appearance: AppearanceCfg | None = None,
    reference: np.ndarray | None = None,
) -> list[SlotResult]:
    """Score = P(taken); with `ensemble`, the mean of that and the mapped appearance score."""
    probs = classifier_probs(frame, slots, frame_size, image_size, clf)
    if ensemble:
        app = score_slots_appearance(
            frame, slots, frame_size, image_size, appearance_threshold, appearance, reference
        )
        probs = [
            (p + appearance_prob(a.score, appearance_threshold)) / 2
            for p, a in zip(probs, app, strict=True)
        ]
    return [
        SlotResult(id=s.id, score=float(p), taken=float(p) >= threshold)
        for s, p in zip(slots, probs, strict=True)
    ]
