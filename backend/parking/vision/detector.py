"""Vehicle detector behind a small protocol (docs/design/vision.md §1).

`YoloDetector` wraps an exported Ultralytics model; `FakeDetector` returns detections
from a JSON sidecar so tests and CI don't need PyTorch. `build_detector` picks the class
for a camera's `detector.type` (`yolox`: `parking/vision/yolox.py`, no Ultralytics code).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

# COCO ids of the vehicle classes we care about (vision.md §1).
COCO_VEHICLES: dict[str, int] = {"car": 2, "motorcycle": 3, "bus": 5, "truck": 7}


@dataclass(frozen=True)
class Detection:
    cls: str  # "car" | "motorcycle" | "bus" | "truck"
    conf: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in frame pixels
    mask: np.ndarray | None = None  # polygon (N×2 float32) in frame pixels, if use_masks
    track_id: int | None = None  # set by the tracker (flow only)


class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> list[Detection]: ...


def class_ids(names: Mapping[int, str], classes: Iterable[str]) -> list[int]:
    """Map class names to the model's ids. Unknown names are an error, not silently dropped."""
    by_name = {name: i for i, name in names.items()}
    wanted = list(classes)
    unknown = [c for c in wanted if c not in by_name]
    if unknown:
        raise ValueError(f"model has no class(es) {unknown}")
    return sorted(by_name[c] for c in wanted)


def _as_mask(points: Any) -> np.ndarray | None:
    if points is None:
        return None
    arr = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    return arr if len(arr) >= 3 else None  # fewer than 3 points is not a polygon


def to_detections(
    boxes: Sequence[Sequence[float]],
    cls_ids: Sequence[int],
    confs: Sequence[float],
    names: Mapping[int, str],
    classes: Iterable[str],
    conf: float = 0.0,
    masks: Sequence[Any] | None = None,
) -> list[Detection]:
    """Turn raw model outputs into `Detection`s, keeping only `classes` at or above `conf`."""
    keep = set(classes)
    out = []
    for i, (box, cid, c) in enumerate(zip(boxes, cls_ids, confs, strict=True)):
        name = names.get(int(cid))
        if name not in keep or float(c) < conf:
            continue
        x1, y1, x2, y2 = (float(v) for v in box)
        mask = _as_mask(masks[i]) if masks is not None else None
        out.append(Detection(cls=name, conf=float(c), box=(x1, y1, x2, y2), mask=mask))
    return out


def _model_task(model_path: str | Path) -> str:
    return "segment" if "-seg" in Path(model_path).name else "detect"


class YoloDetector:
    """An exported YOLO model (NCNN, OpenVINO, ONNX, ...), loaded once."""

    def __init__(
        self,
        model_path: str,
        imgsz: int,
        conf: float,
        classes: list[str],
        use_masks: bool,
    ):
        from ultralytics import YOLO  # heavy import, only when a real model is used

        task = _model_task(model_path)
        if use_masks and task != "segment":
            raise ValueError(f"use_masks needs a segmentation model (*-seg), got {model_path}")
        self.model = YOLO(str(model_path), task=task)
        self.names: dict[int, str] = dict(self.model.names)
        self.imgsz = imgsz
        self.conf = conf
        self.classes = list(classes)
        self.use_masks = use_masks
        self._class_ids = class_ids(self.names, self.classes)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        result = self.model.predict(
            frame, imgsz=self.imgsz, conf=self.conf, classes=self._class_ids, verbose=False
        )[0]
        boxes = result.boxes
        masks = result.masks.xy if (self.use_masks and result.masks is not None) else None
        return to_detections(
            boxes.xyxy.tolist(),
            boxes.cls.tolist(),
            boxes.conf.tolist(),
            self.names,
            self.classes,
            self.conf,
            masks,
        )


def build_detector(cfg: Any, model_path: str | Path) -> Detector:
    """The detector for a camera's `detector` settings (config.md §1), model at `model_path`."""
    if cfg.type == "yolox":
        from parking.vision.yolox import YoloxDetector

        return YoloxDetector(str(model_path), cfg.imgsz, cfg.conf, cfg.classes)
    return YoloDetector(str(model_path), cfg.imgsz, cfg.conf, cfg.classes, cfg.use_masks)


class FakeDetector:
    """Returns the detections stored in a JSON sidecar (format in vision.md §1), ignoring the frame.

    The same class and confidence filter as `YoloDetector` is applied.
    """

    def __init__(
        self,
        json_path: str | Path,
        conf: float = 0.0,
        classes: Iterable[str] = tuple(COCO_VEHICLES),
    ):
        data = json.loads(Path(json_path).read_text())
        items = data["detections"]
        names = dict(enumerate(sorted({d["cls"] for d in items})))
        ids = {n: i for i, n in names.items()}
        self.detections = to_detections(
            [d["box"] for d in items],
            [ids[d["cls"]] for d in items],
            [d["conf"] for d in items],
            names,
            classes,
            conf,
            [d.get("mask") for d in items],
        )

    def detect(self, frame: np.ndarray) -> list[Detection]:
        return list(self.detections)


def export_model(model: str, imgsz: int, runtime: str, out_dir: str | Path = "models") -> Path:
    """Download `<model>.pt` into `out_dir` and export it for `runtime`. Returns the export path."""
    from ultralytics import YOLO, settings

    settings.update({"sync": False})  # no analytics or crash reports
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    weights = out_dir / f"{model}.pt"
    yolo = YOLO(str(weights))  # downloads the official weights to `weights` if missing
    return Path(yolo.export(format=runtime, imgsz=imgsz))
