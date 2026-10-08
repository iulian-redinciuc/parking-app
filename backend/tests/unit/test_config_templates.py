"""Checks the committed config templates (config/lot*.yaml, deploy/.env.example)."""

import re
from pathlib import Path

import pytest
import yaml

from parking.config import load_slots

REPO = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO / "deploy" / ".env.example"
LOT_FILES = [REPO / "config" / "lot.example.yaml", REPO / "config" / "lot.yaml"]
SECRETS = [
    "WORKER_TOKEN",
    "CAM_GROUND_SNAPSHOT_URL",
    "CAM_GROUND_RTSP_URL",
    "CAM_RAMP_RTSP_URL",
    "VAPID_PUBLIC_KEY",
    "VAPID_PRIVATE_KEY",
    "ADMIN_PASSWORD_HASH",
    "ADMIN_TOKEN",
    "TUNNEL_TOKEN",
]
VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


def read_env_example() -> dict[str, str]:
    env = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def spec_env_vars() -> set[str]:
    spec = (REPO / "docs" / "design" / "config.md").read_text()
    section = spec.split("## 5. Environment variables", 1)[1].split("Frontend build-time", 1)[0]
    names = set()
    for row in section.splitlines():
        if row.startswith("| `"):
            names.update(re.findall(r"`([A-Z][A-Z0-9_]+)`", row.split("|")[1]))
    return names


def test_env_example_has_every_spec_variable():
    assert spec_env_vars() - read_env_example().keys() == set()


def test_env_example_secrets_are_empty():
    env = read_env_example()
    for name in SECRETS:
        assert env[name] == "", f"{name} must be empty in .env.example"


@pytest.mark.parametrize("path", LOT_FILES, ids=lambda p: p.name)
def test_lot_yaml_parses_with_env_example(path: Path):
    env = read_env_example()
    lines = []
    for line in path.read_text().splitlines():
        code = line.split("#", 1)[0]
        for name in VAR.findall(code):
            assert name in env, f"{path.name} uses ${{{name}}}, missing from .env.example"
        lines.append(VAR.sub(lambda m: env[m.group(1)] or "x", code))
    config = yaml.safe_load("\n".join(lines))
    assert config["version"] == 1
    zone_ids = {z["id"] for z in config["zones"]}
    for camera in config["cameras"]:
        assert set(camera["zones"]) <= zone_ids


SLOT_FILES = sorted((REPO / "config" / "slots").glob("*.json"))


@pytest.mark.parametrize("path", SLOT_FILES, ids=lambda p: p.name)
def test_committed_slot_files_load(path: Path):
    slots = load_slots(path)
    assert slots.camera_id == path.stem
    assert slots.slots, "a committed slot file should have spaces"
    lot = yaml.safe_load(VAR.sub("x", (REPO / "config" / "lot.yaml").read_text()))
    camera = next(c for c in lot["cameras"] if c["id"] == slots.camera_id)
    assert camera["slots_file"] == f"config/slots/{path.name}"
    assert {s.zone for s in slots.slots} <= set(camera["zones"])
