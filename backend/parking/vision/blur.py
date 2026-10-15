"""Blur people and number plates in kept frames (P4.8 step 4, security-privacy.md §4).

Validation frames are kept longer than the debug captures' retention, so faces and plates in
them are blurred: boxes the detector finds as `person`, plus boxes drawn by hand (plates; the
COCO models have no plate class). Each box is grown by `PAD` and replaced by a strong Gaussian
blur of itself, in place. A `blurred.json` next to the images records what was done, so a
second run only touches new images and new manual boxes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path

import cv2
import numpy as np

PAD = 0.15  # box growth on every side, as a share of its size
LOG = "blurred.json"

Box = tuple[int, int, int, int]  # x1, y1, x2, y2 in image pixels


def grow(box: Sequence[float], shape: tuple[int, ...], pad: float = PAD) -> Box:
    """`box` grown by `pad` of its size on every side, clipped to the image."""
    h, w = shape[:2]
    x1, y1, x2, y2 = box
    dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
    return (
        max(int(x1 - dx), 0),
        max(int(y1 - dy), 0),
        min(int(np.ceil(x2 + dx)), w),
        min(int(np.ceil(y2 + dy)), h),
    )


def blur_boxes(image: np.ndarray, boxes: Iterable[Box]) -> np.ndarray:
    """`image` with every box blurred beyond recognition (sigma = ¼ of the box's short side)."""
    out = image.copy()
    for x1, y1, x2, y2 in boxes:
        if x2 <= x1 or y2 <= y1:
            continue
        patch = out[y1:y2, x1:x2]
        sigma = max(min(x2 - x1, y2 - y1) / 4, 2.0)
        out[y1:y2, x1:x2] = cv2.GaussianBlur(patch, (0, 0), sigma)
    return out


def load_log(folder: Path) -> dict[str, list[dict]]:
    path = folder / LOG
    return json.loads(path.read_text()) if path.is_file() else {}


def save_log(folder: Path, log: dict[str, list[dict]]) -> None:
    (folder / LOG).write_text(json.dumps(log, indent=1, sort_keys=True) + "\n")


def load_manual(path: Path) -> dict[str, list[Box]]:
    """Hand-drawn boxes: `{"<image>.jpg": [[x1, y1, x2, y2], ...]}`."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("expected {image: [[x1, y1, x2, y2], ...]}")
    out = {}
    for name, boxes in data.items():
        if not isinstance(boxes, list) or not all(
            isinstance(b, list) and len(b) == 4 and all(isinstance(v, int | float) for v in b)
            for b in boxes
        ):
            raise ValueError(f"{name}: expected a list of [x1, y1, x2, y2]")
        out[name] = [tuple(int(v) for v in b) for b in boxes]
    return out
