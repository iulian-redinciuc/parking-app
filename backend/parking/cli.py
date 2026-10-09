"""The `parking` command line. Commands are listed in docs/design/architecture.md §6."""

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from parking import __version__

DEFAULT_CONFIG = Path("config/lot.yaml")

# Runtimes `models export` can produce; Hailo models are compiled separately (vision.md §11).
EXPORT_RUNTIMES = ("ncnn", "openvino", "onnx", "engine")

app = typer.Typer(help="Parking occupancy: vision, API and admin tools.", no_args_is_help=True)

models_app = typer.Typer(help="Download and export detector models.", no_args_is_help=True)
worker_app = typer.Typer(help="Run the camera workers.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations and maintenance.", no_args_is_help=True)
push_app = typer.Typer(help="Web Push setup.", no_args_is_help=True)
admin_app = typer.Typer(help="Admin helpers.", no_args_is_help=True)

app.add_typer(models_app, name="models")
app.add_typer(worker_app, name="worker")
app.add_typer(db_app, name="db")
app.add_typer(push_app, name="push")
app.add_typer(admin_app, name="admin")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"parking {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Parking occupancy: vision, API and admin tools."""


@models_app.command("export")
def models_export(
    model: Annotated[str, typer.Option(help="Ultralytics model name, e.g. yolo11n-seg.")] = (
        "yolo11n-seg"
    ),
    imgsz: Annotated[int, typer.Option(help="Input size the model is exported for.")] = 640,
    runtime: Annotated[
        str, typer.Option(help="ncnn | openvino | onnx | engine (see vision.md §11).")
    ] = "ncnn",
    out: Annotated[Path, typer.Option(help="Folder for the weights and the export.")] = Path(
        "models"
    ),
) -> None:
    """Download a COCO-pretrained YOLO model and export it for this machine's runtime."""
    if runtime not in EXPORT_RUNTIMES:
        raise typer.BadParameter(
            f"must be one of {', '.join(EXPORT_RUNTIMES)}", param_hint="--runtime"
        )
    from parking.vision.detector import export_model

    path = export_model(model, imgsz, runtime, out)
    typer.echo(f"exported {model} @ {imgsz} ({runtime}) -> {path}")


def _find_config(path: Path) -> Path:
    """`path`, or for the default path also `../config/lot.yaml` (running from `backend/`)."""
    if path.is_file() or path.is_absolute() or path != DEFAULT_CONFIG:
        return path
    up = Path("..") / path
    return up if up.is_file() else path


def _resolve(path: Path, root: Path) -> Path:
    """Paths in lot.yaml are relative to the repo/app root (the folder holding `config/`)."""
    if path.is_absolute() or path.exists():
        return path
    return root / path


def _fail(msg: str) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(1)


def _fake_detector_on(flag: bool) -> bool:
    return flag or os.environ.get("PARKING_FAKE_DETECTOR", "") not in ("", "0", "false")


def _camera(config: Path, camera: str):
    """Load lot.yaml and return `(lot, camera, root)`; exits with a message on any problem."""
    from parking.config import ConfigError, cli_env, load_config

    config = _find_config(config)
    root = config.resolve().parent.parent
    try:
        lot = load_config(config, cli_env(root))
    except (ConfigError, ValueError) as e:
        _fail(f"{config}: {e}")
    cams = {c.id: c for c in lot.cameras}
    if camera not in cams:
        _fail(f"camera '{camera}' is not in {config} (cameras: {', '.join(cams) or 'none'})")
    return lot, cams[camera], root


def _occupancy_camera(config: Path, camera: str, command: str):
    """`_camera`, but only for an occupancy camera."""
    lot, cam, root = _camera(config, camera)
    if cam.role != "occupancy":
        _fail(f"camera '{camera}' is a {cam.role} camera; {command} needs an occupancy camera")
    return lot, cam, root


def _slot_file(cam, root: Path):
    from parking.config import ConfigError, load_slots

    try:
        return load_slots(_resolve(cam.slots_file, root))
    except (ConfigError, ValueError) as e:
        _fail(f"slot file for '{cam.id}': {e} (draw the slots with tools/slot-editor)")


def _yolo_detector(det_cfg, root: Path):
    from parking.vision.detector import YoloDetector

    model = _resolve(Path(det_cfg.model), root)
    if not model.exists():
        _fail(f"model {model} not found; run `parking models export` first")
    return YoloDetector(str(model), det_cfg.imgsz, det_cfg.conf, det_cfg.classes, det_cfg.use_masks)


def _reference(occ, root: Path):
    """The empty-lot image for appearance scoring (`occupancy.appearance.reference_empty`)."""
    if occ.method != "appearance" or occ.appearance.reference_empty is None:
        return None
    import cv2

    path = _resolve(occ.appearance.reference_empty, root)
    ref = cv2.imread(str(path))
    if ref is None:
        _fail(f"can't read reference_empty image {path}")
    return ref


def _check_method(method: str | None) -> None:
    if method is not None and method not in ("detector", "appearance"):
        raise typer.BadParameter("must be detector or appearance", param_hint="--method")


@app.command()
def analyze(
    image: Annotated[Path, typer.Option(help="Image to analyse.")],
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ground.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    out: Annotated[Path, typer.Option(help="Folder for the JSON and the PNG.")] = Path(
        "out/analyze"
    ),
    threshold: Annotated[
        float | None, typer.Option(help="Override occupancy.threshold (0..1).")
    ] = None,
    mode: Annotated[
        str | None, typer.Option(help="Override occupancy.mode: mask | box_bottom.")
    ] = (None),
    imgsz: Annotated[int | None, typer.Option(help="Override detector.imgsz.")] = None,
    method: Annotated[
        str | None, typer.Option(help="Override occupancy.method: detector | appearance.")
    ] = None,
    fake_detector: Annotated[
        bool,
        typer.Option(
            "--fake-detector",
            help="Read detections from <image>.json instead of running the model "
            "(also env PARKING_FAKE_DETECTOR=1).",
        ),
    ] = False,
) -> None:
    """Analyse one image: write <out>/<image>.json (observation + totals) and <image>.png."""
    import cv2

    from parking.vision.annotate import annotate_occupancy
    from parking.vision.pipeline import analyze_frame

    if threshold is not None and not 0 < threshold < 1:
        raise typer.BadParameter("must be between 0 and 1", param_hint="--threshold")
    if mode is not None and mode not in ("mask", "box_bottom"):
        raise typer.BadParameter("must be mask or box_bottom", param_hint="--mode")
    if imgsz is not None and imgsz <= 0:
        raise typer.BadParameter("must be positive", param_hint="--imgsz")
    _check_method(method)

    lot, cam, root = _occupancy_camera(config, camera, "analyze")

    # CLI flags override the config for experiments
    overrides = {"threshold": threshold, "mode": mode, "method": method}
    occ = cam.occupancy.model_copy(update={k: v for k, v in overrides.items() if v is not None})
    det_cfg = cam.detector.model_copy(update={"imgsz": imgsz} if imgsz else {})
    cam = cam.model_copy(update={"occupancy": occ, "detector": det_cfg})

    slot_file = _slot_file(cam, root)
    capacities = {z: lot.zone_capacity(z, [slot_file]) for z in cam.zones}

    image = _resolve(image, root)
    frame = cv2.imread(str(image))
    if frame is None:
        _fail(f"can't read image {image}")

    detector = None
    if occ.method == "appearance":
        pass  # no model: vision.md §2.1
    elif _fake_detector_on(fake_detector):
        from parking.vision.detector import FakeDetector

        sidecar = image.with_suffix(".json")
        if not sidecar.is_file():
            _fail(f"fake detector: no sidecar {sidecar}")
        detector = FakeDetector(sidecar, conf=det_cfg.conf, classes=det_cfg.classes)
    else:
        detector = _yolo_detector(det_cfg, root)

    result = analyze_frame(frame, cam, slot_file, detector, capacities, _reference(occ, root))
    png = annotate_occupancy(
        frame,
        slot_file.slots,
        result.slots,
        result.detections,
        {lot.zone(z).name.get("en", z): t for z, t in result.banner_totals().items()},
        result.inference_ms,
        slot_file.image_size,
    )

    out.mkdir(parents=True, exist_ok=True)
    json_path, png_path = out / f"{image.stem}.json", out / f"{image.stem}.png"
    json_path.write_text(json.dumps(result.to_observation(), indent=2) + "\n")
    cv2.imwrite(str(png_path), png)

    secs = result.timings["total_ms"] / 1000
    summary = "; ".join(
        f"{z}: {t.free} free / {t.capacity} ({t.taken} taken)" for z, t in result.totals.items()
    )
    typer.echo(f"{summary or 'no zones'} in {secs:.1f} s -> {json_path}, {png_path}")


@app.command()
def evaluate(
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ground.")],
    images: Annotated[Path, typer.Option(help="Folder of labelled images, or one image.")] = Path(
        "data/samples"
    ),
    labels: Annotated[
        Path | None, typer.Option(help="Labels file (default data/labels/<camera>.json).")
    ] = None,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    sweep: Annotated[
        str | None, typer.Option(help="Threshold sweep start:stop:step, e.g. 0.1:0.6:0.05.")
    ] = None,
    mode: Annotated[
        str | None, typer.Option(help="mask | box_bottom | both (default: the config's).")
    ] = None,
    threshold: Annotated[
        float | None, typer.Option(help="Override occupancy.threshold (0..1).")
    ] = None,
    imgsz: Annotated[int | None, typer.Option(help="Override detector.imgsz.")] = None,
    method: Annotated[
        str | None, typer.Option(help="Override occupancy.method: detector | appearance.")
    ] = None,
    out: Annotated[Path, typer.Option(help="Folder for the report and mistake images.")] = Path(
        "out/eval"
    ),
    cache: Annotated[Path, typer.Option(help="Detection cache folder.")] = Path("out/cache"),
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Always rerun the detector.")
    ] = False,
    fake_detector: Annotated[
        bool,
        typer.Option(
            "--fake-detector",
            help="Read detections from <image>.json next to each image "
            "(also env PARKING_FAKE_DETECTOR=1).",
        ),
    ] = False,
) -> None:
    """Score labelled images: slot accuracy, free-precision/recall, count error, sweep."""
    from datetime import date

    import cv2

    from parking.config import ConfigError, load_labels
    from parking.vision import evaluate as ev
    from parking.vision.annotate import annotate_occupancy, highlight_slots
    from parking.vision.detector import FakeDetector

    if threshold is not None and not 0 < threshold < 1:
        raise typer.BadParameter("must be between 0 and 1", param_hint="--threshold")
    if mode is not None and mode not in ("mask", "box_bottom", "both"):
        raise typer.BadParameter("must be mask, box_bottom or both", param_hint="--mode")
    if imgsz is not None and imgsz <= 0:
        raise typer.BadParameter("must be positive", param_hint="--imgsz")
    try:
        thresholds = ev.parse_sweep(sweep) if sweep else []
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--sweep") from None
    _check_method(method)

    lot, cam, root = _occupancy_camera(config, camera, "evaluate")
    occ = cam.occupancy.model_copy(update={"method": method} if method else {})
    appearance = occ.method == "appearance"
    det_cfg = cam.detector.model_copy(update={"imgsz": imgsz} if imgsz else {})
    threshold = threshold if threshold is not None else occ.threshold
    if appearance:
        modes = ["appearance"]  # `mode` only applies to the detector
    else:
        modes = ["mask", "box_bottom"] if mode == "both" else [mode or occ.mode]
    slot_file = _slot_file(cam, root)

    labels_path = _resolve(labels or Path(f"data/labels/{camera}.json"), root)
    try:
        label_file = load_labels(labels_path)
        ev.check_labels(label_file, [s.id for s in slot_file.slots])
    except (ConfigError, ValueError) as e:
        _fail(f"labels {labels_path}: {e} (label the images with tools/slot-editor)")
    if label_file.camera_id != camera:
        _fail(f"labels {labels_path} are for camera '{label_file.camera_id}', not '{camera}'")

    folder = _resolve(images, root)
    single = folder.is_file()
    if single:
        found, folder = [folder], folder.parent
    elif folder.is_dir():
        found = ev.list_images(folder)
    else:
        _fail(f"image folder {folder} not found")
    paths = [p for p in found if p.name in label_file.images]
    skipped = [p.name for p in found if p.name not in label_file.images]
    missing = [] if single else sorted(set(label_file.images) - {p.name for p in found})
    if skipped:
        typer.echo(f"not labelled, skipped: {', '.join(skipped)}")
    if missing:
        typer.echo(f"labelled but not in {folder}: {', '.join(missing)}")
    if not paths:
        _fail(f"no labelled images in {folder}")

    fake = not appearance and _fake_detector_on(fake_detector)
    if appearance:
        try:
            frames = ev.appearance_frames(
                paths,
                lambda p: cv2.imread(str(p)),
                slot_file,
                occ.appearance,
                _reference(occ, root),
            )
        except ValueError as e:
            _fail(str(e))
        ran = len(frames)
    elif fake:
        frames = []
        for p in paths:
            frame = cv2.imread(str(p))
            sidecar = p.with_suffix(".json")
            if frame is None:
                _fail(f"can't read image {p}")
            if not sidecar.is_file():
                _fail(f"fake detector: no sidecar {sidecar}")
            dets = FakeDetector(sidecar, det_cfg.conf, det_cfg.classes).detections
            frames.append(ev.Frame(p.name, (frame.shape[1], frame.shape[0]), dets))
        ran = len(frames)
    else:
        settings = {
            "conf": det_cfg.conf,
            "classes": list(det_cfg.classes),
            "use_masks": det_cfg.use_masks,
        }
        try:
            frames, ran = ev.detect_frames(
                paths,
                lambda p: cv2.imread(str(p)),
                lambda: _yolo_detector(det_cfg, root),
                None if no_cache else cache,
                det_cfg.model,
                det_cfg.imgsz,
                settings,
            )
        except ValueError as e:
            _fail(str(e))
    if appearance:
        source = "appearance scoring"
        typer.echo(f"{len(frames)} image(s), {source} (no detector)\n")
    else:
        source = "fake detector" if fake else f"{Path(det_cfg.model).name} @ {det_cfg.imgsz}"
        typer.echo(
            f"{len(frames)} image(s), {source}, {ran} detected, {len(frames) - ran} from cache\n"
        )

    out.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "camera_id": camera,
        "date": date.today().isoformat(),
        "detector": source,
        "modes": {},
    }
    for m in modes:
        evals = ev.evaluate_frames(frames, slot_file, label_file, m, threshold)
        overall = ev.summarize(evals)
        typer.echo(f"== mode {m}, threshold {threshold:.2f} ==")
        typer.echo("\n".join(ev.image_table(evals)))
        typer.echo(ev.summary_line("overall", overall))
        conds = ev.by_condition(evals)
        for tag, s in conds.items():
            typer.echo(ev.summary_line(f"  {tag}", s))

        entry: dict = {
            "threshold": threshold,
            "overall": overall.to_dict(),
            "by_condition": {t: s.to_dict() for t, s in conds.items()},
            "images": [
                {
                    "image": e.image,
                    "conditions": list(e.conditions),
                    "labelled": e.labelled,
                    "correct": e.correct,
                    "true_free": e.true_free,
                    "pred_free": e.pred_free,
                    "said_free": list(e.said_free),
                    "said_taken": list(e.said_taken),
                }
                for e in evals
            ],
        }
        if thresholds:
            rows = ev.sweep(frames, slot_file, label_file, m, thresholds)
            best = ev.best_threshold(rows, threshold)
            typer.echo("\n".join(["", *ev.sweep_table(rows, best)]))
            bs = dict(rows)[best]
            typer.echo(
                f"best threshold ({m}): {best:.2f} (slot accuracy {ev.pct(bs.accuracy)}, "
                f"free-precision {ev.pct(bs.free_precision)})"
            )
            entry["sweep"] = [{"threshold": t, **s.to_dict()} for t, s in rows]
            entry["best_threshold"] = best

        # annotated images of the frames with mistakes
        by_name = {f.image: f for f in frames}
        for e in evals:
            if e.correct == e.labelled:
                continue
            f = by_name[e.image]
            pixels = cv2.imread(str(folder / e.image))
            results = ev.score_frame(f, slot_file, m, threshold)
            img = annotate_occupancy(
                pixels, slot_file.slots, results, f.detections, {}, None, slot_file.image_size
            )
            img = highlight_slots(img, slot_file.slots, e.notes(), slot_file.image_size)
            png = out / f"{camera}-{Path(e.image).stem}-{m}.png"
            cv2.imwrite(str(png), img)
        report["modes"][m] = entry
        typer.echo("")

    json_path = out / f"{camera}-{report['date']}.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    typer.echo(f"report -> {json_path}; images with mistakes -> {out}/{camera}-*.png")


@app.command("bootstrap-slots")
def bootstrap_slots(
    image: Annotated[Path, typer.Option(help="Image of the lot when it's busy.")],
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ground.")],
    out: Annotated[
        Path | None, typer.Option(help="Slot file to write (default: the camera's slots_file).")
    ] = None,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
    footprint: Annotated[
        str, typer.Option(help="box_bottom (angled view) | box (seen from above).")
    ] = "box_bottom",
    imgsz: Annotated[int, typer.Option(help="Detector input size.")] = 1280,
    conf: Annotated[float, typer.Option(help="Detector confidence threshold.")] = 0.25,
    fake_detector: Annotated[
        bool,
        typer.Option(
            "--fake-detector",
            help="Read detections from <image>.json instead of running the model "
            "(also env PARKING_FAKE_DETECTOR=1).",
        ),
    ] = False,
) -> None:
    """Draft a slot file from detected vehicles (vision.md §4); fix it in tools/slot-editor."""
    import cv2

    from parking.config import SlotFile
    from parking.vision.bootstrap import format_json, propose_slots, slot_file
    from parking.vision.detector import FakeDetector

    if footprint not in ("box_bottom", "box"):
        raise typer.BadParameter("must be box_bottom or box", param_hint="--footprint")
    if imgsz <= 0:
        raise typer.BadParameter("must be positive", param_hint="--imgsz")
    if not 0 < conf < 1:
        raise typer.BadParameter("must be between 0 and 1", param_hint="--conf")

    _, cam, root = _occupancy_camera(config, camera, "bootstrap-slots")
    target = out or _resolve(cam.slots_file, root)
    if target.exists() and not force:
        _fail(f"{target} exists; pass --force to overwrite it")

    path = _resolve(image, root)
    frame = cv2.imread(str(path))
    if frame is None:
        _fail(f"can't read image {path}")

    # vision.md §4: masks on, 1280 px, a low confidence to find as many vehicles as possible
    det_cfg = cam.detector.model_copy(update={"imgsz": imgsz, "conf": conf, "use_masks": True})
    if _fake_detector_on(fake_detector):
        sidecar = path.with_suffix(".json")
        if not sidecar.is_file():
            _fail(f"fake detector: no sidecar {sidecar}")
        detector = FakeDetector(sidecar, conf=det_cfg.conf, classes=det_cfg.classes)
    else:
        detector = _yolo_detector(det_cfg, root)

    zone = cam.zones[0]
    slots = propose_slots(detector.detect(frame), zone, footprint)
    if not slots:
        _fail(f"no vehicles found in {path}; draw the slots by hand in tools/slot-editor")

    try:
        reference = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        reference = str(path)
    data = slot_file(camera, (frame.shape[1], frame.shape[0]), slots, reference)
    SlotFile.model_validate(data)  # what load_slots() checks
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_json(data))
    typer.echo(
        f"{len(slots)} slot(s) in zone '{zone}' -> {target}\n"
        "Now fix it in tools/slot-editor: add the empty spaces and adjust the corners."
    )


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@app.command()
def benchmark(
    image: Annotated[Path, typer.Option(help="Image to run every case on.")],
    runs: Annotated[int, typer.Option(help="Timed runs per case.")] = 20,
    warmup: Annotated[int, typer.Option(help="Untimed runs before timing.")] = 3,
    runtimes: Annotated[str, typer.Option(help="Comma list: pytorch,ncnn ('' for none).")] = (
        "pytorch,ncnn"
    ),
    imgsz: Annotated[str, typer.Option(help="Comma list of input sizes.")] = "640,1280",
    models: Annotated[str, typer.Option(help="Comma list of model names.")] = (
        "yolo11n,yolo11n-seg"
    ),
    camera: Annotated[
        str, typer.Option(help="Camera whose slots time the appearance scorer ('' to skip).")
    ] = "cam-ground",
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    models_dir: Annotated[Path, typer.Option(help="Folder with the .pt weights.")] = Path("models"),
    out: Annotated[Path, typer.Option(help="Folder for the JSON report.")] = Path("out/benchmark"),
) -> None:
    """Time each runtime × imgsz × model (median, p95, peak RSS) and the appearance scorer.

    Each case runs in a fresh process. NCNN models are exported per size into
    <models-dir>/bench/ on first use. Prints a Markdown table for PROGRESS.md → Metrics.
    """
    import platform
    from datetime import datetime

    from parking.vision import benchmark as bm

    if runs < 1 or warmup < 0:
        raise typer.BadParameter(
            "runs must be at least 1 and warmup at least 0", param_hint="--runs"
        )
    bad = [r for r in _csv(runtimes) if r not in bm.RUNTIMES]
    if bad:
        raise typer.BadParameter(
            f"unknown {bad}; use {', '.join(bm.RUNTIMES)}", param_hint="--runtimes"
        )
    try:
        sizes = [int(s) for s in _csv(imgsz)]
    except ValueError:
        raise typer.BadParameter("must be integers, e.g. 640,1280", param_hint="--imgsz") from None

    root = _find_config(config).resolve().parent.parent
    image = _resolve(image, root)
    if not image.is_file():
        _fail(f"can't read image {image}")
    models_dir = _resolve(models_dir, root)
    cases = bm.cases(_csv(runtimes), sizes, _csv(models))
    jobs: list[tuple[bm.Case, object]] = [(c, str(models_dir)) for c in cases]
    if camera:
        _, cam, root = _occupancy_camera(config, camera, "benchmark")
        occ = cam.occupancy.model_copy(update={"method": "appearance"})
        cam = cam.model_copy(update={"occupancy": occ})
        jobs.append((bm.Case("appearance"), (cam, _slot_file(cam, root), _reference(occ, root))))
    if not jobs:
        _fail("nothing to benchmark")

    temp_before = bm.cpu_temp()
    results = []
    for case, extra in jobs:
        typer.echo(f"{case.name} ...", err=True)
        results.append(bm.run_isolated(case, str(image), runs, warmup, extra))
    temp_after = bm.cpu_temp()

    for line in bm.table(results):
        typer.echo(line)
    temps = " -> ".join("?" if t is None else f"{t:.1f} °C" for t in (temp_before, temp_after))
    typer.echo(
        f"\n{platform.node()} ({platform.machine()}), {runs} runs after {warmup} warm-up, "
        f"CPU temperature {temps}"
    )
    if max((t for t in (temp_before, temp_after) if t is not None), default=0) > 80:
        typer.echo("warning: CPU over 80 °C; the Pi needs its active cooler", err=True)

    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    report = bm.to_json(
        results,
        date=stamp,
        machine=platform.node(),
        arch=platform.machine(),
        image=image.name,
        runs=runs,
        warmup=warmup,
        cpu_temp_before=temp_before,
        cpu_temp_after=temp_after,
    )
    json_path = out / f"benchmark-{stamp}.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    typer.echo(f"report -> {json_path}")


@app.command()
def grab(
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ground.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    source: Annotated[
        str | None, typer.Option(help="Source URI to use instead of the camera's `source`.")
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Image to write (default data/reference/<camera>.jpg).")
    ] = None,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for a frame.")] = 20.0,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Save one full-resolution frame from a camera, e.g. the slot reference image (P4.5)."""
    import time

    import cv2

    from parking.vision.sources import make_source

    if timeout <= 0:
        raise typer.BadParameter("must be positive", param_hint="--timeout")
    _, cam, root = _camera(config, camera)
    target = out or root / "data" / "reference" / f"{camera}.jpg"
    if target.exists() and not force:
        _fail(f"{target} exists; pass --force to overwrite it")
    try:
        src = make_source(source or cam.source, root)
    except (ValueError, NotImplementedError) as e:
        _fail(str(e))  # make_source never echoes the URI (it may hold credentials)

    deadline = time.monotonic() + timeout
    frame = None
    try:
        while frame is None and time.monotonic() < deadline:
            frame = src.read()
            if frame is None:
                time.sleep(0.2)  # rtsp: the reader thread is connecting; snapshot: backoff
    finally:
        src.close()
    if frame is None:
        _fail(f"no frame from '{camera}' within {timeout:g} s")

    target.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(target), frame.image, [cv2.IMWRITE_JPEG_QUALITY, 95]):
        _fail(f"can't write {target}")
    h, w = frame.image.shape[:2]
    typer.echo(f"{w}x{h} frame from '{camera}' at {frame.ts.isoformat()} -> {target}")


@app.command("health-stats")
def health_stats(
    logs: Annotated[
        list[Path], typer.Argument(help="Worker log files with LOG_LEVEL=DEBUG ('-' = stdin).")
    ],
    camera: Annotated[str, typer.Option(help="Occupancy camera id in the config.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    since: Annotated[
        str | None, typer.Option(help="Ignore frames before this ISO time (e.g. a lens test).")
    ] = None,
    until: Annotated[str | None, typer.Option(help="Ignore frames after this ISO time.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """Summarise logged frame-health metrics per lot-local hour and suggest thresholds (P4.4)."""
    import sys
    from datetime import UTC, datetime

    from parking.vision.health_stats import parse_lines, report, suggest, summarize

    lot, cam, _ = _occupancy_camera(config, camera, "health-stats")

    def when(value: str | None, name: str) -> datetime | None:
        if value is None:
            return None
        try:
            t = datetime.fromisoformat(value)
        except ValueError:
            raise typer.BadParameter("not an ISO time", param_hint=name) from None
        return t if t.tzinfo else t.replace(tzinfo=UTC)

    lo, hi = when(since, "--since"), when(until, "--until")

    def lines():
        for path in logs:
            if str(path) == "-":
                yield from sys.stdin
                continue
            if not path.is_file():
                _fail(f"{path}: no such file")
            with path.open(errors="replace") as f:
                yield from f

    records = [
        r
        for r in parse_lines(lines())
        if r.camera == camera and (lo is None or r.ts >= lo) and (hi is None or r.ts <= hi)
    ]
    if not records:
        _fail(
            f"no health_metrics lines for camera '{camera}' (run the worker with LOG_LEVEL=DEBUG)"
        )
    hours, total = summarize(records, lot.lot.timezone)
    if as_json:
        out = {
            "camera": camera,
            "timezone": lot.lot.timezone,
            "hours": {f"{h:02d}": g.as_dict() for h, g in hours.items()},
            "all": total.as_dict(),
            "suggested": suggest(total, cam.health),
            "current": cam.health.model_dump(
                include={"black_mean_max", "blur_laplacian_min", "frozen_diff_max"}
            ),
        }
        typer.echo(json.dumps(out, indent=2))
    else:
        typer.echo(report(hours, total, cam.health))


@worker_app.command("occupancy")
def worker_occupancy(
    camera: Annotated[str, typer.Option(help="Occupancy camera id in the config.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    print_mode: Annotated[
        bool,
        typer.Option(
            "--print", help="Print observations and health as JSON lines instead of sending."
        ),
    ] = False,
    fake_detector: Annotated[
        bool,
        typer.Option(
            "--fake-detector",
            help="Read detections from <image>.json next to each replayed image "
            "(also env PARKING_FAKE_DETECTOR=1).",
        ),
    ] = False,
    control_port: Annotated[
        int, typer.Option(help="Port of the /control/* server (0 = off).")
    ] = 9000,
    control_host: Annotated[str, typer.Option(help="Address the control server binds.")] = (
        "0.0.0.0"
    ),
    max_frames: Annotated[int, typer.Option(help="Stop after this many frames (0 = never).")] = 0,
) -> None:
    """Run the occupancy worker: analyse a frame every interval and send observations."""
    import logging

    from parking.workers.base import WorkerError, load_settings
    from parking.workers.occupancy_worker import OccupancyWorker

    if not 0 <= control_port <= 65535:
        raise typer.BadParameter("must be 0..65535", param_hint="--control-port")
    if max_frames < 0:
        raise typer.BadParameter("must be >= 0", param_hint="--max-frames")

    config = _find_config(config)
    settings = load_settings(config.resolve().parent.parent)
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        worker = OccupancyWorker(
            config,
            camera,
            fake_detector=_fake_detector_on(fake_detector),
            print_mode=print_mode,
            settings=settings,
            control_port=control_port or None,
            control_host=control_host,
        )
        worker.run(max_frames or None)
    except WorkerError as e:
        _fail(str(e))


@app.command("api")
def api(
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    host: Annotated[str, typer.Option(help="Address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    reload: Annotated[
        bool, typer.Option("--reload", help="Restart when the backend code changes (dev only).")
    ] = False,
) -> None:
    """Run the API (uvicorn): ingest from the workers, live status for the app."""
    import os

    import uvicorn

    import parking

    config = _find_config(config)
    if not config.is_file():
        _fail(f"config not found: {config}")
    # the factory (also in uvicorn's reload subprocess) reads the config path from here
    os.environ["PARKING_CONFIG"] = str(config.resolve())
    uvicorn.run(
        "parking.api.app:create_app_from_env",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[str(Path(parking.__file__).parent)] if reload else None,
        log_config=None,
    )


@db_app.command("upgrade")
def db_upgrade(
    url: Annotated[
        str | None,
        typer.Option(help="Database URL. Default: PARKING_DB_URL, else data/db/parking.sqlite."),
    ] = None,
    revision: Annotated[str, typer.Option(help="Alembic revision to upgrade to.")] = "head",
) -> None:
    """Apply the database migrations (creates the SQLite file if needed)."""
    from parking.db.engine import default_url, upgrade

    root = _find_config(DEFAULT_CONFIG).parent.parent
    url = url or default_url(root)
    try:
        upgrade(url, revision)
    except Exception as e:  # alembic/sqlalchemy errors: show the message, not a traceback
        _fail(f"upgrade failed: {e}")
    typer.echo(f"database at {revision}: {url}")


DbUrl = Annotated[
    str | None,
    typer.Option(help="Database URL. Default: PARKING_DB_URL, else data/db/parking.sqlite."),
]


def _db_engine(config: Path, url: str | None):
    """`(lot config, engine)` for the maintenance commands; the DB is upgraded first."""
    from parking.config import ConfigError, cli_env, load_config
    from parking.db.engine import default_url, make_engine, upgrade

    config = _find_config(config)
    root = config.resolve().parent.parent
    try:
        lot = load_config(config, cli_env(root))
    except (ConfigError, ValueError) as e:
        _fail(f"{config}: {e}")
    url = url or default_url(root)
    try:
        upgrade(url)
    except Exception as e:
        _fail(f"upgrade failed: {e}")
    return lot, make_engine(url)


@db_app.command("aggregate")
def db_aggregate(
    backfill: Annotated[
        bool,
        typer.Option(
            "--backfill",
            help="Delete zone_minute/zone_hour and rebuild them from all of zone_state.",
        ),
    ] = False,
    config: Annotated[Path, typer.Option(help="lot.yaml (for the zone ids).")] = DEFAULT_CONFIG,
    url: DbUrl = None,
) -> None:
    """Build the zone_minute / zone_hour rollups now (the API also does it every minute)."""
    from parking.api.jobs import MaintenanceJobs
    from parking.core.clock import SystemClock
    from parking.db import rollups
    from parking.db.engine import session_scope

    lot, engine = _db_engine(config, url)
    now = SystemClock().now()
    try:
        if backfill:
            with session_scope(engine) as session:
                minutes, hours = rollups.backfill(session, [z.id for z in lot.zones], now)
        else:
            jobs = MaintenanceJobs(engine, lot, SystemClock())
            minutes, hours = jobs.run_minutes(now), jobs.run_hours(now)
    finally:
        engine.dispose()
    typer.echo(f"zone_minute: {minutes} row(s) written, zone_hour: {hours} row(s) written")


@db_app.command("prune")
def db_prune(
    raw_days: Annotated[
        float, typer.Option(min=0, help="Keep slot_state, zone_state, flow_event this long.")
    ] = 90,
    minute_days: Annotated[float, typer.Option(min=0, help="Keep zone_minute this long.")] = 30,
    log_days: Annotated[float, typer.Option(min=0, help="Keep notification_log this long.")] = 30,
    vacuum: Annotated[bool, typer.Option("--vacuum", help="Then VACUUM the file.")] = False,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    url: DbUrl = None,
) -> None:
    """Delete rows past the retention periods (the API also does it daily at 04:00)."""
    from datetime import timedelta

    from parking.api.jobs import MaintenanceJobs
    from parking.api.jobs import vacuum as vacuum_db
    from parking.core.clock import SystemClock
    from parking.db.rollups import Retention

    lot, engine = _db_engine(config, url)
    retention = Retention(
        raw=timedelta(days=raw_days),
        minute=timedelta(days=minute_days),
        log=timedelta(days=log_days),
    )
    try:
        deleted = MaintenanceJobs(engine, lot, SystemClock(), retention).run_prune(
            SystemClock().now()
        )
        if vacuum:
            vacuum_db(engine)
    finally:
        engine.dispose()
    for table, count in deleted.items():
        typer.echo(f"{table}: {count} row(s) deleted")


@push_app.command("vapid-keys")
def push_vapid_keys() -> None:
    """Generate a VAPID key pair and print it as `.env` lines (notifications.md §2)."""
    from parking.push.sender import generate_vapid_keys

    public, private = generate_vapid_keys()
    typer.echo(f"VAPID_PUBLIC_KEY={public}")
    typer.echo(f"VAPID_PRIVATE_KEY={private}")
    typer.echo(
        "Put both in deploy/.env (never commit them) and back them up: losing or rotating "
        "them breaks every push subscription.",
        err=True,
    )


ADMIN_PASSWORD_MIN = 10


@admin_app.command("hash-password")
def admin_hash_password() -> None:
    """Ask for the admin password twice and print its argon2 hash as a `.env` line."""
    from parking.api.routes.admin import hash_password

    password = typer.prompt("Admin password", hide_input=True, confirmation_prompt=True)
    if len(password) < ADMIN_PASSWORD_MIN:
        _fail(f"use at least {ADMIN_PASSWORD_MIN} characters")
    # single quotes: Compose and python-dotenv then keep the `$`s of the hash as they are
    typer.echo(f"ADMIN_PASSWORD_HASH='{hash_password(password)}'")
    typer.echo("Put it in deploy/.env (never commit it) and restart the API.", err=True)
