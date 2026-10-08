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


def _occupancy_camera(config: Path, camera: str, command: str):
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
    cam = cams[camera]
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

    lot, cam, root = _occupancy_camera(config, camera, "analyze")

    # CLI flags override the config for experiments
    occ = cam.occupancy.model_copy(
        update={k: v for k, v in {"threshold": threshold, "mode": mode}.items() if v is not None}
    )
    det_cfg = cam.detector.model_copy(update={"imgsz": imgsz} if imgsz else {})
    cam = cam.model_copy(update={"occupancy": occ, "detector": det_cfg})

    slot_file = _slot_file(cam, root)
    capacities = {z: lot.zone_capacity(z, [slot_file]) for z in cam.zones}

    image = _resolve(image, root)
    frame = cv2.imread(str(image))
    if frame is None:
        _fail(f"can't read image {image}")

    if _fake_detector_on(fake_detector):
        from parking.vision.detector import FakeDetector

        sidecar = image.with_suffix(".json")
        if not sidecar.is_file():
            _fail(f"fake detector: no sidecar {sidecar}")
        detector = FakeDetector(sidecar, conf=det_cfg.conf, classes=det_cfg.classes)
    else:
        detector = _yolo_detector(det_cfg, root)

    result = analyze_frame(frame, cam, slot_file, detector, capacities)
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
    images: Annotated[Path, typer.Option(help="Folder of labelled images.")] = Path("data/samples"),
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

    lot, cam, root = _occupancy_camera(config, camera, "evaluate")
    det_cfg = cam.detector.model_copy(update={"imgsz": imgsz} if imgsz else {})
    threshold = threshold if threshold is not None else cam.occupancy.threshold
    modes = ["mask", "box_bottom"] if mode == "both" else [mode or cam.occupancy.mode]
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
    if not folder.is_dir():
        _fail(f"image folder {folder} not found")
    found = ev.list_images(folder)
    paths = [p for p in found if p.name in label_file.images]
    skipped = [p.name for p in found if p.name not in label_file.images]
    missing = sorted(set(label_file.images) - {p.name for p in found})
    if skipped:
        typer.echo(f"not labelled, skipped: {', '.join(skipped)}")
    if missing:
        typer.echo(f"labelled but not in {folder}: {', '.join(missing)}")
    if not paths:
        _fail(f"no labelled images in {folder}")

    fake = _fake_detector_on(fake_detector)
    if fake:
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
