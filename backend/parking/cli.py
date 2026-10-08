"""The `parking` command line. Commands are listed in docs/design/architecture.md §6."""

from pathlib import Path
from typing import Annotated

import typer

from parking import __version__

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
