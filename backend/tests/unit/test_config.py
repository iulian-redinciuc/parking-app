"""Config loader: lot.yaml, slot/line files, env interpolation and Settings (config.md)."""

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from parking.config import (
    ConfigError,
    LotConfig,
    Settings,
    SlotFile,
    cli_env,
    interpolate_env,
    load_config,
    load_lines,
    load_slots,
)

REPO = Path(__file__).resolve().parents[3]
LOT_FILES = [REPO / "config" / "lot.example.yaml", REPO / "config" / "lot.yaml"]
ENV = {
    "LOT_LAT": "51.5007",
    "LOT_LON": "-0.1246",
    "TZ": "Europe/Bucharest",
    "CAM_RAMP_RTSP_URL": "rtsp://cam/sub",
}


@pytest.fixture
def env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def raw(env) -> dict:
    return yaml.safe_load(interpolate_env((REPO / "config" / "lot.example.yaml").read_text()))


def invalid(raw: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        LotConfig.model_validate(raw)


@pytest.mark.parametrize("path", LOT_FILES, ids=lambda p: p.name)
def test_committed_lot_files_load(env, path):
    config = load_config(path)
    assert config.lot.location.lat == pytest.approx(51.5007)
    assert config.lot.timezone == "Europe/Bucharest"
    assert [z.id for z in config.zones] == ["ground", "underground"]
    ground = config.camera("cam-ground")
    assert ground.slots_file == Path("config/slots/cam-ground.json")
    assert ground.occupancy.threshold == 0.30
    assert config.camera("cam-ramp").source == "rtsp:rtsp://cam/sub"


# --- env interpolation ---


def test_interpolation_replaces_vars():
    assert interpolate_env("a: ${FOO}\nb: ${BAR_2}\n", {"FOO": "1", "BAR_2": "x"}) == "a: 1\nb: x\n"


def test_interpolation_missing_var_is_an_error():
    with pytest.raises(ConfigError, match="line 2: .*MISSING"):
        interpolate_env("a: 1\nb: ${MISSING}\n", {})


def test_interpolation_skips_commented_lines():
    text = "  # Phase 4: ${NOT_SET}\na: 1\n"
    assert interpolate_env(text, {}) == text


def test_load_config_missing_env_var(monkeypatch):
    monkeypatch.delenv("CAM_RAMP_RTSP_URL", raising=False)
    for key in ("LOT_LAT", "LOT_LON", "TZ"):
        monkeypatch.setenv(key, ENV[key])
    with pytest.raises(ConfigError, match="CAM_RAMP_RTSP_URL"):
        load_config(LOT_FILES[0])


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path / "nope.yaml")


# --- loader rules ---


def test_duplicate_zone_id(raw):
    raw["zones"].append(dict(raw["zones"][0]))
    invalid(raw, "duplicate zone id.*ground")


def test_duplicate_camera_id(raw):
    raw["cameras"][1]["id"] = "cam-ground"
    invalid(raw, "duplicate camera id.*cam-ground")


@pytest.mark.parametrize("bad", ["Ground", "cam_1", "a b"])
def test_ids_must_match_pattern(raw, bad):
    raw["zones"][0]["id"] = bad
    invalid(raw, r"zones\.0\.id")


def test_camera_unknown_zone(raw):
    raw["cameras"][0]["zones"] = ["roof"]
    invalid(raw, "camera 'cam-ground': unknown zone 'roof'")


def test_slots_zone_needs_occupancy_camera(raw):
    raw["cameras"] = [c for c in raw["cameras"] if c["role"] != "occupancy"]
    invalid(raw, "zone 'ground': 'slots' zones need an occupancy camera")


def test_flow_zone_needs_exactly_one_flow_camera(raw):
    second = dict(raw["cameras"][1], id="cam-ramp-2")
    raw["cameras"].append(second)
    invalid(raw, "zone 'underground': 'flow' zones need exactly one flow camera, found 2")
    raw["cameras"] = raw["cameras"][:1]
    invalid(raw, "found 0")


@pytest.mark.parametrize("method", ["count", "flow"])
def test_capacity_required_for_count_and_flow(raw, method):
    raw["zones"][1]["capacity"] = None
    raw["zones"][1]["method"] = method
    raw["zones"][1].pop("reset")
    invalid(raw, f"zone 'underground': capacity is required for '{method}' zones")


def test_reset_only_for_flow_zones(raw):
    raw["zones"][0]["reset"] = {"enabled": True}
    invalid(raw, "reset is only allowed for 'flow' zones")


def test_occupancy_camera_needs_slots_file(raw):
    del raw["cameras"][0]["slots_file"]
    invalid(raw, "occupancy cameras need slots_file")


def test_flow_camera_needs_lines_file(raw):
    del raw["cameras"][1]["lines_file"]
    invalid(raw, "flow cameras need lines_file")


@pytest.mark.parametrize("threshold", [0, 1, 1.5, -0.1])
def test_threshold_between_0_and_1(raw, threshold):
    raw["cameras"][0]["occupancy"]["threshold"] = threshold
    invalid(raw, r"occupancy\.threshold")


def test_occupancy_method_and_appearance(raw):
    occ = LotConfig.model_validate(raw).camera("cam-ground").occupancy
    assert occ.method == "detector"
    assert occ.appearance.k_mad == 1.5
    raw["cameras"][0]["occupancy"]["method"] = "magic"
    invalid(raw, r"occupancy\.method")
    raw["cameras"][0]["occupancy"]["method"] = "appearance"
    raw["cameras"][0]["occupancy"]["appearance"]["shadow_l_range"] = [0.9, 0.2]
    invalid(raw, "shadow_l_range")


def test_committed_ground_camera_uses_appearance(env):
    assert load_config(REPO / "config" / "lot.yaml").camera("cam-ground").occupancy.method == (
        "appearance"
    )


def test_consistent_readings_at_least_1(raw):
    raw["cameras"][0]["smoothing"]["consistent_readings"] = 0
    invalid(raw, r"smoothing\.consistent_readings")


def test_unknown_key_is_an_error(raw):
    raw["cameras"][0]["occupancy"]["treshold"] = 0.3
    invalid(raw, r"occupancy\.treshold")


# --- slot and line files ---

SLOTS = {
    "version": 1,
    "camera_id": "cam-ground",
    "image_size": [2560, 1440],
    "reference_image": "data/reference/cam-ground.jpg",
    "slots": [
        {"id": "G01", "zone": "ground", "polygon": [[0, 0], [100, 0], [100, 200], [0, 200]]},
        {
            "id": "G02",
            "zone": "ground",
            "polygon": [[100, 0], [200, 0], [200, 200], [100, 200]],
            "type": "accessible",
        },
    ],
    "count_zones": [{"zone": "ground", "polygon": [[0, 600], [2560, 600], [2560, 1440]]}],
}


def write_json(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "file.json"
    path.write_text(json.dumps(data))
    return path


def test_load_slots(tmp_path):
    slots = load_slots(write_json(tmp_path, SLOTS))
    assert [s.id for s in slots.slots] == ["G01", "G02"]
    assert slots.slots[0].type == "standard"
    assert slots.slots[1].type == "accessible"


def test_slots_scaled():
    slots = SlotFile.model_validate(SLOTS)
    scaled = slots.scaled(1280, 720)
    assert scaled[0].polygon == [(0, 0), (50, 0), (50, 100), (0, 100)]
    assert scaled[1].id == "G02"
    assert slots.slots[0].polygon[1] == (100, 0)  # original unchanged


def test_slots_scaled_per_axis():
    scaled = SlotFile.model_validate(SLOTS).scaled(5120, 720)
    assert scaled[0].polygon[2] == (200, 100)


def test_slot_polygon_needs_3_points():
    data = json.loads(json.dumps(SLOTS))
    data["slots"][0]["polygon"] = [[0, 0], [1, 1]]
    with pytest.raises(ValidationError, match="at least 3 points"):
        SlotFile.model_validate(data)


def test_slot_polygon_self_intersecting():
    data = json.loads(json.dumps(SLOTS))
    data["slots"][0]["polygon"] = [[0, 0], [100, 100], [100, 0], [0, 100]]
    with pytest.raises(ValidationError, match="self-intersecting"):
        SlotFile.model_validate(data)


def test_duplicate_slot_id():
    data = json.loads(json.dumps(SLOTS))
    data["slots"][1]["id"] = "G01"
    with pytest.raises(ValidationError, match="duplicate slot id.*G01"):
        SlotFile.model_validate(data)


def test_zone_capacity(env):
    config = load_config(LOT_FILES[0])
    slots = SlotFile.model_validate(SLOTS)
    assert config.zone_capacity("ground", [slots]) == 2
    assert config.zone_capacity("underground", [slots]) == 60


def test_missing_slot_file_is_clear(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_slots(tmp_path / "cam-ground.json")


def test_load_lines(tmp_path):
    data = {
        "version": 1,
        "camera_id": "cam-ramp",
        "image_size": [640, 360],
        "roi": [[60, 120], [600, 120], [600, 360], [60, 360]],
        "line_a": [[100, 220], [560, 220]],
        "line_b": [[100, 270], [560, 270]],
        "in_direction": "a_to_b",
    }
    lines = load_lines(write_json(tmp_path, data))
    assert lines.line_a == ((100, 220), (560, 220))
    data["in_direction"] = "up"
    with pytest.raises(ValidationError, match="in_direction"):
        load_lines(write_json(tmp_path, data))


# --- Settings (.env) ---


def test_settings_all_optional(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.worker_token is None
    assert settings.parking_config == Path("config/lot.yaml")


def test_settings_reads_env_and_ignores_empty(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKER_TOKEN", "abc")
    monkeypatch.setenv("ADMIN_TOKEN", "")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("DEBUG_CAPTURE", "true")
    settings = Settings()
    assert settings.worker_token.get_secret_value() == "abc"
    assert settings.admin_token is None
    assert settings.cors_origin_list == ["https://a.example", "https://b.example"]
    assert settings.debug_capture is True


def test_settings_cover_every_env_example_variable():
    names = set()
    for line in (REPO / "deploy" / ".env.example").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            names.add(line.split("=", 1)[0].strip().lower())
    assert names - Settings.model_fields.keys() == set()


def test_cli_env_layers_example_dotenv_and_environment(tmp_path, monkeypatch):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / ".env.example").write_text("# c\nA=example\nB=example\nC=example\n")
    (tmp_path / "deploy" / ".env").write_text("B='dotenv'\nC=dotenv\n\nnot a pair\n")
    monkeypatch.setenv("C", "process")
    env = cli_env(tmp_path)
    assert (env["A"], env["B"], env["C"]) == ("example", "dotenv", "process")


def test_cli_env_without_files_is_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ONLY_HERE", "1")
    assert cli_env(tmp_path)["ONLY_HERE"] == "1"


def test_load_config_with_cli_env_from_repo(monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    cfg = load_config(REPO / "config" / "lot.yaml", cli_env(REPO))
    assert cfg.camera("cam-ground").role == "occupancy"
