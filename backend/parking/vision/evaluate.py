"""Occupancy and flow evaluation against ground-truth labels (docs/design/vision.md §10).

"Positive" is **free**: saying free when a slot is taken sends someone to a full lot, so
free-precision is the number to watch. Slots labelled `unsure` are left out of every metric.
Detections are cached per image so a threshold sweep only reruns the scoring. With
`occupancy.method: appearance` there are no detections: each image's slot scores are
computed once and a sweep only re-thresholds them (no cache needed).

Flow (P5.9): `run_flow` plays a clip through the flow pipeline (gate → tracker → two-line
counter) and `match_flow` compares the counted events with a tally CSV (config.md §4): same
direction within ±`FLOW_MATCH_S`, closest pairs first; TP/FP/FN, event accuracy and net error.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from parking.config import AppearanceCfg, ImageLabels, LabelFile, OccupancyCfg, SlotFile
from parking.vision.appearance import score_slots_appearance
from parking.vision.detector import Detection, Detector, FakeDetector
from parking.vision.occupancy import Mode, Size, SlotResult, score_slots
from parking.vision.slot_classifier import Classifier, score_slots_classifier

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
# The occupancy target (phase 4, P4.9): below it, P4.10 (per-slot classifier) is needed.
TARGET_ACCURACY = 0.97
TARGET_COUNT_WITHIN_1 = 0.95


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

    @property
    def meets_target(self) -> bool | None:
        """Slot accuracy >= TARGET_ACCURACY and count error <= 1 on >= TARGET_COUNT_WITHIN_1
        of the images; None without data."""
        if self.accuracy is None or self.count_within_1 is None:
            return None
        return self.accuracy >= TARGET_ACCURACY and self.count_within_1 >= TARGET_COUNT_WITHIN_1

    def to_dict(self) -> dict[str, Any]:
        keys = (
            "accuracy",
            "free_precision",
            "free_recall",
            "mean_count_error",
            "count_within_1",
            "meets_target",
        )
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
    """One labelled image's cached detections, or its appearance scores (slot file order)."""

    image: str
    frame_size: Size
    detections: list[Detection]
    scores: tuple[float, ...] | None = None


def score_frame(f: Frame, slot_file: SlotFile, mode: Mode, threshold: float) -> list[SlotResult]:
    """Slot results at `threshold`: re-thresholded appearance scores, else detection overlap."""
    if f.scores is not None:
        return [
            SlotResult(id=s.id, score=sc, taken=sc >= threshold)
            for s, sc in zip(slot_file.slots, f.scores, strict=True)
        ]
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


def scored_frames(
    images: Sequence[Path],
    read: Callable[[Path], np.ndarray | None],
    score: Callable[[np.ndarray, Size], list[SlotResult]],
) -> list[Frame]:
    """Slot scores for every image from `score(pixels, (w, h))`; no detector, no cache."""
    frames = []
    for img in images:
        pixels = read(img)
        if pixels is None:
            raise ValueError(f"can't read image {img}")
        h, w = pixels.shape[:2]
        results = score(pixels, (w, h))
        frames.append(Frame(img.name, (w, h), [], tuple(r.score for r in results)))
    return frames


def appearance_frames(
    images: Sequence[Path],
    read: Callable[[Path], np.ndarray | None],
    slot_file: SlotFile,
    params: AppearanceCfg,
    reference: np.ndarray | None = None,
) -> list[Frame]:
    """Appearance scores for every image (vision.md §2.1)."""
    return scored_frames(
        images,
        read,
        lambda px, size: score_slots_appearance(
            px, slot_file.slots, size, slot_file.image_size, 0.5, params, reference
        ),
    )


def classifier_frames(
    images: Sequence[Path],
    read: Callable[[Path], np.ndarray | None],
    slot_file: SlotFile,
    occ: OccupancyCfg,
    clf: Classifier,
    reference: np.ndarray | None = None,
) -> list[Frame]:
    """Slot classifier (or ensemble) scores for every image (vision.md §9)."""
    return scored_frames(
        images,
        read,
        lambda px, size: score_slots_classifier(
            px,
            slot_file.slots,
            size,
            slot_file.image_size,
            clf,
            ensemble=occ.method == "ensemble",
            appearance_threshold=occ.threshold,
            appearance=occ.appearance,
            reference=reference,
        ),
    )


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


def target_line(s: Summary) -> str:
    """The P4.9 verdict: target met, or which part missed it."""
    goal = (
        f"slot accuracy >= {TARGET_ACCURACY * 100:.0f}% and count error <= 1 on "
        f">= {TARGET_COUNT_WITHIN_1 * 100:.0f}% of images"
    )
    if s.meets_target is None:
        return f"target ({goal}): n/a (no labelled slots)"
    if s.meets_target:
        return f"target ({goal}): met"
    short = []
    if (s.accuracy or 0.0) < TARGET_ACCURACY:
        short.append(f"slot accuracy {pct(s.accuracy)}")
    if (s.count_within_1 or 0.0) < TARGET_COUNT_WITHIN_1:
        short.append(f"count error <= 1 in {pct(s.count_within_1)}")
    return f"target ({goal}): missed ({', '.join(short)})"


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


# --- flow (per clip) ---

FLOW_MATCH_S = 2.0  # a counted event matches a tallied one of the same direction this close
TARGET_EVENT_ACCURACY = 0.98  # the flow target (P5.9), per clip
FLOW_CSV_HEADER = ("video_time_s", "direction", "note")


@dataclass(frozen=True)
class FlowLabel:
    """One crossing at `t` seconds into the clip (tallied, or counted by the pipeline)."""

    t: float
    direction: str  # in | out
    note: str = ""


def parse_flow_labels(text: str, name: str = "labels") -> list[FlowLabel]:
    """Read a flow labels CSV (config.md §4): header `video_time_s,direction[,note]`, then one
    row per crossing; blank lines are skipped. Sorted by time."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"{name}: empty file (header video_time_s,direction,note expected)")
    head = tuple(c.strip() for c in rows[0])
    if head not in (FLOW_CSV_HEADER, FLOW_CSV_HEADER[:2]):
        want, got = ",".join(FLOW_CSV_HEADER), ",".join(head)
        raise ValueError(f"{name}: header must be {want}, got {got}")
    out = []
    for n, row in enumerate(rows[1:], start=2):
        if len(row) > len(head) or len(row) < 2:
            raise ValueError(f"{name} line {n}: expected {len(head)} columns, got {len(row)}")
        try:
            t = float(row[0])
        except ValueError:
            raise ValueError(f"{name} line {n}: video_time_s {row[0]!r} is not a number") from None
        if not math.isfinite(t) or t < 0:
            raise ValueError(f"{name} line {n}: video_time_s must be >= 0")
        direction = row[1].strip().lower()
        if direction not in ("in", "out"):
            raise ValueError(f"{name} line {n}: direction must be in or out, got {row[1]!r}")
        out.append(FlowLabel(t, direction, row[2].strip() if len(row) > 2 else ""))
    return sorted(out, key=lambda e: e.t)


def load_flow_labels(path: Path) -> list[FlowLabel]:
    return parse_flow_labels(path.read_text(encoding="utf-8"), path.name)


def flow_labels_csv(events: Iterable[FlowLabel]) -> str:
    """The same CSV format, e.g. the pipeline's events for the tally tool's _Import CSV_."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FLOW_CSV_HEADER)
    for e in sorted(events, key=lambda e: e.t):
        w.writerow([f"{e.t:.1f}", e.direction, e.note])
    return buf.getvalue()


@dataclass(frozen=True)
class FlowEval:
    matches: tuple[tuple[FlowLabel, FlowLabel], ...]  # (truth, predicted)
    false_pos: tuple[FlowLabel, ...]  # counted, not in the tally (extra counts)
    missed: tuple[FlowLabel, ...]  # tallied, not counted
    true_in: int
    true_out: int
    pred_in: int
    pred_out: int

    @property
    def tp(self) -> int:
        return len(self.matches)

    @property
    def fp(self) -> int:
        return len(self.false_pos)

    @property
    def fn(self) -> int:
        return len(self.missed)

    @property
    def accuracy(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fp + self.fn)

    @property
    def net_error(self) -> int:
        """(pred in − pred out) − (true in − true out): what makes the zone count drift."""
        return (self.pred_in - self.pred_out) - (self.true_in - self.true_out)

    @property
    def mean_offset(self) -> float | None:
        """Mean predicted − tallied time of the matches (the tally lags a little, usually)."""
        if not self.matches:
            return None
        return sum(p.t - t.t for t, p in self.matches) / len(self.matches)

    @property
    def meets_target(self) -> bool | None:
        acc = self.accuracy
        return None if acc is None else acc >= TARGET_EVENT_ACCURACY

    def to_dict(self) -> dict[str, Any]:
        def ev(e: FlowLabel) -> dict[str, Any]:
            return {"t": round(e.t, 2), "direction": e.direction, "note": e.note}

        mo = self.mean_offset
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "event_accuracy": self.accuracy,
            "net_error": self.net_error,
            "true": {"in": self.true_in, "out": self.true_out},
            "predicted": {"in": self.pred_in, "out": self.pred_out},
            "mean_offset_s": None if mo is None else round(mo, 2),
            "meets_target": self.meets_target,
            "false_positives": [ev(e) for e in self.false_pos],
            "missed": [ev(e) for e in self.missed],
            "matches": [
                {"truth_t": round(t.t, 2), "pred_t": round(p.t, 2), "direction": t.direction}
                for t, p in self.matches
            ],
        }


def match_flow(
    predicted: Sequence[FlowLabel], truth: Sequence[FlowLabel], tolerance: float = FLOW_MATCH_S
) -> FlowEval:
    """Pair predicted with tallied events of the same direction within ±`tolerance` s.

    Greedy by time difference: the closest pair is taken first (ties: the earlier truth, then
    the earlier prediction); each event is used at most once.
    """
    pairs = sorted(
        (abs(p.t - t.t), ti, pi)
        for ti, t in enumerate(truth)
        for pi, p in enumerate(predicted)
        if p.direction == t.direction and abs(p.t - t.t) <= tolerance
    )
    used_t: set[int] = set()
    used_p: set[int] = set()
    matched: list[tuple[int, int]] = []
    for _, ti, pi in pairs:
        if ti in used_t or pi in used_p:
            continue
        used_t.add(ti)
        used_p.add(pi)
        matched.append((ti, pi))
    matched.sort()

    def n(events: Sequence[FlowLabel], d: str) -> int:
        return sum(e.direction == d for e in events)

    return FlowEval(
        matches=tuple((truth[ti], predicted[pi]) for ti, pi in matched),
        false_pos=tuple(p for i, p in enumerate(predicted) if i not in used_p),
        missed=tuple(t for i, t in enumerate(truth) if i not in used_t),
        true_in=n(truth, "in"),
        true_out=n(truth, "out"),
        pred_in=n(predicted, "in"),
        pred_out=n(predicted, "out"),
    )


@dataclass(frozen=True)
class FlowRun:
    """The pipeline's counted events over a clip, plus how the run went."""

    events: list[FlowLabel]
    frames: int
    active_frames: int
    seconds: float
    track_ms_avg: float
    tracks: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "active_frames": self.active_frames,
            "seconds": round(self.seconds, 2),
            "track_ms_avg": round(self.track_ms_avg, 1),
            "tracks": self.tracks,
        }


def run_flow(
    src: Any,
    gate: Any,
    tracker: Any,
    counter: Any,
    seconds: float = math.inf,
    on_frame: Callable[[np.ndarray, float, bool, list[Detection], list[FlowLabel]], None]
    | None = None,
) -> FlowRun:
    """Gate → tracker → two-line counter over every new frame of `src` (as the flow worker
    does); each counted event's `t` is its frame's time from the first frame, in seconds.
    `on_frame(image, elapsed_s, active, tracks, new_events)` is called per frame."""
    from parking.vision.tracking import run_tracking

    events: list[FlowLabel] = []

    def step(image: np.ndarray, elapsed: float, active: bool, tracks: list[Detection]) -> None:
        h, w = image.shape[:2]
        new = [
            FlowLabel(e.ts, e.direction, f"#{e.track_id} {e.cls} {e.conf:.2f}")
            for e in counter.update(tracks, elapsed, (w, h))
        ]
        events.extend(new)
        if on_frame is not None:
            on_frame(image, elapsed, active, tracks, new)

    stats = run_tracking(src, gate, tracker, seconds, on_frame=step)
    return FlowRun(
        events,
        stats.frames,
        stats.active_frames,
        stats.seconds,
        stats.track_ms_avg,
        len(stats.spans),
    )


def _times(events: Sequence[FlowLabel]) -> str:
    parts = [f"{e.t:.1f} s {e.direction}" + (f" ({e.note})" if e.note else "") for e in events]
    return ", ".join(parts) if parts else "none"


def flow_lines(r: FlowEval) -> list[str]:
    """The terminal report for one clip."""
    sign = f"{r.net_error:+d}" if r.net_error else "0"
    mo = r.mean_offset
    goal = f"event accuracy >= {TARGET_EVENT_ACCURACY * 100:.0f}%"
    if r.meets_target is None:
        verdict = f"target ({goal}): n/a (no events in the tally or the run)"
    else:
        verdict = f"target ({goal}): {'met' if r.meets_target else 'missed'}"
    return [
        f"truth      in {r.true_in:4d}  out {r.true_out:4d}",
        f"predicted  in {r.pred_in:4d}  out {r.pred_out:4d}",
        f"TP {r.tp}  FP {r.fp}  FN {r.fn}  event accuracy {pct(r.accuracy)}  net error {sign}"
        + ("" if mo is None else f"  (counted {mo:+.1f} s after the tally on average)"),
        verdict,
        f"extra counts (FP): {_times(r.false_pos)}",
        f"missed (FN): {_times(r.missed)}",
    ]
