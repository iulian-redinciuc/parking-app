"""Occupancy evaluation against ground-truth labels (docs/design/vision.md §10).

"Positive" is **free**: saying free when a slot is taken sends someone to a full lot, so
free-precision is the number to watch. Slots labelled `unsure` are left out of every metric.
Detections are cached per image so a threshold sweep only reruns the scoring.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from parking.config import ImageLabels, LabelFile, SlotFile
from parking.vision.detector import Detection, Detector, FakeDetector
from parking.vision.occupancy import Mode, Size, SlotResult, score_slots

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


# --- metrics ---


@dataclass(frozen=True)
class ImageEval:
    image: str
    conditions: tuple[str, ...]
    labelled: int  # slots compared (unsure excluded)
    correct: int
    true_free: int
    pred_free: int
    said_free: tuple[str, ...]  # taken, predicted free (the worse error)
    said_taken: tuple[str, ...]  # free, predicted taken

    @property
    def free_tp(self) -> int:
        return self.pred_free - len(self.said_free)

    @property
    def count_error(self) -> int:
        return abs(self.pred_free - self.true_free)

    def notes(self) -> dict[str, str]:
        """Slot id -> what it should have been, for the mistakes image."""
        return {
            **{sid: "should be taken" for sid in self.said_free},
            **{sid: "should be free" for sid in self.said_taken},
        }


def check_labels(labels: LabelFile, slot_ids: Iterable[str]) -> None:
    """Every slot id in the labels must exist in the slot file (catches typos)."""
    known = set(slot_ids)
    bad = {
        image: sorted(set(lab.taken + lab.unsure) - known)
        for image, lab in labels.images.items()
        if set(lab.taken + lab.unsure) - known
    }
    if bad:
        parts = "; ".join(f"{img}: {', '.join(ids)}" for img, ids in bad.items())
        raise ValueError(f"labels name slot(s) not in the slot file: {parts}")


def evaluate_image(image: str, results: Sequence[SlotResult], label: ImageLabels) -> ImageEval:
    """Compare one image's slot results with its labels (every slot not taken/unsure is free)."""
    taken, unsure = set(label.taken), set(label.unsure)
    compared = [r for r in results if r.id not in unsure]
    said_free = tuple(r.id for r in compared if not r.taken and r.id in taken)
    said_taken = tuple(r.id for r in compared if r.taken and r.id not in taken)
    return ImageEval(
        image=image,
        conditions=tuple(label.conditions),
        labelled=len(compared),
        correct=len(compared) - len(said_free) - len(said_taken),
        true_free=sum(r.id not in taken for r in compared),
        pred_free=sum(not r.taken for r in compared),
        said_free=said_free,
        said_taken=said_taken,
    )


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


@dataclass(frozen=True)
class Summary:
    images: int
    labelled: int
    correct: int
    free_tp: int
    free_fp: int  # predicted free, truly taken
    free_fn: int  # predicted taken, truly free
    count_errors: tuple[int, ...]

    @property
    def accuracy(self) -> float | None:
        return _ratio(self.correct, self.labelled)

    @property
    def free_precision(self) -> float | None:
        return _ratio(self.free_tp, self.free_tp + self.free_fp)

    @property
    def free_recall(self) -> float | None:
        return _ratio(self.free_tp, self.free_tp + self.free_fn)

    @property
    def mean_count_error(self) -> float | None:
        return _ratio(sum(self.count_errors), len(self.count_errors))

    @property
    def count_within_1(self) -> float | None:
        """Share of images whose free count is off by at most 1."""
        return _ratio(sum(e <= 1 for e in self.count_errors), len(self.count_errors))

    def to_dict(self) -> dict[str, Any]:
        keys = ("accuracy", "free_precision", "free_recall", "mean_count_error", "count_within_1")
        return {
            "images": self.images,
            "labelled": self.labelled,
            "correct": self.correct,
            "free_tp": self.free_tp,
            "free_fp": self.free_fp,
            "free_fn": self.free_fn,
            **{k: getattr(self, k) for k in keys},
        }


def summarize(evals: Sequence[ImageEval]) -> Summary:
    return Summary(
        images=len(evals),
        labelled=sum(e.labelled for e in evals),
        correct=sum(e.correct for e in evals),
        free_tp=sum(e.free_tp for e in evals),
        free_fp=sum(len(e.said_free) for e in evals),
        free_fn=sum(len(e.said_taken) for e in evals),
        count_errors=tuple(e.count_error for e in evals),
    )


def by_condition(evals: Sequence[ImageEval]) -> dict[str, Summary]:
    """One summary per condition tag (an image with two tags counts in both)."""
    tags = sorted({t for e in evals for t in e.conditions})
    return {t: summarize([e for e in evals if t in e.conditions]) for t in tags}


# --- threshold sweep ---


def parse_sweep(spec: str) -> list[float]:
    """'0.1:0.6:0.05' -> [0.1, 0.15, ..., 0.6] (end included), each strictly between 0 and 1."""
    try:
        start, stop, step = (float(p) for p in spec.split(":"))
    except ValueError:
        raise ValueError(f"sweep must be start:stop:step, got {spec!r}") from None
    if step <= 0 or start > stop:
        raise ValueError(f"sweep needs step > 0 and start <= stop, got {spec!r}")
    n = math.floor((stop - start) / step + 1e-9)
    values = [round(start + i * step, 6) for i in range(n + 1)]
    if not all(0 < v < 1 for v in values):
        raise ValueError(f"sweep thresholds must be between 0 and 1, got {spec!r}")
    return values


@dataclass(frozen=True)
class Frame:
    """One labelled image's cached detections."""

    image: str
    frame_size: Size
    detections: list[Detection]


def score_frame(f: Frame, slot_file: SlotFile, mode: Mode, threshold: float) -> list[SlotResult]:
    return score_slots(
        f.detections, slot_file.slots, f.frame_size, mode, threshold, slot_file.image_size
    )


def evaluate_frames(
    frames: Sequence[Frame], slot_file: SlotFile, labels: LabelFile, mode: Mode, threshold: float
) -> list[ImageEval]:
    return [
        evaluate_image(f.image, score_frame(f, slot_file, mode, threshold), labels.images[f.image])
        for f in frames
    ]


def sweep(
    frames: Sequence[Frame],
    slot_file: SlotFile,
    labels: LabelFile,
    mode: Mode,
    thresholds: Iterable[float],
) -> list[tuple[float, Summary]]:
    return [(t, summarize(evaluate_frames(frames, slot_file, labels, mode, t))) for t in thresholds]


def best_threshold(rows: Sequence[tuple[float, Summary]], prefer: float) -> float:
    """Highest slot accuracy, then free-precision, then lowest mean count error;
    a remaining tie goes to the threshold closest to `prefer` (the configured one)."""

    def key(row: tuple[float, Summary]):
        t, s = row
        return (
            s.accuracy or 0.0,
            s.free_precision or 0.0,
            -(s.mean_count_error or 0.0),
            -abs(t - prefer),
        )

    if not rows:
        raise ValueError("no sweep rows")
    return max(rows, key=key)[0]


# --- detection cache ---


def list_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def cache_path(cache_dir: Path, image: Path, model: str, imgsz: int) -> Path:
    """`<cache_dir>/<image>.<model>.<imgsz>.json`"""
    return Path(cache_dir) / f"{image.name}.{Path(model).name}.{imgsz}.json"


def _cache_key(image: Path, settings: Mapping[str, Any]) -> dict[str, Any]:
    st = image.stat()
    return {"image_bytes": st.st_size, "image_mtime_ns": st.st_mtime_ns, **settings}


def save_detections(path: Path, frame: Frame, key: Mapping[str, Any]) -> None:
    """Write the detections in the FakeDetector sidecar format plus the cache key."""
    dets = [
        {
            "cls": d.cls,
            "conf": round(d.conf, 4),
            "box": [round(v, 2) for v in d.box],
            "mask": None if d.mask is None else np.round(d.mask, 2).tolist(),
        }
        for d in frame.detections
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"key": dict(key), "frame_size": list(frame.frame_size), "detections": dets}
    path.write_text(json.dumps(data) + "\n")


def load_detections(path: Path, image: str, key: Mapping[str, Any]) -> Frame | None:
    """The cached frame, or None if there is no cache or it was made with other settings."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if data.get("key") != dict(key):
        return None
    dets = FakeDetector(path).detections
    return Frame(image=image, frame_size=tuple(data["frame_size"]), detections=dets)


def detect_frames(
    images: Sequence[Path],
    read: Callable[[Path], np.ndarray | None],
    detector: Callable[[], Detector],
    cache_dir: Path | None,
    model: str,
    imgsz: int,
    settings: Mapping[str, Any],
) -> tuple[list[Frame], int]:
    """Detections for every image, from the cache when it matches `settings` (conf, classes,
    masks) and the image file; `detector` is only created on a cache miss.
    Returns the frames and the number of images actually run through the detector."""
    frames, ran, det = [], 0, None
    for img in images:
        key = _cache_key(img, settings)
        path = cache_path(cache_dir, img, model, imgsz) if cache_dir else None
        frame = load_detections(path, img.name, key) if path else None
        if frame is None:
            pixels = read(img)
            if pixels is None:
                raise ValueError(f"can't read image {img}")
            det = det or detector()
            h, w = pixels.shape[:2]
            frame = Frame(img.name, (w, h), det.detect(pixels))
            ran += 1
            if path:
                save_detections(path, frame, key)
        frames.append(frame)
    return frames, ran


# --- report ---


def pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def num(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def image_table(evals: Sequence[ImageEval]) -> list[str]:
    head = f"{'image':<22} {'conditions':<12} {'correct':>9} {'acc':>7} "
    head += f"{'free t/p':>9} {'err':>4}  mistakes"
    lines = [head, "-" * len(head)]
    for e in evals:
        mistakes = [f"{s} (taken, said free)" for s in e.said_free]
        mistakes += [f"{s} (free, said taken)" for s in e.said_taken]
        lines.append(
            f"{e.image:<22} {','.join(e.conditions) or '-':<12} "
            f"{f'{e.correct}/{e.labelled}':>9} {pct(_ratio(e.correct, e.labelled)):>7} "
            f"{f'{e.true_free}/{e.pred_free}':>9} {e.count_error:>4}  "
            f"{', '.join(mistakes) or '-'}"
        )
    return lines


def summary_line(name: str, s: Summary) -> str:
    return (
        f"{name}: slot accuracy {pct(s.accuracy)} ({s.correct}/{s.labelled}), "
        f"free-precision {pct(s.free_precision)}, free-recall {pct(s.free_recall)}, "
        f"count error {num(s.mean_count_error)} (<= 1 in {pct(s.count_within_1)} of "
        f"{s.images} image(s))"
    )


def sweep_table(rows: Sequence[tuple[float, Summary]], best: float) -> list[str]:
    head = f"{'threshold':>9} {'accuracy':>9} {'free-prec':>10} {'free-rec':>9} {'count err':>10}"
    lines = [head, "-" * len(head)]
    for t, s in rows:
        mark = "  <- best" if t == best else ""
        lines.append(
            f"{t:>9.2f} {pct(s.accuracy):>9} {pct(s.free_precision):>10} "
            f"{pct(s.free_recall):>9} {num(s.mean_count_error):>10}{mark}"
        )
    return lines
