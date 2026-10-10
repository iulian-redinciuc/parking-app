"""Labelled frames → a detector training set (P9.4, docs/design/detector-training.md).

Every `taken` slot of a labelled frame becomes one box (the slot polygon's bounding box,
class `car`): rough, so the boxes are reviewed before training. Layout (Ultralytics YOLO,
plus COCO JSON derived from the same text files for YOLOX):

    <out>/images/train|val/<frame>.jpg     copies (unsure slots painted grey)
    <out>/labels/train|val/<frame>.txt     `0 cx cy w h`, normalised
    <out>/data.yaml                        for `yolo detect train data=…`
    <out>/annotations/train.json|val.json  COCO format, from the .txt files
    <out>/holdout.json                     the val frames' slot labels, for `parking evaluate`
    <out>/review/<frame>.jpg               boxes drawn on the frame (`--review`)
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from parking.config import LabelFile, SlotFile
from parking.vision.occupancy import _scaled
from parking.vision.validation import held_out

CLASS_NAMES = ("car",)  # one class: whatever is parked in a space
SPLITS = ("train", "val")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
GREY = (114, 114, 114)  # the padding colour of YOLO / YOLOX letterboxing
REVIEW_MAX_SIDE = 1600

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 in frame pixels


@dataclass(frozen=True)
class ExportStats:
    train: int
    val: int
    boxes: int
    masked: int  # unsure slots painted out
    missing: tuple[str, ...]  # labelled images that aren't in the folder


def slot_box(points: Sequence[Sequence[float]], frame_size: tuple[int, int], pad: float) -> Box:
    """The polygon's bounding box, grown by `pad` (share of its size) and clipped to the frame."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
    w, h = frame_size
    return (max(0.0, x1 - dx), max(0.0, y1 - dy), min(float(w), x2 + dx), min(float(h), y2 + dy))


def yolo_line(box: Box, frame_size: tuple[int, int], cls: int = 0) -> str:
    w, h = frame_size
    x1, y1, x2, y2 = box
    return (
        f"{cls} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
        f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}"
    )


def parse_yolo(text: str, frame_size: tuple[int, int]) -> list[tuple[int, Box]]:
    """The boxes of a YOLO label file, back in frame pixels."""
    w, h = frame_size
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValueError(f"line {n}: expected 'class cx cy w h', got '{line}'")
        cx, cy, bw, bh = (float(v) for v in parts[1:])
        box = ((cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h)
        out.append((int(parts[0]), box))
    return out


def frame_boxes(
    slot_file: SlotFile, taken: Iterable[str], frame_size: tuple[int, int], pad: float
) -> list[Box]:
    taken = set(taken)
    return [
        slot_box(_scaled(s.polygon, slot_file.image_size, frame_size), frame_size, pad)
        for s in slot_file.slots
        if s.id in taken
    ]


def mask_slots(
    frame: np.ndarray, slot_file: SlotFile, ids: Iterable[str], pad: float
) -> np.ndarray:
    """Paint the given slots' boxes grey: an unsure space must be neither a car nor background."""
    out = frame.copy()
    size = (frame.shape[1], frame.shape[0])
    for x1, y1, x2, y2 in frame_boxes(slot_file, ids, size, pad):
        cv2.rectangle(out, (int(x1), int(y1)), (int(x2), int(y2)), GREY, thickness=-1)
    return out


def draw_review(frame: np.ndarray, boxes: Sequence[Box]) -> np.ndarray:
    """The frame with its boxes drawn, shrunk to a size that is quick to flip through."""
    out = frame.copy()
    thick = max(2, round(max(frame.shape[:2]) / 500))
    for x1, y1, x2, y2 in boxes:
        cv2.rectangle(out, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), thick)
    scale = REVIEW_MAX_SIDE / max(out.shape[:2])
    if scale < 1:
        out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return out


def data_yaml(out: Path) -> str:
    names = "\n".join(f"  {i}: {n}" for i, n in enumerate(CLASS_NAMES))
    return f"path: {out.resolve()}\ntrain: images/train\nval: images/val\nnames:\n{names}\n"


def export_frames(
    images: Path,
    labels: LabelFile,
    slot_file: SlotFile,
    out: Path,
    holdout: float = 0.2,
    pad: float = 0.0,
    review: bool = False,
) -> ExportStats:
    """Write the dataset for the labelled frames found in `images`."""
    for split in SPLITS:
        (out / "images" / split).mkdir(parents=True, exist_ok=True)
        (out / "labels" / split).mkdir(parents=True, exist_ok=True)
    if review:
        (out / "review").mkdir(exist_ok=True)

    counts = {"train": 0, "val": 0}
    boxes_total = masked = 0
    missing, held = [], {}
    for name, lab in sorted(labels.images.items()):
        src = images / name
        frame = cv2.imread(str(src))
        if frame is None:
            missing.append(name)
            continue
        size = (frame.shape[1], frame.shape[0])
        split = "val" if held_out(name, holdout) else "train"
        if split == "val":
            held[name] = lab.model_dump()
        boxes = frame_boxes(slot_file, lab.taken, size, pad)
        target = out / "images" / split / name
        if lab.unsure:
            frame = mask_slots(frame, slot_file, lab.unsure, pad)
            masked += len(lab.unsure)
            params = [cv2.IMWRITE_JPEG_QUALITY, 95] if src.suffix.lower() != ".png" else []
            cv2.imwrite(str(target), frame, params)
        else:
            shutil.copyfile(src, target)
        text = "".join(yolo_line(b, size) + "\n" for b in boxes)
        (out / "labels" / split / f"{src.stem}.txt").write_text(text)
        if review:
            cv2.imwrite(str(out / "review" / f"{src.stem}.jpg"), draw_review(frame, boxes))
        counts[split] += 1
        boxes_total += len(boxes)

    (out / "data.yaml").write_text(data_yaml(out))
    held_file = {"version": 1, "camera_id": labels.camera_id, "images": held}
    (out / "holdout.json").write_text(json.dumps(held_file, indent=2) + "\n")
    write_coco(out)
    return ExportStats(counts["train"], counts["val"], boxes_total, masked, tuple(missing))


def coco_split(out: Path, split: str) -> dict:
    """COCO annotations for one split, read back from its images and .txt label files."""
    images, annotations = [], []
    folder = out / "images" / split
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    for image_id, path in enumerate(files, 1):
        frame = cv2.imread(str(path))
        if frame is None:
            raise ValueError(f"can't read image {path}")
        h, w = frame.shape[:2]
        images.append({"id": image_id, "file_name": path.name, "width": w, "height": h})
        label = out / "labels" / split / f"{path.stem}.txt"
        text = label.read_text() if label.is_file() else ""
        try:
            rows = parse_yolo(text, (w, h))
        except ValueError as e:
            raise ValueError(f"{label}: {e}") from e
        for cls, (x1, y1, x2, y2) in rows:
            if not 0 <= cls < len(CLASS_NAMES):
                raise ValueError(f"{label}: class {cls} is not one of {list(CLASS_NAMES)}")
            bw, bh = x2 - x1, y2 - y1
            annotations.append(
                {
                    "id": len(annotations) + 1,
                    "image_id": image_id,
                    "category_id": cls + 1,
                    "bbox": [round(x1, 2), round(y1, 2), round(bw, 2), round(bh, 2)],
                    "area": round(bw * bh, 2),
                    "iscrowd": 0,
                }
            )
    categories = [{"id": i + 1, "name": n} for i, n in enumerate(CLASS_NAMES)]
    return {"images": images, "annotations": annotations, "categories": categories}


def write_coco(out: Path) -> dict[str, int]:
    """(Re)write `annotations/<split>.json` from the label files; returns boxes per split.
    Run again after correcting boxes in a labelling tool."""
    (out / "annotations").mkdir(parents=True, exist_ok=True)
    totals = {}
    for split in SPLITS:
        data = coco_split(out, split)
        (out / "annotations" / f"{split}.json").write_text(json.dumps(data) + "\n")
        totals[split] = len(data["annotations"])
    return totals
