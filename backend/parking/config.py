"""Config models and loaders: lot.yaml, slot/line files and .env settings.

Spec: docs/design/config.md (§1 lot.yaml + loader rules, §2 slot file, §3 line file,
§4 ground-truth labels, §5 .env).
"""

import os
import re
from pathlib import Path
from typing import Annotated, Literal, Self, get_args

import shapely
import yaml
from apscheduler.triggers.cron import CronTrigger
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")
ID_PATTERN = r"^[a-z0-9-]+$"

Id = Annotated[str, Field(pattern=ID_PATTERN)]
Point = tuple[float, float]
Size = tuple[Annotated[int, Field(gt=0)], Annotated[int, Field(gt=0)]]


class ConfigError(ValueError):
    """The config can't be read: a missing file or a missing ${VAR}."""


class Strict(BaseModel):
    """Unknown keys are errors, so typos in the YAML/JSON don't pass silently."""

    model_config = ConfigDict(extra="forbid")


def _check_polygon(points: list[Point]) -> list[Point]:
    if len(points) < 3:
        raise ValueError("a polygon needs at least 3 points")
    if not shapely.LinearRing(points).is_simple:
        raise ValueError("polygon is self-intersecting")
    return points


Polygon = Annotated[list[Point], AfterValidator(_check_polygon)]


# --- lot.yaml (config.md §1) ---


class Location(Strict):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Lot(Strict):
    id: Id
    name: str
    location: Location
    notify_radius_m: int = Field(default=500, gt=0)
    timezone: str


_CRON_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def crontab_trigger(expr: str, timezone) -> CronTrigger:
    """A 5-field crontab expression as an APScheduler trigger. APScheduler 3 numbers weekdays
    from 0 = Monday, crontab from 0 = Sunday, so numeric weekdays become names first."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 fields, got {len(fields)}")
    minute, hour, day, month, dow = fields
    dow = re.sub(r"(?<![/\d])[0-7](?!\d)", lambda m: _CRON_DAYS[int(m.group())], dow)
    return CronTrigger(
        minute=minute, hour=hour, day=day, month=month, day_of_week=dow, timezone=timezone
    )


def _crontab(value: str) -> str:
    try:
        crontab_trigger(value, "UTC")
    except ValueError as e:
        raise ValueError(f"not a 5-field crontab expression: {e}") from None
    return value


class ResetCfg(Strict):
    """A flow zone's scheduled reset (P5.7): set the count to `value` at `cron`, lot time."""

    enabled: bool = False
    cron: Annotated[str, AfterValidator(_crontab)] = "0 3 * * *"
    value: int = Field(default=0, ge=0)


class Zone(Strict):
    id: Id
    name: dict[str, str] = Field(min_length=1)
    method: Literal["slots", "count", "flow"]
    capacity: int | None = Field(default=None, gt=0)
    reset: ResetCfg | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.method in ("count", "flow") and self.capacity is None:
            raise ValueError(f"zone '{self.id}': capacity is required for '{self.method}' zones")
        if self.reset is not None and self.method != "flow":
            raise ValueError(f"zone '{self.id}': reset is only allowed for 'flow' zones")
        if (
            self.reset is not None
            and self.capacity is not None
            and self.reset.value > self.capacity
        ):
            raise ValueError(f"zone '{self.id}': reset.value is over the capacity")
        return self

    def display_name(self, lang: str = "en") -> str:
        """The name in `lang`, else English, else the first one given."""
        return self.name.get(lang) or self.name.get("en") or next(iter(self.name.values()))


class DetectorCfg(Strict):
    runtime: Literal["ncnn", "openvino", "onnx", "engine", "hailo"] = "ncnn"
    model: str
    imgsz: int = Field(default=640, gt=0)
    conf: float = Field(default=0.35, gt=0, lt=1)
    classes: list[str] = Field(default_factory=lambda: ["car", "motorcycle", "bus", "truck"])
    use_masks: bool = False


class AppearanceCfg(Strict):
    """Top-down appearance scoring parameters (vision.md §2.1)."""

    inset: float = Field(default=0.12, ge=0, lt=0.5)
    k_mad: float = Field(default=1.5, gt=0)
    min_delta_e: float = Field(default=12, ge=0)
    shadow_l_range: tuple[float, float] = (0.35, 0.9)
    shadow_chroma_max: float = Field(default=2, ge=0)
    morph_frac: float = Field(default=0.06, gt=0, lt=1)
    reference_empty: Path | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        lo, hi = self.shadow_l_range
        if not 0 <= lo <= hi:
            raise ValueError("shadow_l_range must be [low, high] with 0 <= low <= high")
        return self


class ClassifierCfg(Strict):
    """Per-slot classifier (vision.md §9), for `occupancy.method: classifier | ensemble`."""

    model: Path = Path("models/slot_classifier.onnx")
    threshold: float = Field(default=0.5, gt=0, lt=1)  # on P(taken), or the ensemble's mean


OccupancyMethod = Literal["detector", "appearance", "classifier", "ensemble"]
OCCUPANCY_METHODS: tuple[str, ...] = get_args(OccupancyMethod)


class OccupancyCfg(Strict):
    method: OccupancyMethod = "detector"
    threshold: float = Field(default=0.30, gt=0, lt=1)
    mode: Literal["mask", "box_bottom"] = "mask"
    appearance: AppearanceCfg = Field(default_factory=AppearanceCfg)
    classifier: ClassifierCfg = Field(default_factory=ClassifierCfg)

    @property
    def uses_detector(self) -> bool:
        return self.method == "detector"

    @property
    def uses_appearance(self) -> bool:
        return self.method in ("appearance", "ensemble")

    @property
    def uses_classifier(self) -> bool:
        return self.method in ("classifier", "ensemble")

    @property
    def decision_threshold(self) -> float:
        """The cut-off on the slot score: the classifier's for classifier/ensemble."""
        return self.classifier.threshold if self.uses_classifier else self.threshold


class SmoothingCfg(Strict):
    consistent_readings: int = Field(default=3, ge=1)


class HealthCfg(Strict):
    black_mean_max: float = 12
    frozen_diff_max: float = 0.5
    frozen_frames: int = Field(default=6, ge=1)
    blur_laplacian_min: float = 40
    shift_check_every_s: int = Field(default=300, gt=0)
    shift_max_px: float = 8


class FlowCfg(Strict):
    min_track_frames: int = Field(default=5, ge=1)
    motion_min_area_px: int = Field(default=1500, ge=0)


class Camera(Strict):
    id: Id
    role: Literal["occupancy", "flow"]
    zones: list[str] = Field(min_length=1)
    source: str
    control_url: str | None = None
    detector: DetectorCfg
    # occupancy cameras
    sample_every_s: float = Field(default=5, gt=0)
    slots_file: Path | None = None
    occupancy: OccupancyCfg = Field(default_factory=OccupancyCfg)
    smoothing: SmoothingCfg = Field(default_factory=SmoothingCfg)
    health: HealthCfg = Field(default_factory=HealthCfg)
    # flow cameras
    fps: float = Field(default=10, gt=0)
    lines_file: Path | None = None
    flow: FlowCfg = Field(default_factory=FlowCfg)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.role == "occupancy" and self.slots_file is None:
            raise ValueError(f"camera '{self.id}': occupancy cameras need slots_file")
        if self.role == "flow" and self.lines_file is None:
            raise ValueError(f"camera '{self.id}': flow cameras need lines_file")
        return self


class Levels(Strict):
    plenty: float = Field(default=0.20, gt=0, lt=1)
    filling: float = Field(default=0.05, ge=0, lt=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.filling >= self.plenty:
            raise ValueError("levels: filling must be below plenty")
        return self


class ApiCfg(Strict):
    stale_after_s: int = Field(default=60, gt=0)
    sse_ping_s: int = Field(default=15, gt=0)
    trend_window_min: int = Field(default=15, gt=0)
    levels: Levels = Field(default_factory=Levels)


class LotConfig(Strict):
    version: Literal[1]
    lot: Lot
    zones: list[Zone] = Field(min_length=1)
    cameras: list[Camera] = Field(default_factory=list)
    api: ApiCfg = Field(default_factory=ApiCfg)

    @model_validator(mode="after")
    def _cross_check(self) -> Self:
        _require_unique("zone", [z.id for z in self.zones])
        _require_unique("camera", [c.id for c in self.cameras])
        zone_ids = {z.id for z in self.zones}
        for camera in self.cameras:
            for zone_id in camera.zones:
                if zone_id not in zone_ids:
                    raise ValueError(f"camera '{camera.id}': unknown zone '{zone_id}'")
        for zone in self.zones:
            roles = [c.role for c in self.cameras if zone.id in c.zones]
            if zone.method == "slots" and "occupancy" not in roles:
                raise ValueError(f"zone '{zone.id}': 'slots' zones need an occupancy camera")
            if zone.method == "flow" and roles.count("flow") != 1:
                raise ValueError(
                    f"zone '{zone.id}': 'flow' zones need exactly one flow camera, "
                    f"found {roles.count('flow')}"
                )
        return self

    def zone(self, zone_id: str) -> Zone:
        return next(z for z in self.zones if z.id == zone_id)

    def camera(self, camera_id: str) -> Camera:
        return next(c for c in self.cameras if c.id == camera_id)

    def zone_capacity(self, zone_id: str, slot_files: list["SlotFile"]) -> int:
        """The zone's capacity; for `slots` zones without one, the number of its slots."""
        zone = self.zone(zone_id)
        if zone.capacity is not None:
            return zone.capacity
        return sum(1 for f in slot_files for s in f.slots if s.zone == zone_id)


def _require_unique(kind: str, ids: list[str]) -> None:
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate {kind} id(s): {', '.join(dupes)}")


# --- slot file (config.md §2) ---


class Slot(Strict):
    id: str = Field(min_length=1)
    zone: str
    polygon: Polygon
    type: Literal["standard", "accessible", "ev", "motorcycle", "reserved"] = "standard"


class CountZone(Strict):
    zone: str
    polygon: Polygon


class SlotFile(Strict):
    version: Literal[1]
    camera_id: str
    image_size: Size
    reference_image: str | None = None
    slots: list[Slot] = Field(default_factory=list)
    count_zones: list[CountZone] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require_unique("slot", [s.id for s in self.slots])
        return self

    def ids(self) -> list[str]:
        return [s.id for s in self.slots]

    def scaled(self, frame_w: int, frame_h: int) -> list[Slot]:
        """Slots with polygons rescaled from `image_size` to the frame size (both axes)."""
        sx, sy = frame_w / self.image_size[0], frame_h / self.image_size[1]
        return [
            s.model_copy(update={"polygon": [(x * sx, y * sy) for x, y in s.polygon]})
            for s in self.slots
        ]


# --- line file (config.md §3) ---


class LineFile(Strict):
    version: Literal[1]
    camera_id: str
    image_size: Size
    roi: Polygon | None = None
    line_a: tuple[Point, Point]
    line_b: tuple[Point, Point]
    in_direction: Literal["a_to_b", "b_to_a"] = "a_to_b"


# --- ground-truth labels (config.md §4) ---


class ImageLabels(Strict):
    conditions: list[str] = Field(default_factory=list)
    taken: list[str] = Field(default_factory=list)
    unsure: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        both = sorted(set(self.taken) & set(self.unsure))
        if both:
            raise ValueError(f"slot(s) both taken and unsure: {', '.join(both)}")
        return self


class LabelFile(Strict):
    version: Literal[1]
    camera_id: str
    images: dict[str, ImageLabels] = Field(default_factory=dict)


# --- loaders ---


def interpolate_env(text: str, env: dict[str, str] | None = None) -> str:
    """Replace `${VAR}` from the environment; lines starting with `#` are left alone."""
    env = os.environ if env is None else env
    out = []
    for n, line in enumerate(text.splitlines(keepends=True), start=1):
        if not line.lstrip().startswith("#"):
            missing = [v for v in ENV_VAR.findall(line) if v not in env]
            if missing:
                names = ", ".join(missing)
                raise ConfigError(f"line {n}: environment variable(s) not set: {names}")
            line = ENV_VAR.sub(lambda m: env[m.group(1)], line)
        out.append(line)
    return "".join(out)


def _read(path: str | Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"file not found: {path}")
    return path.read_text()


def load_config(path: str | Path, env: dict[str, str] | None = None) -> LotConfig:
    """Load lot.yaml; `${VAR}` comes from `env` (default: the process environment)."""
    return LotConfig.model_validate(yaml.safe_load(interpolate_env(_read(path), env)))


def read_env_file(path: str | Path) -> dict[str, str]:
    """`KEY=VALUE` lines of a .env file (comments and blank lines skipped); {} if missing."""
    path = Path(path)
    if not path.is_file():
        return {}
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    return env


def cli_env(root: str | Path) -> dict[str, str]:
    """`${VAR}` values for CLI tools run outside Docker.

    `deploy/.env.example` < `deploy/.env` < the process environment, so a dev checkout
    without a `.env` still loads lot.yaml (the example has placeholders, no secrets).
    """
    deploy = Path(root) / "deploy"
    return {
        **read_env_file(deploy / ".env.example"),
        **read_env_file(deploy / ".env"),
        **os.environ,
    }


def load_slots(path: str | Path) -> SlotFile:
    return SlotFile.model_validate_json(_read(path))


def load_lines(path: str | Path) -> LineFile:
    return LineFile.model_validate_json(_read(path))


def load_labels(path: str | Path) -> LabelFile:
    return LabelFile.model_validate_json(_read(path))


# --- .env (config.md §5) ---


class Settings(BaseSettings):
    """Every `.env` variable; all optional for now. Empty values count as unset."""

    model_config = SettingsConfigDict(
        env_file="deploy/.env", env_ignore_empty=True, extra="ignore", case_sensitive=False
    )

    tz: str | None = None
    parking_config: Path = Path("config/lot.yaml")
    lot_lat: float | None = None
    lot_lon: float | None = None
    parking_db_url: str | None = None
    worker_token: SecretStr | None = None
    api_internal_url: str | None = None
    api_host_port: int = 8000
    cam_ground_snapshot_url: SecretStr | None = None
    cam_ground_rtsp_url: SecretStr | None = None
    cam_ramp_rtsp_url: SecretStr | None = None
    cors_origins: str = ""
    public_app_url: str | None = None
    vapid_public_key: str | None = None
    vapid_private_key: SecretStr | None = None
    vapid_subject: str | None = None
    admin_password_hash: SecretStr | None = None
    admin_token: SecretStr | None = None
    debug_capture: bool = False
    debug_capture_every_min: float = Field(default=10, gt=0)
    debug_retention_hours: float = Field(default=24, gt=0)
    public_host: str | None = None
    tunnel_token: SecretStr | None = None
    log_level: str = "INFO"
    parking_version: str = "latest"
    vpn_bind_ip: str | None = None
    vision_cpus: float | None = None
    flow_cpus: float | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
