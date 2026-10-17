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
validation_app = typer.Typer(help="Build the validation set (P4.8).", no_args_is_help=True)
forecast_app = typer.Typer(help="The forecast model (P9.6).", no_args_is_help=True)

app.add_typer(models_app, name="models")
app.add_typer(worker_app, name="worker")
app.add_typer(db_app, name="db")
app.add_typer(push_app, name="push")
app.add_typer(admin_app, name="admin")
app.add_typer(validation_app, name="validation")
app.add_typer(forecast_app, name="forecast")


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


def _worker_logging(level: str) -> None:
    import logging

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=level.upper(), format=fmt)
    # httpx logs every request at INFO: one line per observation / health message sent to the API
    # (every few seconds), and a camera URL may hold credentials in its query
    logging.getLogger("httpx").setLevel(logging.WARNING)


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


def _resolve_out(path: Path, root: Path) -> Path:
    """An output path: like `_resolve`, but going by its top folder (the file isn't there yet)."""
    if path.is_absolute() or Path(path.parts[0]).exists():
        return path
    return root / path


def _fail(msg: str) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(1)


def _fake_detector_on(flag: bool) -> bool:
    return flag or os.environ.get("PARKING_FAKE_DETECTOR", "") not in ("", "0", "false")


def _camera(config: Path, camera: str):
    """Load lot.yaml and return `(lot, camera, root)`; exits with a message on any problem."""
    from parking.config import ConfigError, cli_env, load_lots, lot_of_camera

    config = _find_config(config)
    root = config.resolve().parent.parent
    try:
        lots = load_lots(config, cli_env(root))
    except (ConfigError, ValueError) as e:
        _fail(f"{config}: {e}")
    cams = {c.id: c for lot in lots for c in lot.cameras}
    if camera not in cams:
        _fail(f"camera '{camera}' is not in {config} (cameras: {', '.join(cams) or 'none'})")
    return lot_of_camera(lots, camera), cams[camera], root


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
    """The camera's detector: `detector.type` picks YOLO (Ultralytics) or YOLOX."""
    from parking.vision.detector import build_detector

    model = _resolve(Path(det_cfg.model), root)
    if not model.exists():
        _fail(f"model {model} not found; run `parking models export` first")
    try:
        return build_detector(det_cfg, model)
    except ValueError as e:
        _fail(f"detector {model}: {e}")


def _reference(occ, root: Path):
    """The empty-lot image for appearance scoring (`occupancy.appearance.reference_empty`)."""
    if not occ.uses_appearance or occ.appearance.reference_empty is None:
        return None
    import cv2

    path = _resolve(occ.appearance.reference_empty, root)
    ref = cv2.imread(str(path))
    if ref is None:
        _fail(f"can't read reference_empty image {path}")
    return ref


METHOD_HELP = "Override occupancy.method: detector | appearance | classifier | ensemble."


def _check_method(method: str | None) -> None:
    from parking.config import OCCUPANCY_METHODS

    if method is not None and method not in OCCUPANCY_METHODS:
        raise typer.BadParameter(
            f"must be one of {', '.join(OCCUPANCY_METHODS)}", param_hint="--method"
        )


def _with_classifier(occ, model: Path | None):
    if model is None:
        return occ
    clf = occ.classifier.model_copy(update={"model": model.resolve()})
    return occ.model_copy(update={"classifier": clf})


def _classifier(occ, root: Path):
    """The slot classifier (vision.md §9) for `classifier` / `ensemble`, else None."""
    if not occ.uses_classifier:
        return None
    from parking.vision.slot_classifier import load_classifier

    try:
        return load_classifier(_resolve(occ.classifier.model, root))
    except ImportError:
        _fail("the slot classifier needs onnxruntime: uv sync --extra vision")
    except Exception as e:
        _fail(str(e))


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
    method: Annotated[str | None, typer.Option(help=METHOD_HELP)] = None,
    classifier: Annotated[
        Path | None, typer.Option(help="Override occupancy.classifier.model (an .onnx file).")
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
    occ = _with_classifier(occ, classifier)
    det_cfg = cam.detector.model_copy(update={"imgsz": imgsz} if imgsz else {})
    cam = cam.model_copy(update={"occupancy": occ, "detector": det_cfg})

    slot_file = _slot_file(cam, root)
    capacities = {z: lot.zone_capacity(z, [slot_file]) for z in cam.zones}

    image = _resolve(image, root)
    frame = cv2.imread(str(image))
    if frame is None:
        _fail(f"can't read image {image}")

    detector = None
    if not occ.uses_detector:
        pass  # appearance (vision.md §2.1) / slot classifier (§9)
    elif _fake_detector_on(fake_detector):
        from parking.vision.detector import FakeDetector

        sidecar = image.with_suffix(".json")
        if not sidecar.is_file():
            _fail(f"fake detector: no sidecar {sidecar}")
        detector = FakeDetector(sidecar, conf=det_cfg.conf, classes=det_cfg.classes)
    else:
        detector = _yolo_detector(det_cfg, root)

    result = analyze_frame(
        frame, cam, slot_file, detector, capacities, _reference(occ, root), _classifier(occ, root)
    )
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
    method: Annotated[str | None, typer.Option(help=METHOD_HELP)] = None,
    classifier: Annotated[
        Path | None, typer.Option(help="Override occupancy.classifier.model (an .onnx file).")
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
    occ = _with_classifier(occ, classifier)
    appearance = not occ.uses_detector  # appearance, classifier or ensemble: scores, no detector
    det_cfg = cam.detector.model_copy(update={"imgsz": imgsz} if imgsz else {})
    threshold = threshold if threshold is not None else occ.decision_threshold
    if appearance:
        modes = [occ.method]  # `mode` only applies to the detector
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
        read = lambda p: cv2.imread(str(p))  # noqa: E731
        try:
            if occ.uses_classifier:
                clf = _classifier(occ, root)
                ref = _reference(occ, root)
                frames = ev.classifier_frames(paths, read, slot_file, occ, clf, ref)
            else:
                frames = ev.appearance_frames(
                    paths, read, slot_file, occ.appearance, _reference(occ, root)
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
        source = {
            "appearance": "appearance scoring",
            "classifier": f"slot classifier {Path(occ.classifier.model).name}",
            "ensemble": f"slot classifier {Path(occ.classifier.model).name} + appearance",
        }[occ.method]
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
        typer.echo(ev.target_line(overall))
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
            typer.echo(f"at {best:.2f}: {ev.target_line(bs)}")
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


@app.command("simulate-feed")
def simulate_feed(
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ground.")],
    base: Annotated[Path, typer.Option(help="The photo to make the frames from.")] = Path(
        "data/samples/ground-01.jpg"
    ),
    labels: Annotated[
        Path | None,
        typer.Option(
            help="Labels file holding the base photo (default data/labels/<camera>.json)."
        ),
    ] = None,
    frames: Annotated[int, typer.Option(help="Number of frames to write.")] = 200,
    seed: Annotated[int, typer.Option(help="Same seed, same frames.")] = 1,
    out: Annotated[
        Path | None, typer.Option(help="Folder for the frames (default data/replay/<zone>-sim).")
    ] = None,
    labels_out: Annotated[
        Path | None,
        typer.Option(help="Labels file to write (default data/labels/<camera>-sim.json)."),
    ] = None,
    day: Annotated[
        bool, typer.Option("--day", help="Follow a day: fills up, stays busy, empties.")
    ] = False,
    shift: Annotated[
        int, typer.Option(help="Whole-frame shift per frame, up to this many px.")
    ] = 2,
    noise: Annotated[float, typer.Option(help="Sensor noise (sigma in grey levels).")] = 2.0,
    empty_reference: Annotated[
        Path | None,
        typer.Option(
            help="Where the photo with every car removed goes, for `reference_empty` "
            "(default data/reference/<camera>-empty.jpg)."
        ),
    ] = None,
    quality: Annotated[int, typer.Option(help="JPEG quality of the frames.")] = 90,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
) -> None:
    """Make a simulated camera feed from one labelled photo: cars arriving and leaving (P2.12)."""
    import json

    import cv2

    from parking.config import ConfigError, load_labels
    from parking.vision import simulate as sim

    if frames <= 0:
        raise typer.BadParameter("must be positive", param_hint="--frames")
    if not 0 <= shift <= 3:
        raise typer.BadParameter("must be between 0 and 3", param_hint="--shift")
    if noise < 0:
        raise typer.BadParameter("must not be negative", param_hint="--noise")
    if not 1 <= quality <= 100:
        raise typer.BadParameter("must be between 1 and 100", param_hint="--quality")
    _, cam, root = _occupancy_camera(config, camera, "simulate-feed")
    slot_file = _slot_file(cam, root)

    base = _resolve(base, root)
    image = cv2.imread(str(base))
    if image is None:
        _fail(f"can't read image {base}")
    labels_path = _resolve(labels or Path(f"data/labels/{camera}.json"), root)
    try:
        label_file = load_labels(labels_path)
    except (ConfigError, ValueError) as e:
        _fail(f"labels {labels_path}: {e} (label the image with tools/slot-editor)")
    if base.name not in label_file.images:
        _fail(f"{base.name} isn't labelled in {labels_path}")
    label = label_file.images[base.name]
    try:
        simulator = sim.Simulator(image, slot_file, label.taken, label.unsure, seed=seed)
    except ValueError as e:
        _fail(str(e))

    target = _resolve_out(out or Path(f"data/replay/{cam.zones[0]}-sim"), root)
    labels_target = _resolve_out(labels_out or Path(f"data/labels/{camera}-sim.json"), root)
    empty_target = _resolve_out(empty_reference or Path(f"data/reference/{camera}-empty.jpg"), root)
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("frame-*.jpg"):  # a shorter run must not leave the last one's frames
        old.unlink()
    images: dict = {}
    counts = []
    for i, state in enumerate(sim.sequence(simulator, frames, seed, day)):
        frame = sim.vary(simulator.render(state), i, seed, noise=noise, shift=shift)
        name = sim.frame_name(i)
        if not cv2.imwrite(str(target / name), frame, [cv2.IMWRITE_JPEG_QUALITY, quality]):
            _fail(f"can't write {target / name}")
        images[name] = {
            "conditions": ["simulated"],
            "taken": sorted(state),
            "unsure": sorted(label.unsure),
        }
        counts.append(len(state))
    labels_target.parent.mkdir(parents=True, exist_ok=True)
    labels_target.write_text(
        json.dumps({"version": 1, "camera_id": camera, "images": images}, indent=1) + "\n"
    )
    empty_target.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(empty_target), simulator.render({}), [cv2.IMWRITE_JPEG_QUALITY, 95]):
        _fail(f"can't write {empty_target}")
    changes = sum(a != b for a, b in zip(counts, counts[1:], strict=False))
    typer.echo(
        f"{frames} frame(s) from {base.name} -> {target} (taken {min(counts)}..{max(counts)} "
        f"of {len(simulator.slot_ids)}, the count changes {changes} times), "
        f"labels -> {labels_target}, empty lot -> {empty_target}"
    )


@app.command("build-replay")
def build_replay(
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ground.")],
    sim: Annotated[
        Path | None,
        typer.Option(help="Simulated frames to include (default data/replay/<zone>-sim)."),
    ] = None,
    sim_labels: Annotated[
        Path | None, typer.Option(help="Their labels (default data/labels/<camera>-sim.json).")
    ] = None,
    photos: Annotated[Path, typer.Option(help="Folder of labelled real photos.")] = Path(
        "data/samples"
    ),
    photo_labels: Annotated[
        Path | None, typer.Option(help="Their labels (default data/labels/<camera>.json).")
    ] = None,
    generated: Annotated[Path, typer.Option(help="Folder of generated images.")] = Path(
        "samples/generated"
    ),
    generated_labels: Annotated[
        Path | None, typer.Option(help="Their labels (default <generated>/labels.json).")
    ] = None,
    reference: Annotated[
        Path | None,
        typer.Option(help="The view the slots were drawn on (default: the slot file's)."),
    ] = None,
    hold: Annotated[int, typer.Option(help="Frames each still image stays in the feed.")] = 6,
    out: Annotated[
        Path | None, typer.Option(help="Folder for the feed (default data/replay/<zone>-feed).")
    ] = None,
    labels_out: Annotated[
        Path | None,
        typer.Option(help="Labels file to write (default data/labels/<camera>-feed.json)."),
    ] = None,
    review: Annotated[
        Path | None,
        typer.Option(help="Also write every still with the slots drawn on it here (to label)."),
    ] = None,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
) -> None:
    """Build the dev replay feed from every image source: simulated frames, labelled real
    photos and generated images that show the reference view (P2.13)."""
    import json

    import cv2

    from parking.config import ConfigError, load_labels
    from parking.vision import replay as rp
    from parking.vision.evaluate import list_images
    from parking.vision.shift import ShiftDetector

    if hold <= 0:
        raise typer.BadParameter("must be positive", param_hint="--hold")
    _, cam, root = _occupancy_camera(config, camera, "build-replay")
    slot_file = _slot_file(cam, root)
    slot_ids = {s.id for s in slot_file.slots}

    def labels_of(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            file = load_labels(path)
        except (ConfigError, ValueError) as e:
            _fail(f"labels {path}: {e}")
        if file.camera_id != camera:
            _fail(f"labels {path} are for camera '{file.camera_id}', not '{camera}'")
        for name, label in file.images.items():
            unknown = sorted(set(label.taken + label.unsure) - slot_ids)
            if unknown:
                _fail(f"labels {path}: {name} names unknown slot(s) {', '.join(unknown)}")
        return file.images

    target = _resolve_out(out or Path(f"data/replay/{cam.zones[0]}-feed"), root)
    labels_target = _resolve_out(labels_out or Path(f"data/labels/{camera}-feed.json"), root)
    target.mkdir(parents=True, exist_ok=True)
    rp.clear(target, ("a-sim-", "b-photo-", "c-gen-"))
    feed: dict = {}

    def add(name: str, label) -> None:
        feed[name] = label.model_dump()

    # a: the simulated frames, as they are
    sim_dir = _resolve(sim or Path(f"data/replay/{cam.zones[0]}-sim"), root)
    sim_images = labels_of(_resolve(sim_labels or Path(f"data/labels/{camera}-sim.json"), root))
    n_sim = 0
    for path in list_images(sim_dir) if sim_dir.is_dir() else []:
        if path.name in sim_images:
            rp.link(path, target / f"a-sim-{path.name}")
            add(f"a-sim-{path.name}", sim_images[path.name])
            n_sim += 1
    if not n_sim:
        typer.echo(f"no labelled simulated frames in {sim_dir} (parking simulate-feed makes them)")

    # b, c: still images, only those that show the reference view
    stills = []
    for kind, folder, labels_path in (
        ("photo", _resolve(photos, root), photo_labels or Path(f"data/labels/{camera}.json")),
        ("gen", _resolve(generated, root), generated_labels or generated / "labels.json"),
    ):
        found = list_images(folder) if folder.is_dir() else []
        stills.append((kind, found, labels_of(_resolve(labels_path, root)), labels_path))
    detector = None
    if any(found for _, found, _, _ in stills):
        ref_path = reference or (
            Path(slot_file.reference_image) if slot_file.reference_image else None
        )
        ref = cv2.imread(str(_resolve(ref_path, root))) if ref_path else None
        if ref is None:
            _fail(
                f"can't read the reference image ({ref_path or 'none in the slot file'}): "
                "the still images are checked against it (--reference)"
            )
        detector = ShiftDetector(ref, slot_file, cam.health)
    review_dir = _resolve_out(review, root) if review else None
    if review_dir:
        review_dir.mkdir(parents=True, exist_ok=True)
    used = {"photo": 0, "gen": 0}
    left_out = 0
    for kind, found, images, labels_path in stills:
        checks = {p.name: rp.check_view(cv2.imread(str(p)), detector) for p in found}
        for p in found:
            c = checks[p.name]
            if review_dir and c.image is not None:
                cv2.imwrite(str(review_dir / f"{p.stem}.jpg"), rp.overlay(c.image, slot_file))
            if c.usable and p.name not in images:
                typer.echo(
                    f"  {p.name}: not in the feed, not labelled in {labels_path} ({c.reason})"
                )
            elif not c.usable:
                typer.echo(f"  {p.name}: not usable with this slot file, {c.reason}")
            else:
                typer.echo(f"  {p.name}: {c.status}, {c.reason}")
        ready = [n for n, c in checks.items() if c.usable and n in images]
        left_out += len(found) - len(ready)
        order = rp.up_and_down(ready, images) if kind == "gen" else sorted(ready)
        prefix = "b-photo" if kind == "photo" else "c-gen"
        for i, name in enumerate(order, 1):
            stem = f"{prefix}-{i:02}-{Path(name).stem}"
            try:
                frames = rp.hold(checks[name].image, target, stem, hold)
            except OSError as e:
                _fail(str(e))
            for frame in frames:
                add(frame, images[name])
            used[kind] += 1
    if not feed:
        _fail("nothing to put in the feed: no simulated frames and no usable labelled image")
    labels_target.parent.mkdir(parents=True, exist_ok=True)
    labels_target.write_text(
        json.dumps({"version": 1, "camera_id": camera, "images": feed}, indent=1) + "\n"
    )
    typer.echo(
        f"{len(feed)} frame(s) -> {target}: {n_sim} simulated, {used['photo']} photo(s) and "
        f"{used['gen']} generated image(s) x {hold}; {left_out} image(s) left out; "
        f"labels -> {labels_target}"
    )


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
    carry_from: Annotated[
        Path | None,
        typer.Option(
            help="Carry this slot file's spaces over onto the image (matched with its reference "
            "image) instead of detecting vehicles."
        ),
    ] = None,
    carry_ref: Annotated[
        Path | None,
        typer.Option(help="The image --carry-from was drawn on (default: its reference_image)."),
    ] = None,
) -> None:
    """Draft a slot file from detected vehicles or an older slot file (vision.md §4); fix it in
    tools/slot-editor."""
    import cv2

    from parking.config import SlotFile, load_slots
    from parking.vision.bootstrap import (
        CarryError,
        carry_slots,
        format_json,
        propose_slots,
        slot_file,
    )
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

    zone = cam.zones[0]
    if carry_from is not None:
        old_path = _resolve(carry_from, root)
        if not old_path.is_file():
            _fail(f"slot file {old_path} not found")
        old = load_slots(old_path)
        if carry_ref is None and old.reference_image is None:
            _fail(f"{old_path} names no reference_image; pass --carry-ref")
        ref_path = _resolve(carry_ref or Path(old.reference_image or ""), root)
        if ref_path.resolve() == path.resolve():
            _fail(
                f"--carry-ref {ref_path} is the new image itself (did `parking grab --force` "
                "replace it?); pass the image the old slots were drawn on"
            )
        old_ref = cv2.imread(str(ref_path))
        if old_ref is None:
            _fail(f"can't read --carry-ref image {ref_path}")
        try:
            carried = carry_slots(old, old_ref, frame)
        except CarryError as e:
            _fail(f"can't carry the slots over: {e}; draw them by hand in tools/slot-editor")
        slots = carried.slots
        how = f"carried from {old_path.name} ({carried.inliers} of {carried.matches} matches agree)"
        if carried.outside:
            how += f"; partly outside the image, clipped: {', '.join(carried.outside)}"
    else:
        # vision.md §4: masks on, 1280 px, a low confidence to find as many vehicles as possible
        masks = cam.detector.type == "yolo"  # YOLOX gives boxes only
        det_cfg = cam.detector.model_copy(update={"imgsz": imgsz, "conf": conf, "use_masks": masks})
        if _fake_detector_on(fake_detector):
            sidecar = path.with_suffix(".json")
            if not sidecar.is_file():
                _fail(f"fake detector: no sidecar {sidecar}")
            detector = FakeDetector(sidecar, conf=det_cfg.conf, classes=det_cfg.classes)
        else:
            detector = _yolo_detector(det_cfg, root)

        slots = propose_slots(detector.detect(frame), zone, footprint)
        if not slots:
            _fail(f"no vehicles found in {path}; draw the slots by hand in tools/slot-editor")
        how = f"in zone '{zone}'"

    try:
        reference = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        reference = str(path)
    data = slot_file(camera, (frame.shape[1], frame.shape[0]), slots, reference)
    SlotFile.model_validate(data)  # what load_slots() checks
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_json(data))
    hint = (
        "check every space covers its ground area (where the tyres are)"
        if carry_from is not None
        else "add the empty spaces and adjust the corners"
    )
    typer.echo(f"{len(slots)} slot(s) {how} -> {target}\nNow fix it in tools/slot-editor: {hint}.")


@app.command("export-yolo")
def export_yolo(
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ground.")],
    images: Annotated[
        Path | None,
        typer.Option(help="Folder with the labelled frames (default data/validation/<camera>)."),
    ] = None,
    labels: Annotated[
        Path | None,
        typer.Option(help="Labels file (default data/labels/<camera>-validation.json)."),
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Dataset folder to write (default data/yolo/<camera>).")
    ] = None,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    holdout: Annotated[
        float, typer.Option(help="Share of the frames kept for validation (by file name).")
    ] = 0.2,
    pad: Annotated[
        float, typer.Option(help="Grow each box by this share of the slot's size per side.")
    ] = 0.0,
    review: Annotated[
        bool, typer.Option("--review", help="Also write review/<frame>.jpg with the boxes drawn.")
    ] = False,
    coco: Annotated[
        bool,
        typer.Option(
            "--coco",
            help="Only rebuild annotations/*.json from the label files of an existing dataset "
            "(after correcting boxes).",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing dataset.")] = False,
) -> None:
    """Turn labelled frames into a detector training set (detector-training.md): one rough
    box per taken space, YOLO text labels + COCO JSON, 20% of the frames held out."""
    import shutil

    from parking.config import ConfigError, load_labels
    from parking.vision.yolo_dataset import SPLITS, export_frames, write_coco

    if not 0 <= holdout < 1:
        raise typer.BadParameter("must be between 0 and 1", param_hint="--holdout")
    if not 0 <= pad <= 0.5:
        raise typer.BadParameter("must be between 0 and 0.5", param_hint="--pad")

    _, cam, root = _occupancy_camera(config, camera, "export-yolo")
    target = _resolve_out(out or Path("data") / "yolo" / camera, root)

    if coco:
        if not (target / "labels").is_dir():
            _fail(f"no dataset at {target}; run export-yolo without --coco first")
        try:
            totals = write_coco(target)
        except ValueError as e:
            _fail(str(e))
        typer.echo(
            f"annotations rebuilt in {target / 'annotations'}: "
            + ", ".join(f"{s} {totals[s]} box(es)" for s in SPLITS)
        )
        return

    slots = _slot_file(cam, root)
    folder = _resolve(images or Path("data") / "validation" / camera, root)
    if not folder.is_dir():
        _fail(f"no image folder {folder}")
    labels_path = _resolve(labels or Path(f"data/labels/{camera}-validation.json"), root)
    try:
        label_file = load_labels(labels_path)
    except (ConfigError, ValueError, OSError) as e:
        _fail(f"labels {labels_path}: {e} (label the images with tools/slot-editor)")
    if label_file.camera_id != camera:
        _fail(f"labels {labels_path} are for camera '{label_file.camera_id}', not '{camera}'")
    known = {s.id for s in slots.slots}
    unknown = sorted(
        {i for lab in label_file.images.values() for i in (*lab.taken, *lab.unsure)} - known
    )
    if unknown:
        _fail(f"labels {labels_path} name slot(s) not in the slot file: {', '.join(unknown)}")

    if any((target / d).exists() for d in ("images", "labels")):
        if not force:
            _fail(f"{target} already holds a dataset; pass --force to replace it")
        for d in ("images", "labels", "annotations", "review"):
            shutil.rmtree(target / d, ignore_errors=True)

    stats = export_frames(folder, label_file, slots, target, holdout, pad, review)
    if stats.train + stats.val == 0:
        _fail(f"none of the {len(label_file.images)} labelled image(s) are in {folder}")
    for name in stats.missing:
        typer.echo(f"skipped {name}: not in {folder} or unreadable", err=True)
    typer.echo(
        f"{stats.train} train + {stats.val} val frame(s), {stats.boxes} box(es), "
        f"{stats.masked} unsure space(s) painted out -> {target}"
    )
    typer.echo(
        "The boxes are the slots' outlines, not the cars': look through them"
        + (f" in {target / 'review'}" if review else " (--review draws them)")
        + ", fix the label files where needed, then `--coco` if you train YOLOX."
    )


@app.command("slot-map")
def slot_map(
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ground.")],
    zone: Annotated[
        str | None, typer.Option(help="Zone to draw (default: the camera's first zone).")
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(help="SVG to write (default: the zone's map, else config/maps/<zone>.svg)."),
    ] = None,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Don't write: compare the existing map with the slot file."),
    ] = False,
    snap_deg: Annotated[
        float, typer.Option(help="Draw spaces turned by less than this upright (0 = never).")
    ] = 10.0,
    gap: Annotated[
        float, typer.Option(help="Space around each rectangle, as a share of a slot's width.")
    ] = 0.06,
) -> None:
    """Draw a zone's slot map from the slot polygons of a straight-down camera (slot-map.md)."""
    from parking.slot_map import SlotMapError, build_slot_map, compare_slot_map

    if not 0 <= snap_deg <= 45:
        raise typer.BadParameter("must be between 0 and 45", param_hint="--snap-deg")
    if not 0 <= gap < 0.5:
        raise typer.BadParameter("must be between 0 and 0.5", param_hint="--gap")

    lot, cam, root = _occupancy_camera(config, camera, "slot-map")
    zone_id = zone or cam.zones[0]
    if zone_id not in cam.zones:
        _fail(f"camera '{camera}' doesn't report on zone '{zone_id}' ({', '.join(cam.zones)})")
    slots = _slot_file(cam, root)
    configured = lot.zone(zone_id).map
    target = out or _resolve(configured or Path("config") / "maps" / f"{zone_id}.svg", root)

    if check:
        if not target.is_file():
            _fail(f"no map at {target}")
        try:
            missing, unknown = compare_slot_map(target.read_text(), slots, zone_id)
        except SlotMapError as e:
            _fail(f"{target}: {e}")
        if missing or unknown:
            _fail(
                f"{target} doesn't match the slot file: "
                f"not on the map: {', '.join(missing) or 'none'}; "
                f"on the map but no slot: {', '.join(unknown) or 'none'}"
            )
        typer.echo(f"{target} has every slot of zone '{zone_id}'")
        return

    if target.exists() and not force:
        _fail(f"{target} exists; pass --force to overwrite it")
    try:
        svg = build_slot_map(slots, zone_id, snap_deg=snap_deg, gap=gap)
    except SlotMapError as e:
        _fail(str(e))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(svg)
    typer.echo(f"{svg.count('<rect ')} slot(s) of zone '{zone_id}' -> {target}")
    if configured is None:
        typer.echo(f"Now set `map:` for zone '{zone_id}' in lot.yaml so the API serves it.")


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
    <models-dir>/bench/ on first use. Prints a Markdown table for docs/history.md → Metrics.
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
        reason = getattr(src, "last_error", None)  # device:/picamera: say what's wrong
        _fail(f"no frame from '{camera}' within {timeout:g} s" + (f": {reason}" if reason else ""))

    target.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(target), frame.image, [cv2.IMWRITE_JPEG_QUALITY, 95]):
        _fail(f"can't write {target}")
    h, w = frame.image.shape[:2]
    typer.echo(f"{w}x{h} frame from '{camera}' at {frame.ts.isoformat()} -> {target}")


@app.command()
def record(
    camera: Annotated[str, typer.Option(help="Camera id in the config, e.g. cam-ramp.")],
    minutes: Annotated[float, typer.Option(help="How long to record.")] = 60.0,
    out: Annotated[
        Path,
        typer.Option(help="Folder (file name <camera>-<lot-local time>.mp4) or an .mp4 path."),
    ] = Path("data/recordings"),
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    source: Annotated[
        str | None, typer.Option(help="rtsp: URI to use instead of the camera's `source`.")
    ] = None,
) -> None:
    """Save a camera's RTSP stream to MP4 with FFmpeg stream copy, no re-encoding (P5.2)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from parking.vision.recording import RecordError, ffmpeg_tool, record

    if minutes <= 0:
        raise typer.BadParameter("must be positive", param_hint="--minutes")
    lot, cam, root = _camera(config, camera)
    uri = source or cam.source
    scheme, _, url = uri.partition(":")
    if scheme != "rtsp" or not url:
        _fail(f"record needs an 'rtsp:' source; '{camera}' uses '{scheme}:'")
    out = _resolve(out, root)
    if out.suffix.lower() != ".mp4":
        stamp = datetime.now(ZoneInfo(lot.lot.timezone)).strftime("%Y-%m-%d-%H%M")
        out = out / f"{camera}-{stamp}.mp4"
    if out.exists():
        _fail(f"{out} exists")
    try:
        ffmpeg_tool(), ffmpeg_tool("ffprobe")
    except RecordError as e:
        _fail(str(e))

    seconds = minutes * 60
    typer.echo(f"recording '{camera}' for {seconds:g} s -> {out} (Ctrl-C stops early)")
    rec = record(url, seconds, out)
    if rec.errors:
        typer.echo(rec.errors, err=True)  # redacted: never holds the URL or password
    info = rec.info
    if info is None:
        _fail(f"nothing recorded (ffmpeg exit code {rec.returncode})")
    size = out.stat().st_size / 1e6
    fps = f"{info.fps:.2f}" if info.fps else "?"
    typer.echo(
        f"{out}: {info.duration_s or 0:.1f} s, {info.codec} {info.width}x{info.height}, "
        f"{info.frames} frames, {fps} fps, {size:.1f} MB"
    )
    if not rec.complete:
        _fail(f"cut short: {info.duration_s or 0:.0f} of {seconds:g} s (the file is kept)")


@app.command("stream-check")
def stream_check(
    camera: Annotated[str | None, typer.Option(help="Camera id in the config.")] = None,
    source: Annotated[
        str | None,
        typer.Option(help="Source URI instead of the camera's, e.g. video:data/recordings/x.mp4."),
    ] = None,
    seconds: Annotated[float, typer.Option(help="How long to read, from the first frame.")] = 60.0,
    window: Annotated[float, typer.Option(help="Seconds per fps window.")] = 10.0,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    as_json: Annotated[bool, typer.Option("--json", help="Print the stats as JSON.")] = False,
) -> None:
    """Read a source and report its frame rate per window; exit 1 unless it is steady (P5.2).

    Steady = every window within ±20% of the median and no gap over 1 s between new frames.
    """
    from parking.vision.recording import measure_stream
    from parking.vision.sources import make_source

    if seconds <= 0 or window <= 0:
        raise typer.BadParameter("must be positive", param_hint="--seconds/--window")
    if camera is None and source is None:
        raise typer.BadParameter("give --camera or --source")
    root = Path.cwd()
    if camera is not None:
        _, cam, root = _camera(config, camera)
        source = source or cam.source
    else:
        root = _find_config(config).resolve().parent.parent
    try:
        src = make_source(source, root)
    except ValueError as e:
        _fail(str(e))

    def show(elapsed: float, fps: float) -> None:
        if not as_json:
            typer.echo(f"{elapsed:7.1f} s  {fps:6.2f} fps")

    try:
        stats = measure_stream(src, seconds, window=window, on_window=show)
    finally:
        src.close()
    extra = {k: getattr(src, k) for k in ("native_fps", "dropped", "reconnects") if hasattr(src, k)}
    if as_json:
        typer.echo(json.dumps(stats.to_dict() | extra))
    else:
        rates = stats.windows or [0.0]
        typer.echo(
            f"{stats.frames} frames in {stats.seconds:.1f} s = {stats.fps:.2f} fps "
            f"(windows {min(rates):.2f}–{max(rates):.2f}, longest gap {stats.max_gap_s:.2f} s"
            + "".join(f", {k} {v:g}" for k, v in extra.items() if v is not None)
            + f"): {'steady' if stats.steady else 'NOT steady'}"
        )
    if not stats.steady:
        raise typer.Exit(1)


@app.command("motion-check")
def motion_check(
    camera: Annotated[str, typer.Option(help="Flow camera id in the config, e.g. cam-ramp.")],
    source: Annotated[
        str | None,
        typer.Option(help="Source URI instead of the camera's, e.g. video:data/recordings/x.mp4."),
    ] = None,
    seconds: Annotated[float, typer.Option(help="Frame time to cover, from the first frame.")] = (
        3600.0
    ),
    max_ratio: Annotated[
        float, typer.Option(help="Exit 1 when the gate is active for this share or more.")
    ] = 0.20,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    as_json: Annotated[bool, typer.Option("--json", help="Print the stats as JSON.")] = False,
) -> None:
    """Run the motion gate over a flow camera or a recording and log how often it's active (P5.3).

    Uses the camera's `flow.motion_min_area_px` and its line file's ROI (whole frame if the
    line file is missing). For a recording pass `--source video:<file>?realtime=false`.
    """
    from parking.config import ConfigError, load_lines
    from parking.vision.motion import MotionGate, measure_motion
    from parking.vision.sources import make_source

    if seconds <= 0 or not 0 < max_ratio <= 1:
        raise typer.BadParameter("--seconds > 0 and 0 < --max-ratio ≤ 1")
    _, cam, root = _camera(config, camera)
    if cam.role != "flow":
        _fail(f"camera '{camera}' is an {cam.role} camera; motion-check needs a flow camera")
    lines = None
    path = _resolve(cam.lines_file, root)
    try:
        lines = load_lines(path)
    except (ConfigError, ValueError, OSError) as e:
        typer.echo(f"warning: no usable line file ({e}); the gate watches the whole frame")
    try:
        src = make_source(source or cam.source, root)
    except ValueError as e:
        _fail(str(e))
    gate = MotionGate(cam.flow.motion_min_area_px, lines)
    minute = [-1]

    def show(elapsed: float, res) -> None:
        if not as_json and int(elapsed // 60) != minute[0]:
            minute[0] = int(elapsed // 60)
            typer.echo(f"{elapsed:7.0f} s  {'active' if res.active else 'idle'}")

    try:
        stats = measure_motion(src, gate, seconds, on_frame=show)
    finally:
        src.close()
    if not stats.frames:
        _fail("no frames from the source")
    ok = stats.ratio < max_ratio
    if as_json:
        typer.echo(json.dumps(stats.to_dict() | {"max_ratio": max_ratio, "ok": ok}))
    else:
        typer.echo(
            f"{stats.frames} frames over {stats.seconds:.0f} s: gate active "
            f"{stats.ratio:.1%} of frames in {stats.bursts} burst(s) "
            f"(limit {max_ratio:.0%}): {'ok' if ok else 'too often'}"
        )
    if stats.seconds < 0.95 * seconds:
        typer.echo(f"warning: only {stats.seconds:.0f} of {seconds:g} s of frames")
    if not ok:
        raise typer.Exit(1)


@app.command("track-check")
def track_check(
    camera: Annotated[str, typer.Option(help="Flow camera id in the config, e.g. cam-ramp.")],
    source: Annotated[
        str | None,
        typer.Option(help="Source URI instead of the camera's, e.g. video:data/recordings/x.mp4."),
    ] = None,
    seconds: Annotated[float, typer.Option(help="Frame time to cover, from the first frame.")] = (
        600.0
    ),
    debug_video: Annotated[
        Path | None,
        typer.Option(help="Write the annotated frames (ROI, lines, boxes, track ids) to this MP4."),
    ] = None,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    as_json: Annotated[bool, typer.Option("--json", help="Print the stats as JSON.")] = False,
) -> None:
    """Run gate + detector + ByteTrack over a flow camera or a clip and list the tracks (P5.4).

    Each passing car should show up as one track id from entering to leaving the frame; check
    it in `--debug-video`. Uses the camera's `detector` settings and its line file's ROI.
    """
    from parking.config import ConfigError, load_lines
    from parking.vision.motion import MotionGate
    from parking.vision.sources import make_source
    from parking.vision.tracking import (
        UltralyticsTracker,
        VehicleTracker,
        draw_tracks,
        run_tracking,
    )

    if seconds <= 0:
        raise typer.BadParameter("must be > 0", param_hint="--seconds")
    _, cam, root = _camera(config, camera)
    if cam.role != "flow":
        _fail(f"camera '{camera}' is an {cam.role} camera; track-check needs a flow camera")
    lines = None
    try:
        lines = load_lines(_resolve(cam.lines_file, root))
    except (ConfigError, ValueError, OSError) as e:
        typer.echo(f"warning: no usable line file ({e}); tracking the whole frame")
    det = cam.detector
    model = _resolve(Path(det.model), root)
    if not model.exists():
        _fail(f"model {model} not found; run `parking models export` first")
    backend = UltralyticsTracker(str(model), det.imgsz, det.conf, det.classes)
    tracker = VehicleTracker(backend, lines)
    gate = MotionGate(cam.flow.motion_min_area_px, lines)
    try:
        src = make_source(source or cam.source, root)
    except ValueError as e:
        _fail(str(e))
    writer = [None]

    def write(image, elapsed: float, active: bool, tracks) -> None:
        if debug_video is None:
            return
        import cv2

        if writer[0] is None:
            debug_video.parent.mkdir(parents=True, exist_ok=True)
            fps = getattr(src, "native_fps", None) or cam.fps
            size = (image.shape[1], image.shape[0])
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer[0] = cv2.VideoWriter(str(debug_video), fourcc, fps, size)
            if not writer[0].isOpened():
                _fail(f"can't write {debug_video}")
        ids = " ".join(f"#{t.track_id}" for t in tracks)
        banner = f"{elapsed:6.1f} s  {'ACTIVE' if active else 'idle'}  {ids}"
        writer[0].write(draw_tracks(image, tracks, lines, banner))

    try:
        stats = run_tracking(src, gate, tracker, seconds, on_frame=write)
    finally:
        src.close()
        if writer[0] is not None:
            writer[0].release()
    if not stats.frames:
        _fail("no frames from the source")
    if as_json:
        typer.echo(json.dumps(stats.to_dict()))
    else:
        typer.echo(
            f"{stats.frames} frames over {stats.seconds:.0f} s, {stats.active_frames} gated in "
            f"({stats.track_ms_avg:.0f} ms each), {len(stats.spans)} track(s), "
            f"{stats.resets} reset(s)"
        )
        for s in stats.spans:
            (fx, fy), (lx, ly) = s.first_anchor, s.last_anchor
            typer.echo(
                f"  #{s.track_id:<4} {s.cls:<10} {s.frames:4d} frames {s.seconds:6.1f} s  "
                f"({fx:.0f},{fy:.0f}) -> ({lx:.0f},{ly:.0f})"
            )
    if debug_video is not None and writer[0] is not None:
        typer.echo(f"debug video -> {debug_video}")


@app.command("evaluate-flow")
def evaluate_flow(
    video: Annotated[Path, typer.Option(help="The clip, e.g. data/recordings/x.mp4.")],
    camera: Annotated[str, typer.Option(help="Flow camera id in the config, e.g. cam-ramp.")],
    truth: Annotated[
        Path | None,
        typer.Option(help="Tally CSV (config.md §4); default data/labels/<clip name>.csv."),
    ] = None,
    lines_file: Annotated[
        Path | None, typer.Option("--lines", help="Line file instead of the camera's (tuning).")
    ] = None,
    conf: Annotated[
        float | None, typer.Option(help="Detector confidence instead of the camera's (tuning).")
    ] = None,
    min_track_frames: Annotated[
        int | None, typer.Option(help="flow.min_track_frames instead of the camera's (tuning).")
    ] = None,
    tolerance: Annotated[
        float, typer.Option(help="Match window in seconds (vision.md §10).")
    ] = 2.0,
    realtime: Annotated[
        bool,
        typer.Option(
            help="Play at the clip's speed and drop frames when slow, like a live camera."
        ),
    ] = False,
    debug_video: Annotated[
        Path | None,
        typer.Option(
            help="Write the annotated frames (lines, tracks, running IN/OUT) to this MP4."
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Report folder.")] = Path("out/eval"),
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Run the full flow pipeline on a clip and compare its events with a tally (P5.9).

    Prints TP/FP/FN, event accuracy and net error; writes out/eval/flow-<clip>-<date>.json and
    the counted events as out/eval/flow-<clip>-pred.csv (open it in tools/flow-tally to review).
    """
    from datetime import date

    from parking.config import ConfigError, load_lines
    from parking.vision import evaluate as ev
    from parking.vision.flow import TwoLineCounter
    from parking.vision.motion import MotionGate
    from parking.vision.sources import VideoFileSource
    from parking.vision.tracking import UltralyticsTracker, VehicleTracker, draw_tracks

    if tolerance <= 0:
        raise typer.BadParameter("must be > 0", param_hint="--tolerance")
    if conf is not None and not 0 < conf < 1:
        raise typer.BadParameter("must be between 0 and 1", param_hint="--conf")
    if min_track_frames is not None and min_track_frames < 1:
        raise typer.BadParameter("must be >= 1", param_hint="--min-track-frames")
    _, cam, root = _camera(config, camera)
    if cam.role != "flow":
        _fail(f"camera '{camera}' is an {cam.role} camera; evaluate-flow needs a flow camera")
    video = _resolve(video, root)
    if not video.is_file():
        _fail(f"clip {video} not found")
    truth = _resolve(truth, root) if truth else root / "data" / "labels" / f"{video.stem}.csv"
    try:
        tally = ev.load_flow_labels(truth)
    except (OSError, ValueError) as e:
        _fail(f"tally {truth}: {e}")
    lines_path = _resolve(lines_file or Path(cam.lines_file), root)
    try:
        lines = load_lines(lines_path)
    except (ConfigError, ValueError, OSError) as e:
        _fail(f"line file {lines_path}: {e}")
    if lines.camera_id != cam.id:
        typer.echo(f"warning: {lines_path.name} is for '{lines.camera_id}', not '{cam.id}'")
    det = cam.detector.model_copy(update={"conf": conf} if conf is not None else {})
    mtf = min_track_frames or cam.flow.min_track_frames
    model = _resolve(Path(det.model), root)
    if not model.exists():
        _fail(f"model {model} not found; run `parking models export` first")
    tracker = VehicleTracker(
        UltralyticsTracker(str(model), det.imgsz, det.conf, det.classes), lines
    )
    gate = MotionGate(cam.flow.motion_min_area_px, lines)
    counter = TwoLineCounter(lines, mtf)
    src = VideoFileSource(video, realtime=realtime)
    writer = [None]
    counts = {"in": 0, "out": 0}

    def write(image, elapsed: float, active: bool, tracks, new) -> None:
        for e in new:
            counts[e.direction] += 1
        if debug_video is None:
            return
        import cv2

        if writer[0] is None:
            debug_video.parent.mkdir(parents=True, exist_ok=True)
            size = (image.shape[1], image.shape[0])
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer[0] = cv2.VideoWriter(str(debug_video), fourcc, src.native_fps or 10, size)
            if not writer[0].isOpened():
                _fail(f"can't write {debug_video}")
        banner = (
            f"{elapsed:6.1f} s  {'ACTIVE' if active else 'idle'}  "
            f"IN {counts['in']}  OUT {counts['out']}"
        )
        writer[0].write(draw_tracks(image, tracks, lines, banner))

    try:
        run = ev.run_flow(src, gate, tracker, counter, on_frame=write)
    finally:
        src.close()
        if writer[0] is not None:
            writer[0].release()
    if not run.frames:
        _fail(f"no frames in {video}")
    result = ev.match_flow(run.events, tally, tolerance)
    settings = {
        "conf": det.conf,
        "imgsz": det.imgsz,
        "min_track_frames": mtf,
        "motion_min_area_px": cam.flow.motion_min_area_px,
        "lines": str(lines_path),
        "tolerance_s": tolerance,
        "realtime": realtime,
    }
    report = {
        "clip": video.name,
        "camera_id": cam.id,
        "truth": str(truth),
        "date": date.today().isoformat(),
        "settings": settings,
        "run": run.to_dict() | {"dropped": src.dropped},
        **result.to_dict(),
    }
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"flow-{video.stem}-{report['date']}.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    csv_path = out / f"flow-{video.stem}-pred.csv"
    csv_path.write_text(ev.flow_labels_csv(run.events))
    if as_json:
        typer.echo(json.dumps(report))
        return
    typer.echo(
        f"{video.name}: {run.frames} frames over {run.seconds:.0f} s, {run.active_frames} gated in "
        f"({run.track_ms_avg:.0f} ms each), {run.tracks} track(s); conf {det.conf:g}, "
        f"min_track_frames {mtf}" + (f", {src.dropped} frame(s) dropped" if realtime else "")
    )
    typer.echo("\n".join(ev.flow_lines(result)))
    typer.echo(f"report -> {json_path}; counted events -> {csv_path}")
    if debug_video is not None and writer[0] is not None:
        typer.echo(f"debug video -> {debug_video}")


@app.command("lines-check")
def lines_check(
    camera: Annotated[str, typer.Option(help="Flow camera id in the config, e.g. cam-ramp.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    image: Annotated[
        Path | None,
        typer.Option(help="Reference frame (default data/reference/<camera>.jpg)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(help="Overlay to write (default out/lines/<camera>.jpg)."),
    ] = None,
) -> None:
    """Check a flow camera's line file and draw it on the reference frame (P5.1)."""
    import cv2

    from parking.config import ConfigError, load_lines
    from parking.vision.lines import check_lines, draw_lines

    _, cam, root = _camera(config, camera)
    if cam.role != "flow":
        _fail(f"camera '{camera}' is an {cam.role} camera; lines-check needs a flow camera")
    path = _resolve(cam.lines_file, root)
    try:
        lf = load_lines(path)
    except (ConfigError, ValueError) as e:
        _fail(f"line file {path}: {e} (draw the lines with the slot editor's lines mode)")
    if lf.camera_id != camera:
        _fail(f"line file {path} is for camera '{lf.camera_id}', not '{camera}'")

    ref_path = _resolve(image or Path(f"data/reference/{camera}.jpg"), root)
    frame = cv2.imread(str(ref_path)) if ref_path.is_file() else None
    if frame is None:
        typer.echo(f"warning: no reference frame at {ref_path}; checking the file only")
    res = check_lines(lf, None if frame is None else (frame.shape[1], frame.shape[0]))
    typer.echo(
        f"line_a {res.length_a:.0f} px, line_b {res.length_b:.0f} px, "
        f"{res.gap:.0f} px apart, {res.angle:.0f}° between them, IN = {lf.in_direction}"
    )
    for msg in res.warnings:
        typer.echo(f"warning: {msg}")
    if frame is not None:
        target = out or root / "out" / "lines" / f"{camera}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), draw_lines(frame, lf)):
            _fail(f"can't write {target}")
        typer.echo(f"overlay -> {target} (check that the whole lane is in view)")
    if not res.ok:
        for msg in res.errors:
            typer.echo(f"error: {msg}", err=True)
        raise typer.Exit(1)
    typer.echo("ok")


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


@app.command("light-feed")
def light_feed(
    source: Annotated[Path, typer.Option(help="Folder of daytime frames to light.")] = Path(
        "data/replay/ground-feed"
    ),
    out: Annotated[Path, typer.Option(help="Folder for the lit frames.")] = Path(
        "data/replay/ground-lit"
    ),
    frames: Annotated[int, typer.Option(help="Frames in the day (240 = one every 6 min).")] = 240,
    start_hour: Annotated[float, typer.Option(help="Lot-local hour of the first frame.")] = 12.0,
    rain: Annotated[
        str, typer.Option(help="Rainy hours, e.g. '14-16,22-23' (empty: none).")
    ] = "14-16,22-23",
    cover: Annotated[
        Path | None,
        typer.Option(help="Also write a lens-cover image here (default <out>-cover.jpg)."),
    ] = None,
    seed: Annotated[int, typer.Option(help="Same seed, same frames.")] = 1,
    quality: Annotated[int, typer.Option(help="JPEG quality of the frames.")] = 90,
    config: Annotated[Path, typer.Option(help="lot.yaml to use (for the repo root).")] = (
        DEFAULT_CONFIG
    ),
) -> None:
    """Make 24 h of light (day, dusk, IR night, rain) from daytime frames, to tune and check
    the frame-health thresholds on images (P4.4, vision.md §5)."""
    import cv2

    from parking.vision import lighting
    from parking.vision.simulate import frame_name

    if frames <= 0:
        raise typer.BadParameter("must be positive", param_hint="--frames")
    try:
        spans = [tuple(float(x) for x in s.split("-")) for s in rain.split(",") if s.strip()]
        if any(len(s) != 2 for s in spans):
            raise ValueError
    except ValueError:
        raise typer.BadParameter("hours like '14-16,22-23'", param_hint="--rain") from None
    root = _find_config(config).resolve().parent.parent
    src = _resolve(source, root)
    files = sorted(f for f in src.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not files:
        _fail(f"no images in {src}")
    target = _resolve_out(out, root)
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("frame-*.jpg"):
        old.unlink()
    notes = {}
    shape = None
    for i in range(frames):
        hour = (start_hour + 24 * i / frames) % 24
        wet = any(a <= hour < b for a, b in spans)
        image = cv2.imread(str(files[i % len(files)]))
        if image is None:
            _fail(f"can't read {files[i % len(files)]}")
        shape = image.shape
        lit, how = lighting.light(image, hour, wet, seed, i)
        name = frame_name(i)
        if not cv2.imwrite(str(target / name), lit, [cv2.IMWRITE_JPEG_QUALITY, quality]):
            _fail(f"can't write {target / name}")
        notes[name] = {"hour": round(hour, 2), "phase": how.phase, "rain": wet}
    (target / "lighting.json").write_text(json.dumps(notes, indent=1) + "\n")
    cover_path = _resolve_out(cover or Path(f"{out}-cover.jpg"), root)
    cv2.imwrite(str(cover_path), lighting.covered(shape, seed), [cv2.IMWRITE_JPEG_QUALITY, quality])
    counts = {p: sum(n["phase"] == p for n in notes.values()) for p in ("day", "dusk", "night")}
    typer.echo(
        f"{frames} frame(s) from {len(files)} image(s) -> {target} ("
        + ", ".join(f"{v} {k}" for k, v in counts.items())
        + f", {sum(n['rain'] for n in notes.values())} in rain), lens cover -> {cover_path}"
    )


def _flow_zone(config: Path, zone: str):
    """Load lot.yaml and return `(lot, root)`; exits unless `zone` is a flow zone."""
    from parking.config import ConfigError, cli_env, load_lots

    config = _find_config(config)
    root = config.resolve().parent.parent
    try:
        lots = load_lots(config, cli_env(root))
    except (ConfigError, ValueError) as e:
        _fail(f"{config}: {e}")
    zones = {z.id: z for lot in lots for z in lot.zones}
    if zone not in zones:
        _fail(f"zone '{zone}' is not in {config} (zones: {', '.join(zones) or 'none'})")
    lot = next(lot for lot in lots if any(z.id == zone for z in lot.zones))
    if zones[zone].method != "flow":
        _fail(f"zone '{zone}' is a {zones[zone].method} zone; the drift test is for flow zones")
    return lot, root


@app.command("drift-note")
def drift_note(
    zone: Annotated[str, typer.Option(help="Flow zone id in the config.")],
    true: Annotated[int, typer.Option("--true", min=0, help="Cars counted in the zone now.")],
    app_value: Annotated[
        int | None, typer.Option("--app", min=0, help="The app's occupied value (skips the API).")
    ] = None,
    api_url: Annotated[
        str, typer.Option("--api", envvar="API_URL", help="API to read the app's value from.")
    ] = "http://localhost:8000",
    note: Annotated[str, typer.Option(help="Free text, e.g. 'after a rainy night'.")] = "",
    at: Annotated[str | None, typer.Option(help="ISO time of the count (default: now).")] = None,
    file: Annotated[
        Path | None, typer.Option(help="Notes CSV (default: data/labels/drift-<zone>.csv).")
    ] = None,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
) -> None:
    """Note the true count of a flow zone next to the app's value (drift test, P5.11)."""
    from datetime import UTC, datetime

    from parking.core.drift import DriftNote, append_drift_note

    _, root = _flow_zone(config, zone)
    if at is None:
        ts = datetime.now(UTC)
    else:
        try:
            ts = datetime.fromisoformat(at)
        except ValueError:
            raise typer.BadParameter("not an ISO time", param_hint="--at") from None
        ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if app_value is None:
        import httpx

        try:
            res = httpx.get(f"{api_url.rstrip('/')}/api/status", timeout=10)
            res.raise_for_status()
            zones = {z["id"]: z for z in res.json()["zones"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            _fail(f"can't read {api_url}/api/status ({e}); pass the app's value with --app")
        if zone not in zones:
            _fail(f"zone '{zone}' is not in the API's status; pass the app's value with --app")
        app_value = int(zones[zone]["occupied"])
    path = file or root / "data" / "labels" / f"drift-{zone}.csv"
    row = DriftNote(ts, true, app_value, note)
    append_drift_note(path, row)
    typer.echo(f"{zone}: true {true}, app {app_value}, error {row.error:+d} -> {path}")


@app.command("drift-report")
def drift_report(
    zone: Annotated[str, typer.Option(help="Flow zone id in the config.")],
    file: Annotated[
        Path | None, typer.Option(help="Notes CSV (default: data/labels/drift-<zone>.csv).")
    ] = None,
    target: Annotated[float, typer.Option(min=0, help="Allowed drift in cars per day.")] = 2.0,
    days: Annotated[float, typer.Option(min=0, help="Days the test has to cover.")] = 7.0,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """Drift per day from the drift-note file; exit 0 only when the verdict is PASSED (P5.11)."""
    from parking.core.drift import load_drift_notes, measure_drift, report

    _, root = _flow_zone(config, zone)
    path = file or root / "data" / "labels" / f"drift-{zone}.csv"
    if not path.is_file():
        _fail(f"{path}: no such file (add counts with `parking drift-note`)")
    try:
        notes = load_drift_notes(path)
    except ValueError as e:
        _fail(str(e))
    if not notes:
        _fail(f"{path}: no notes yet (add counts with `parking drift-note`)")
    result = measure_drift(notes, target=target, min_days=days)
    if as_json:
        typer.echo(json.dumps({"zone": zone, **result.as_dict()}, indent=2))
    else:
        typer.echo(report(result, zone))
    if result.verdict != "PASSED":
        raise typer.Exit(1)


@validation_app.command("timelapse")
def validation_timelapse(
    camera: Annotated[str, typer.Option(help="Occupancy camera id in the config.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    frames: Annotated[
        Path, typer.Option(help="Folder of labelled daytime frames (the replay feed).")
    ] = Path("data/replay/ground-feed"),
    labels: Annotated[
        Path | None, typer.Option(help="Their labels (default data/labels/<camera>-feed.json).")
    ] = None,
    days: Annotated[int, typer.Option(min=1, help="Days of captures to make.")] = 14,
    start: Annotated[
        str | None, typer.Option(help="Lot-local first day, YYYY-MM-DD (default: today - days).")
    ] = None,
    every_min: Annotated[float, typer.Option(help="Minutes between analysed frames.")] = 15.0,
    capture_every_min: Annotated[
        float, typer.Option(help="DEBUG_CAPTURE_EVERY_MIN of the simulated worker.")
    ] = 10.0,
    truth: Annotated[
        Path | None,
        typer.Option(help="Truth labels to write (default data/labels/<camera>-timelapse.json)."),
    ] = None,
    seed: Annotated[int, typer.Option(help="Same seed, same light and rain.")] = 1,
    force: Annotated[
        bool, typer.Option("--force", help="Delete captures already in data/debug/<camera>.")
    ] = False,
) -> None:
    """Make weeks of debug captures from labelled images (lit by the hour, with rain), analysed
    like the worker does, plus their truth labels: the P4.8 PoC stand-in for a real camera."""
    import shutil
    from datetime import date, datetime, timedelta

    from parking.config import ConfigError, LabelFile, load_labels
    from parking.vision.health import FrameHealth
    from parking.vision.pipeline import analyze_frame
    from parking.workers import timelapse as tl
    from parking.workers.debug_capture import debug_dir

    if every_min <= 0 or capture_every_min <= 0:
        raise typer.BadParameter("minutes must be positive")
    lot, cam, root = _occupancy_camera(config, camera, "validation timelapse")
    try:
        first = date.fromisoformat(start) if start else date.today() - timedelta(days=days)
    except ValueError:
        raise typer.BadParameter("YYYY-MM-DD", param_hint="--start") from None
    src = _resolve(frames, root)
    files = sorted(f for f in src.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not files:
        _fail(f"no images in {src}")
    labels_path = _resolve(labels or Path(f"data/labels/{camera}-feed.json"), root)
    try:
        label_file = load_labels(labels_path)
    except (ConfigError, ValueError) as e:
        _fail(f"labels {labels_path}: {e}")
    missing = [f.name for f in files if f.name not in label_file.images]
    if missing:
        _fail(f"{len(missing)} frame(s) not labelled in {labels_path}, e.g. {missing[0]}")
    out_dir = debug_dir(root, camera)
    old = [d for d in out_dir.iterdir() if d.is_dir()] if out_dir.is_dir() else []
    if old and not force:
        _fail(f"{out_dir} already has captures (--force deletes them)")
    for d in old:
        shutil.rmtree(d)

    slot_file = _slot_file(cam, root)
    capacities = {z: lot.zone_capacity(z, [slot_file]) for z in cam.zones}
    occ = cam.occupancy
    detector = _yolo_detector(cam.detector, root) if occ.uses_detector else None
    reference, classifier = _reference(occ, root), _classifier(occ, root)

    def analyse(image):
        return analyze_frame(image, cam, slot_file, detector, capacities, reference, classifier)

    def progress(i: int, total: int) -> None:
        if i % 200 == 0 or i == total:
            typer.echo(f"  {i}/{total} frames", err=True)

    result = tl.run(
        files,
        label_file,
        analyse,
        FrameHealth(cam.health, check_frozen=False),
        root=root,
        camera_id=camera,
        lot_id=lot.lot.id,
        tz=lot.lot.timezone,
        start=datetime.combine(first, datetime.min.time()),
        days=days,
        every_min=every_min,
        capture_every_min=capture_every_min,
        seed=seed,
        progress=progress,
    )
    truth_path = _resolve_out(truth or Path(f"data/labels/{camera}-timelapse.json"), root)
    truth_path.parent.mkdir(parents=True, exist_ok=True)
    truth_file = LabelFile(version=1, camera_id=camera, images=result.truth)
    truth_path.write_text(truth_file.model_dump_json(indent=1) + "\n")
    bad = ", ".join(f"{n} {k}" for k, n in sorted(result.unhealthy.items())) or "none"
    acc = 1 - result.slot_errors / result.slots if result.slots else 0.0
    typer.echo(
        f"{result.frames} frame(s) over {days} day(s) from {first}: {result.saved} captured -> "
        f"{out_dir}, unhealthy: {bad}; slots right {acc:.1%} ({result.slots - result.slot_errors}"
        f"/{result.slots}); truth -> {truth_path}"
    )


@validation_app.command("pick")
def validation_pick(
    camera: Annotated[str, typer.Option(help="Occupancy camera id in the config.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    count: Annotated[int, typer.Option(help="Frames to pick.")] = 200,
    src: Annotated[
        Path | None, typer.Option("--from", help="Capture folder (default data/debug/<camera>).")
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Folder to copy to (default data/validation/<camera>).")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only print the picks.")] = False,
    labels_from: Annotated[
        Path | None,
        typer.Option(
            help="Known truth for the captures (`validation timelapse`): add the picks' labels "
            "to data/labels/<camera>-validation.json instead of labelling them by hand."
        ),
    ] = None,
) -> None:
    """Copy debug captures spread over time of day x occupancy into the validation folder."""
    import shutil
    from collections import Counter

    from parking.vision import validation as va

    if count <= 0:
        raise typer.BadParameter("must be positive", param_hint="--count")
    _, _, root = _occupancy_camera(config, camera, "validation pick")
    src_dir = _resolve(src, root) if src else root / "data" / "debug" / camera
    out_dir = _resolve(out, root) if out else root / "data" / "validation" / camera
    if not src_dir.is_dir():
        _fail(f"{src_dir}: no captures (run the worker with DEBUG_CAPTURE=true)")
    captures = va.scan(src_dir)
    if not captures:
        _fail(f"{src_dir}: no captures with an observation JSON")
    chosen = va.pick(captures, count)
    strata = Counter(c.stratum for c in chosen)
    for (per, lev), n in sorted(strata.items()):
        typer.echo(f"{per:8} {lev:8} {n:4}")
    copied = skipped = 0
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    for c in chosen:
        target = out_dir / c.out_name
        if target.exists():
            skipped += 1
        elif not dry_run:
            shutil.copy2(c.path, target)
            copied += 1
    verb = "would copy" if dry_run else "copied"
    typer.echo(
        f"{len(chosen)} of {len(captures)} capture(s) picked, {verb} "
        f"{len(chosen) - skipped if dry_run else copied}, {skipped} already there -> {out_dir}"
    )
    if dry_run:
        return
    if labels_from is None:
        typer.echo("Label them in tools/slot-editor (label mode, tag the conditions).")
        return
    from parking.config import ConfigError, LabelFile, load_labels

    try:
        truth = load_labels(_resolve(labels_from, root))
    except (ConfigError, ValueError) as e:
        _fail(f"labels {labels_from}: {e}")
    unknown = [c.out_name for c in chosen if c.out_name not in truth.images]
    if unknown:
        _fail(f"{len(unknown)} pick(s) not in {labels_from}, e.g. {unknown[0]}")
    target = root / "data" / "labels" / f"{camera}-validation.json"
    try:
        current = load_labels(target) if target.is_file() else None
    except (ConfigError, ValueError) as e:
        _fail(f"labels {target}: {e}")
    images = dict(current.images) if current else {}
    added = 0
    for c in chosen:
        if c.out_name not in images:  # never overwrite a label someone made or fixed
            images[c.out_name] = truth.images[c.out_name]
            added += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = LabelFile(version=1, camera_id=camera, images=dict(sorted(images.items())))
    target.write_text(merged.model_dump_json(indent=1) + "\n")
    typer.echo(f"{added} label(s) added from {labels_from} -> {target} ({len(images)} in all)")


@validation_app.command("check")
def validation_check(
    camera: Annotated[str, typer.Option(help="Occupancy camera id in the config.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    labels: Annotated[
        Path | None,
        typer.Option(help="Labels file (default data/labels/<camera>-validation.json)."),
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option(
            help="Comma list of tags that must each reach --min-per-tag "
            "(default morning,noon,evening,night,dry,rain,empty,busy,full)."
        ),
    ] = None,
    min_images: Annotated[int, typer.Option(help="Labelled images needed.")] = 200,
    min_per_tag: Annotated[int, typer.Option(help="Images needed per required tag.")] = 15,
) -> None:
    """Check the validation labels against P4.8's target (exit 1 if not reached)."""
    from parking.config import ConfigError, load_labels
    from parking.vision import validation as va

    _, _, root = _occupancy_camera(config, camera, "validation check")
    path = _resolve(labels or Path(f"data/labels/{camera}-validation.json"), root)
    try:
        label_file = load_labels(path)
    except (ConfigError, ValueError) as e:
        _fail(f"labels {path}: {e} (label the images with tools/slot-editor)")
    if label_file.camera_id != camera:
        _fail(f"labels {path} are for camera '{label_file.camera_id}', not '{camera}'")
    required = _csv(tags) if tags is not None else va.REQUIRED_TAGS
    cov = va.coverage(label_file, required, min_images, min_per_tag)
    typer.echo(f"images   {cov.images:4} / {min_images}")
    for tag, n in cov.tags.items():
        mark = "  <- short" if tag in cov.missing else ""
        typer.echo(f"{tag:8} {n:4}{mark}")
    if not cov.ok:
        _fail(f"not enough yet (need {min_images} images and {min_per_tag} per required tag)")
    typer.echo("ok")


@validation_app.command("blur")
def validation_blur(
    camera: Annotated[str, typer.Option(help="Occupancy camera id in the config.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    folder: Annotated[
        Path | None, typer.Option(help="Images to blur (default data/validation/<camera>).")
    ] = None,
    boxes: Annotated[
        Path | None,
        typer.Option(help='Hand-drawn boxes (plates): {"<image>.jpg": [[x1, y1, x2, y2], ...]}.'),
    ] = None,
    model: Annotated[Path, typer.Option(help="Detector for `person` boxes.")] = Path(
        "models/yolo11n_ncnn_model"
    ),
    conf: Annotated[float, typer.Option(help="Confidence for a `person` box.")] = 0.25,
    imgsz: Annotated[int, typer.Option(help="Detector input size.")] = 1280,
    no_detect: Annotated[
        bool, typer.Option("--no-detect", help="Only the hand-drawn boxes, no detector.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only print the boxes.")] = False,
) -> None:
    """Blur people (detector) and plates (hand-drawn boxes) in the validation images, in place
    (P4.8 step 4); `blurred.json` remembers what was done."""
    import cv2

    from parking.vision import blur as bl

    _, _, root = _occupancy_camera(config, camera, "validation blur")
    src = _resolve(folder, root) if folder else root / "data" / "validation" / camera
    images = sorted(f for f in src.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not images:
        _fail(f"no images in {src}")
    try:
        manual = bl.load_manual(_resolve(boxes, root)) if boxes else {}
    except (OSError, ValueError) as e:
        _fail(f"boxes {boxes}: {e}")
    detector = None
    if not no_detect:
        from parking.vision.detector import YoloDetector

        path = _resolve(model, root)
        if not path.exists():
            _fail(f"model {path} not found; run `parking models export` or pass --no-detect")
        detector = YoloDetector(str(path), imgsz, conf, ["person"], use_masks=False)
    log = bl.load_log(src)
    people = hand = touched = 0
    for f in images:
        done = log.get(f.name, [])
        done_manual = {tuple(e["box"]) for e in done if e["by"] == "manual"}
        new_manual = [b for b in manual.get(f.name, []) if tuple(b) not in done_manual]
        detect = detector is not None and not any(e["by"] == "detector" for e in done)
        if not (new_manual or detect):
            continue
        image = cv2.imread(str(f))
        if image is None:
            _fail(f"can't read {f}")
        entries = []
        if detect:
            found = [d for d in detector.detect(image) if d.cls == "person"]
            entries += [
                {"by": "detector", "box": [round(v) for v in d.box], "conf": round(d.conf, 2)}
                for d in found
            ] or [{"by": "detector", "box": None}]  # checked, nobody in it
        entries += [{"by": "manual", "box": list(b)} for b in new_manual]
        regions = [bl.grow(e["box"], image.shape) for e in entries if e["box"] is not None]
        people += sum(1 for e in entries if e["by"] == "detector" and e["box"] is not None)
        hand += len(new_manual)
        for r in regions:
            typer.echo(f"{f.name}: blur {r}")
        if not dry_run:
            if regions and not cv2.imwrite(str(f), bl.blur_boxes(image, regions)):
                _fail(f"can't write {f}")
            log[f.name] = done + entries
            touched += bool(regions)
    if not dry_run:
        bl.save_log(src, log)
    verb = "would blur" if dry_run else "blurred"
    typer.echo(
        f"{len(images)} image(s): {people} person box(es), {hand} hand-drawn box(es); "
        f"{verb} {touched if not dry_run else people + hand} -> {src}"
    )


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

    from parking.workers.base import WorkerError, load_settings
    from parking.workers.occupancy_worker import OccupancyWorker

    if not 0 <= control_port <= 65535:
        raise typer.BadParameter("must be 0..65535", param_hint="--control-port")
    if max_frames < 0:
        raise typer.BadParameter("must be >= 0", param_hint="--max-frames")

    config = _find_config(config)
    settings = load_settings(config.resolve().parent.parent)
    _worker_logging(settings.log_level)
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


@worker_app.command("flow")
def worker_flow(
    camera: Annotated[str, typer.Option(help="Flow camera id in the config, e.g. cam-ramp.")],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    print_mode: Annotated[
        bool,
        typer.Option(
            "--print", help="Print flow events and health as JSON lines instead of sending."
        ),
    ] = False,
    debug_video: Annotated[
        Path | None,
        typer.Option(
            help="Write every frame annotated (ROI, lines, boxes, track ids, running IN/OUT) "
            "to this MP4."
        ),
    ] = None,
    control_port: Annotated[
        int, typer.Option(help="Port of the /control/* server (0 = off).")
    ] = 9000,
    control_host: Annotated[str, typer.Option(help="Address the control server binds.")] = (
        "0.0.0.0"
    ),
    max_frames: Annotated[int, typer.Option(help="Stop after this many frames (0 = never).")] = 0,
) -> None:
    """Run the flow worker: gate + track + two-line count every frame and send in/out events."""

    from parking.workers.base import WorkerError, load_settings
    from parking.workers.flow_worker import FlowWorker

    if not 0 <= control_port <= 65535:
        raise typer.BadParameter("must be 0..65535", param_hint="--control-port")
    if max_frames < 0:
        raise typer.BadParameter("must be >= 0", param_hint="--max-frames")

    config = _find_config(config)
    settings = load_settings(config.resolve().parent.parent)
    _worker_logging(settings.log_level)
    try:
        worker = FlowWorker(
            config,
            camera,
            debug_video=debug_video,
            print_mode=print_mode,
            settings=settings,
            control_port=control_port or None,
            control_host=control_host,
        )
        worker.run(max_frames or None)
    except WorkerError as e:
        _fail(str(e))


@worker_app.command("barrier")
def worker_barrier(
    camera: Annotated[
        str, typer.Option(help="Barrier id in the config (a cameras entry with role: barrier).")
    ],
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    print_mode: Annotated[
        bool,
        typer.Option(
            "--print", help="Print flow events and health as JSON lines instead of sending."
        ),
    ] = False,
    max_events: Annotated[int, typer.Option(help="Stop after this many cars (0 = never).")] = 0,
) -> None:
    """Run the barrier worker: read the barrier's contacts and send one in/out event per car."""

    from parking.workers.barrier_worker import BarrierWorker
    from parking.workers.base import WorkerError, load_settings

    if max_events < 0:
        raise typer.BadParameter("must be >= 0", param_hint="--max-events")

    config = _find_config(config)
    settings = load_settings(config.resolve().parent.parent)
    _worker_logging(settings.log_level)
    try:
        worker = BarrierWorker(config, camera, print_mode=print_mode, settings=settings)
        worker.run(max_events or None)
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
    """`(lot configs, engine)` for the maintenance commands; the DB is upgraded first."""
    from parking.config import ConfigError, cli_env, load_lots
    from parking.db.engine import default_url, make_engine, upgrade

    config = _find_config(config)
    root = config.resolve().parent.parent
    try:
        lots = load_lots(config, cli_env(root))
    except (ConfigError, ValueError) as e:
        _fail(f"{config}: {e}")
    url = url or default_url(root)
    try:
        upgrade(url)
    except Exception as e:
        _fail(f"upgrade failed: {e}")
    return lots, make_engine(url)


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

    lots, engine = _db_engine(config, url)
    now = SystemClock().now()
    jobs = MaintenanceJobs.for_lots(engine, lots, SystemClock())
    try:
        if backfill:
            with session_scope(engine) as session:
                minutes, hours = rollups.backfill(session, jobs.zone_ids, now, totals=jobs.totals)
        else:
            minutes, hours = jobs.run_minutes(now), jobs.run_hours(now)
    finally:
        engine.dispose()
    typer.echo(f"zone_minute: {minutes} row(s) written, zone_hour: {hours} row(s) written")


def _forecast_table(rows) -> None:
    typer.echo(
        f"{'zone':<20} {'hours':>6} {'held out':>8} {'MAE model':>9} {'MAE median':>10}  used"
    )
    for r in rows:
        model = "-" if r.mae_model is None else f"{r.mae_model:.2f}"
        median = "-" if r.mae_median is None else f"{r.mae_median:.2f}"
        typer.echo(
            f"{r.zone_id:<20} {r.rows:>6} {r.holdout_rows:>8} {model:>9} {median:>10}  "
            f"{'model' if r.active else 'median'} ({r.note})"
        )


@forecast_app.command("train")
def forecast_train(
    config: Annotated[Path, typer.Option(help="lot.yaml.")] = DEFAULT_CONFIG,
    url: DbUrl = None,
) -> None:
    """Refresh the weather, then train and check the forecast models now (the API does it
    every night at 03:30). A model is used only if it beats the median on the last 4 weeks."""
    from parking.api.jobs import MaintenanceJobs
    from parking.core.clock import SystemClock

    lots, engine = _db_engine(config, url)
    try:
        reports = MaintenanceJobs.for_lots(engine, lots, SystemClock()).run_forecast(
            SystemClock().now()
        )
    finally:
        engine.dispose()
    if not reports:
        typer.echo("forecast.model is off in lot.yaml: nothing trained")
        return
    _forecast_table(reports)


@forecast_app.command("status")
def forecast_status(
    config: Annotated[Path, typer.Option(help="lot.yaml.")] = DEFAULT_CONFIG,
    url: DbUrl = None,
) -> None:
    """Show each zone's last training: the model's and the median's error on the held-out
    4 weeks (mean absolute error, in spaces) and which one `/api/forecast` uses."""
    from sqlmodel import select

    from parking.db.engine import session_scope
    from parking.db.models import ForecastModel

    _, engine = _db_engine(config, url)
    try:
        with session_scope(engine) as session:
            rows = session.exec(select(ForecastModel).order_by(ForecastModel.zone_id)).all()
            if not rows:
                typer.echo("no forecast model trained yet")
                return
            typer.echo(f"last trained: {max(r.trained_at for r in rows):%Y-%m-%d %H:%M} UTC")
            _forecast_table(rows)
    finally:
        engine.dispose()


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

    lots, engine = _db_engine(config, url)
    retention = Retention(
        raw=timedelta(days=raw_days),
        minute=timedelta(days=minute_days),
        log=timedelta(days=log_days),
    )
    try:
        deleted = MaintenanceJobs.for_lots(engine, lots, SystemClock(), retention).run_prune(
            SystemClock().now()
        )
        if vacuum:
            vacuum_db(engine)
    finally:
        engine.dispose()
    for table, count in deleted.items():
        typer.echo(f"{table}: {count} row(s) deleted")


@db_app.command("retention")
def db_retention(
    raw_days: Annotated[
        float, typer.Option(min=0, help="Period of slot_state, zone_state, flow_event.")
    ] = 90,
    minute_days: Annotated[float, typer.Option(min=0, help="Period of zone_minute.")] = 30,
    log_days: Annotated[float, typer.Option(min=0, help="Period of notification_log.")] = 30,
    config: Annotated[Path, typer.Option(help="lot.yaml to use.")] = DEFAULT_CONFIG,
    url: DbUrl = None,
) -> None:
    """Check that the retention jobs delete: the oldest row of every table and the rows past
    their period (exit 1 if there are any). Deletes nothing."""
    from datetime import timedelta

    from parking.core.clock import SystemClock
    from parking.db.engine import session_scope
    from parking.db.rollups import Retention, retention_report

    _, engine = _db_engine(config, url)
    retention = Retention(
        raw=timedelta(days=raw_days),
        minute=timedelta(days=minute_days),
        log=timedelta(days=log_days),
    )
    now = SystemClock().now()
    try:
        with session_scope(engine) as session:
            report = retention_report(session, now, retention)
    finally:
        engine.dispose()
    for r in report:
        if r.oldest is None:
            oldest = "empty"
        else:
            oldest = f"oldest {r.oldest:%Y-%m-%d %H:%M} ({(now - r.oldest).days} d)"
        verdict = f"OVERDUE {r.overdue} row(s)" if r.overdue else "ok"
        typer.echo(f"{r.table:18} {r.rows:>8} row(s)  {oldest:32} {r.keep}: {verdict}")
    overdue = sum(r.overdue for r in report)
    if overdue:
        _fail(f"retention: {overdue} row(s) past their period, the prune job isn't deleting")
    typer.echo("retention: ok, nothing is kept past its period")


def _backup_paths(config: Path, url: str | None) -> tuple[Path, Path, Path]:
    """`(database file, config/, data/reference/)` under the app root."""
    from parking.db.backup import sqlite_path
    from parking.db.engine import default_url

    root = _find_config(config).resolve().parent.parent
    return sqlite_path(url or default_url(root)), root / "config", root / "data" / "reference"


@app.command()
def backup(
    out: Annotated[Path, typer.Option(help="Folder for the archive.")],
    keep_daily: Annotated[
        int, typer.Option(min=0, help="Rotate: keep the newest archive of this many days.")
    ] = 14,
    keep_weekly: Annotated[
        int, typer.Option(min=0, help="Rotate: keep the newest archive of this many weeks.")
    ] = 8,
    no_rotate: Annotated[bool, typer.Option("--no-rotate", help="Delete no old archive.")] = False,
    config: Annotated[Path, typer.Option(help="lot.yaml of the app to back up.")] = DEFAULT_CONFIG,
    url: DbUrl = None,
) -> None:
    """Write parking-YYYYMMDD-HHMM.tar.gz (UTC): the database, config/ and the reference images.

    Safe while the API runs (SQLite online backup). Then old archives in the folder are rotated.
    """
    from parking.core.clock import SystemClock
    from parking.db.backup import BackupError, create_backup, rotate

    try:
        db, config_dir, reference_dir = _backup_paths(config, url)
        path = create_backup(db, config_dir, reference_dir, out, SystemClock().now())
        deleted = [] if no_rotate else rotate(out, keep_daily, keep_weekly)
    except (BackupError, OSError) as e:
        _fail(f"backup failed: {e}")
    typer.echo(f"backup written: {path} ({path.stat().st_size} bytes)")
    for old in deleted:
        typer.echo(f"rotated out: {old.name}")


@app.command()
def restore(
    archive: Annotated[Path, typer.Argument(help="A parking-YYYYMMDD-HHMM.tar.gz backup.")],
    config: Annotated[
        Path, typer.Option(help="lot.yaml of the app to restore into (it may not exist yet).")
    ] = DEFAULT_CONFIG,
    url: DbUrl = None,
) -> None:
    """Put a backup's database, config/ and reference images in place. Stop the API first.

    The archive is checked first (checksums, database integrity). An existing database is renamed
    to <name>.before-restore-<time>; config files that differ are kept as <file>.bak.
    """
    from parking.core.clock import SystemClock
    from parking.db.backup import BackupError, restore_backup

    try:
        db, config_dir, reference_dir = _backup_paths(config, url)
        result = restore_backup(archive, db, config_dir, reference_dir, SystemClock().now())
    except (BackupError, OSError) as e:
        _fail(f"restore failed: {e}")
    m = result.manifest
    typer.echo(
        f"restored {archive.name} (made {m.get('created')} by {m.get('app_version')}, "
        f"database revision {m.get('db_revision')})"
    )
    typer.echo(f"database: {result.db}")
    if result.previous_db:
        typer.echo(f"previous database kept as: {result.previous_db}")
    typer.echo(f"{len(result.files)} config/reference file(s) written")
    for kept in result.kept:
        typer.echo(f"previous file kept as: {kept}")


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
