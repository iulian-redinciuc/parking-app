"""Speed and memory per runtime × input size × model, plus the appearance scorer (vision.md §11).

Every case runs in its own spawned process, so the peak RSS of one model doesn't hide
another's and imports are paid by every case alike.
"""

from __future__ import annotations

import math
import resource
import shutil
import statistics
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from multiprocessing import get_context
from pathlib import Path
from typing import Any

RUNTIMES = ("pytorch", "ncnn")


@dataclass(frozen=True)
class Case:
    runtime: str  # "pytorch" | "ncnn" | "appearance"
    imgsz: int | None = None  # None for appearance
    model: str | None = None  # e.g. "yolo11n-seg"; None for appearance

    @property
    def name(self) -> str:
        if self.runtime == "appearance":
            return "appearance"
        return f"{self.model} {self.runtime} @ {self.imgsz}"


@dataclass(frozen=True)
class CaseResult:
    case: Case
    runs: int
    median_ms: float
    p95_ms: float
    peak_rss_mb: float
    detections: int | None = None  # vehicles found in the last run (detector cases)
    error: str | None = None


def cases(runtimes: Iterable[str], sizes: Iterable[int], models: Iterable[str]) -> list[Case]:
    """Every runtime × size × model combination, in that nesting order."""
    sizes, models = list(sizes), list(models)
    return [Case(r, s, m) for r in runtimes for s in sizes for m in models]


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100) of a non-empty sequence."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def time_runs(fn: Callable[[], Any], runs: int, warmup: int) -> tuple[list[float], Any]:
    """Call `fn` `warmup` times untimed, then `runs` times; returns (ms per run, last result)."""
    for _ in range(warmup):
        fn()
    times, out = [], None
    for _ in range(runs):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    return times, out


def peak_rss_mb() -> float:
    """Peak resident memory of this process (Linux reports ru_maxrss in KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ncnn_model(models_dir: Path, model: str, imgsz: int) -> Path:
    """`<models>/bench/<model>-<imgsz>/<model>_ncnn_model`, exported on first use.

    NCNN exports have a fixed input size, so each size gets its own folder; the `.pt`
    weights are copied from `models_dir` when present instead of downloaded again.
    """
    from parking.vision.detector import export_model

    folder = models_dir / "bench" / f"{model}-{imgsz}"
    path = folder / f"{model}_ncnn_model"
    if not path.is_dir():
        folder.mkdir(parents=True, exist_ok=True)
        weights = models_dir / f"{model}.pt"
        if weights.is_file() and not (folder / weights.name).exists():
            shutil.copy2(weights, folder / weights.name)
        path = export_model(model, imgsz, "ncnn", folder)
    return path


def model_path(case: Case, models_dir: Path) -> Path:
    if case.runtime == "pytorch":
        return models_dir / f"{case.model}.pt"
    if case.runtime == "ncnn":
        return ncnn_model(models_dir, case.model, case.imgsz)
    raise ValueError(f"unknown runtime {case.runtime}")


def _run_detector(case: Case, image: str, runs: int, warmup: int, models_dir: str) -> CaseResult:
    import cv2

    from parking.vision.detector import COCO_VEHICLES, YoloDetector

    frame = cv2.imread(image)
    path = model_path(case, Path(models_dir))
    use_masks = "-seg" in case.model
    det = YoloDetector(str(path), case.imgsz, 0.25, list(COCO_VEHICLES), use_masks)
    times, dets = time_runs(lambda: det.detect(frame), runs, warmup)
    return _result(case, times, len(dets))


def _run_appearance(case: Case, image: str, runs: int, warmup: int, setup: tuple) -> CaseResult:
    import cv2

    from parking.vision.pipeline import analyze_frame

    cam, slot_file, reference = setup
    frame = cv2.imread(image)
    times, _ = time_runs(
        lambda: analyze_frame(frame, cam, slot_file, None, reference=reference), runs, warmup
    )
    return _result(case, times)


def _result(case: Case, times: list[float], detections: int | None = None) -> CaseResult:
    return CaseResult(
        case=case,
        runs=len(times),
        median_ms=statistics.median(times),
        p95_ms=percentile(times, 95),
        peak_rss_mb=peak_rss_mb(),
        detections=detections,
    )


def run_case(case: Case, image: str, runs: int, warmup: int, extra: Any) -> CaseResult:
    """Run one case in this process. `extra` is the models folder (detector cases) or
    `(camera, slot_file, reference)` (appearance)."""
    if case.runtime == "appearance":
        return _run_appearance(case, image, runs, warmup, extra)
    return _run_detector(case, image, runs, warmup, extra)


def run_isolated(case: Case, image: str, runs: int, warmup: int, extra: Any) -> CaseResult:
    """`run_case` in a fresh spawned process; a failure becomes `CaseResult.error`."""
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        try:
            return pool.submit(run_case, case, image, runs, warmup, extra).result()
        except Exception as e:  # noqa: BLE001 - reported in the table, the other cases go on
            nan = float("nan")
            return CaseResult(case, 0, nan, nan, nan, error=f"{type(e).__name__}: {e}")


def cpu_temp() -> float | None:
    """CPU temperature in °C (`vcgencmd`, else the kernel's thermal zone), or None."""
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=5
        ).stdout
        return float(out.strip().removeprefix("temp=").rstrip("'C"))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def table(results: Sequence[CaseResult]) -> list[str]:
    """A Markdown table, one row per case (ready to paste into PROGRESS.md)."""
    rows = [
        "| Case | Runtime | imgsz | Median ms | p95 ms | Peak RSS MB | Vehicles |",
        "|------|---------|-------|-----------|--------|-------------|----------|",
    ]
    for r in results:
        c = r.case
        what = c.model or "appearance scorer"
        size = str(c.imgsz) if c.imgsz else "full frame"
        if r.error:
            rows.append(f"| {what} | {c.runtime} | {size} | error: {r.error} | | | |")
            continue
        found = "-" if r.detections is None else str(r.detections)
        rows.append(
            f"| {what} | {c.runtime} | {size} | {r.median_ms:.0f} | {r.p95_ms:.0f} "
            f"| {r.peak_rss_mb:.0f} | {found} |"
        )
    return rows


def to_json(results: Sequence[CaseResult], **meta: Any) -> dict[str, Any]:
    return {**meta, "results": [asdict(r) | {"name": r.case.name} for r in results]}
