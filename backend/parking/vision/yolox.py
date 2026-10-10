"""YOLOX detector on ONNX Runtime (vision.md §1, detector-training.md): the Apache-2.0
alternative to the Ultralytics models, behind the same `Detector` protocol. Boxes only.

It runs the ONNX file YOLOX's own `tools/export_onnx.py` writes: input BGR, 0–255, NCHW,
letterboxed to the top-left corner on grey 114; output `[1, N, 5 + classes]` per grid cell
(`cx, cy, w, h, objectness, class scores`), by default not yet decoded from the grids.
An optional `<model>.json` sidecar names the classes of a fine-tuned model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from parking.vision.detector import Detection, class_ids

STRIDES = (8, 16, 32)
PAD_VALUE = 114
NMS_IOU = 0.45

# The 80 COCO classes in the order the official YOLOX models were trained on.
_COCO = (
    "person,bicycle,car,motorcycle,airplane,bus,train,truck,boat,traffic light,fire hydrant,"
    "stop sign,parking meter,bench,bird,cat,dog,horse,sheep,cow,elephant,bear,zebra,giraffe,"
    "backpack,umbrella,handbag,tie,suitcase,frisbee,skis,snowboard,sports ball,kite,"
    "baseball bat,baseball glove,skateboard,surfboard,tennis racket,bottle,wine glass,cup,fork,"
    "knife,spoon,bowl,banana,apple,sandwich,orange,broccoli,carrot,hot dog,pizza,donut,cake,"
    "chair,couch,potted plant,bed,dining table,toilet,tv,laptop,mouse,remote,keyboard,"
    "cell phone,microwave,oven,toaster,sink,refrigerator,book,clock,vase,scissors,teddy bear,"
    "hair drier,toothbrush"
)
COCO_NAMES = _COCO.split(",")


def meta_path(model: Path) -> Path:
    """`models/yolox_nano.onnx` → `models/yolox_nano.json`"""
    return Path(model).with_suffix(".json")


def read_meta(model: Path) -> dict:
    path = meta_path(model)
    return json.loads(path.read_text()) if path.is_file() else {}


def letterbox(frame: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float]:
    """Resize keeping the aspect ratio into the top-left of a grey `size` (h, w) canvas.
    Returns the NCHW float32 blob and the scale (model pixels per frame pixel)."""
    h, w = frame.shape[:2]
    ratio = min(size[0] / h, size[1] / w)
    new_w, new_h = max(1, int(w * ratio)), max(1, int(h * ratio))
    canvas = np.full((size[0], size[1], 3), PAD_VALUE, np.uint8)
    canvas[:new_h, :new_w] = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    return canvas.transpose(2, 0, 1)[None].astype(np.float32), ratio


def decode(pred: np.ndarray, size: tuple[int, int], strides: Sequence[int] = STRIDES) -> np.ndarray:
    """Grid outputs → boxes in model pixels: `cx, cy` are offsets in cells, `w, h` are logs."""
    grids, scales = [], []
    for s in strides:
        gh, gw = size[0] // s, size[1] // s
        xv, yv = np.meshgrid(np.arange(gw), np.arange(gh))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        scales.append(np.full((gh * gw, 1), s))
    grid, scale = np.concatenate(grids), np.concatenate(scales)
    if len(grid) != len(pred):
        raise ValueError(
            f"model output has {len(pred)} cells, expected {len(grid)} for input "
            f"{size[1]}x{size[0]} and strides {list(strides)}"
        )
    out = pred.astype(np.float32, copy=True)
    out[:, :2] = (out[:, :2] + grid) * scale
    out[:, 2:4] = np.exp(np.clip(out[:, 2:4], None, 20)) * scale
    return out


def postprocess(
    pred: np.ndarray,
    ratio: float,
    frame_size: tuple[int, int],
    names: Sequence[str],
    keep_ids: Iterable[int],
    conf: float,
    iou: float = NMS_IOU,
) -> list[Detection]:
    """Decoded rows → `Detection`s in frame pixels: score = objectness × best wanted class,
    then per-class NMS. `frame_size` is (width, height); boxes are clipped to it."""
    ids = sorted(keep_ids)
    if not ids or not len(pred):
        return []
    cls_scores = pred[:, 5:][:, ids] * pred[:, 4:5]
    best = cls_scores.argmax(1)
    scores = cls_scores[np.arange(len(pred)), best]
    ok = scores >= conf
    if not ok.any():
        return []
    rows, scores, cls = pred[ok], scores[ok], np.asarray(ids)[best[ok]]
    half = rows[:, 2:4] / 2
    boxes = np.concatenate((rows[:, :2] - half, rows[:, :2] + half), 1) / ratio
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, frame_size[0])
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, frame_size[1])
    # per-class NMS in one call: boxes of different classes are moved apart
    shifted = boxes + (cls * (max(frame_size) + 1.0))[:, None]
    xywh = np.concatenate((shifted[:, :2], shifted[:, 2:] - shifted[:, :2]), 1)
    kept = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), conf, iou)
    return [
        Detection(
            cls=names[int(cls[i])],
            conf=float(scores[i]),
            box=tuple(float(v) for v in boxes[i]),
        )
        for i in np.asarray(kept, dtype=int).reshape(-1)
    ]


def _session(model_path: str, threads: int):
    import onnxruntime as ort  # heavy import, only when a real model is used

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    return ort.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])


class YoloxDetector:
    """A YOLOX ONNX model, loaded once. Same contract as `YoloDetector`, without masks."""

    def __init__(
        self,
        model_path: str,
        imgsz: int,
        conf: float,
        classes: list[str],
        session: Any = None,  # tests pass a stand-in with get_inputs() and run()
        threads: int = 2,
    ):
        self.model_path = Path(model_path)
        meta = read_meta(self.model_path)
        self.names: list[str] = list(meta.get("classes") or COCO_NAMES)
        self.decoded = bool(meta.get("decoded", False))
        self.strides = tuple(meta.get("strides") or STRIDES)
        self.conf = conf
        self.classes = list(classes)
        self._class_ids = class_ids(dict(enumerate(self.names)), self.classes)
        self.session = session or _session(str(model_path), threads)
        inp = self.session.get_inputs()[0]
        self._input = inp.name
        fixed = [d for d in inp.shape[2:] if isinstance(d, int)]
        if len(fixed) == 2:
            if imgsz not in fixed:
                raise ValueError(
                    f"{model_path} takes {fixed[1]}x{fixed[0]} input; "
                    f"set detector.imgsz to {max(fixed)}"
                )
            self.size = (fixed[0], fixed[1])
        else:
            if imgsz % max(self.strides):
                raise ValueError(f"detector.imgsz must be a multiple of {max(self.strides)}")
            self.size = (imgsz, imgsz)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        blob, ratio = letterbox(frame, self.size)
        pred = np.asarray(self.session.run(None, {self._input: blob})[0])[0]
        if pred.shape[1] != 5 + len(self.names):
            raise ValueError(
                f"model has {pred.shape[1] - 5} class(es) but {len(self.names)} name(s): "
                f'list them as "classes" in {meta_path(self.model_path)}'
            )
        if not self.decoded:
            pred = decode(pred, self.size, self.strides)
        h, w = frame.shape[:2]
        return postprocess(pred, ratio, (w, h), self.names, self._class_ids, self.conf)
