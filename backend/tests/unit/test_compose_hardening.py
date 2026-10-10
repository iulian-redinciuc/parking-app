"""Compose hardening + deploy/scripts/boot-check.sh (deployment.md §4.1, P8.4)."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[3]
DEPLOY = ROOT / "deploy"
BOOT_CHECK = DEPLOY / "scripts" / "boot-check.sh"
RUNTIME_FILES = ("docker-compose.yml", "docker-compose.server.yml", "docker-compose.site.yml")


def _services(name: str) -> dict:
    return yaml.safe_load((DEPLOY / name).read_text())["services"]


def _all(*files: str) -> list:
    return [pytest.param(f, s, id=f"{f}:{s}") for f in files for s in _services(f)]


@pytest.mark.parametrize(("file", "name"), _all(*RUNTIME_FILES, "docker-compose.test.yml"))
def test_every_service_is_locked_down(file, name):
    service = _services(file)[name]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    uid, gid = service["user"].split(":")
    assert int(uid) > 0 and int(gid) > 0
    assert "privileged" not in service and "cap_add" not in service
    assert service["healthcheck"]["test"][0] == "CMD"
    if "cloudflared" not in service["image"]:  # the tunnel writes nothing
        assert service["tmpfs"] == ["/tmp:size=64m"]


@pytest.mark.parametrize(("file", "name"), _all(*RUNTIME_FILES))
def test_every_service_restarts_and_has_limits(file, name):
    service = _services(file)[name]
    assert service["restart"] == "unless-stopped"
    limits = service["deploy"]["resources"]["limits"]
    assert limits["cpus"] and limits["memory"]
    assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}


def test_limits_come_from_the_env_file():
    template = (DEPLOY / ".env.example").read_text()
    used = set()
    for file in RUNTIME_FILES:
        used |= set(re.findall(r"\$\{(\w+_(?:CPUS|MEMORY)):-", (DEPLOY / file).read_text()))
    assert used == {
        "API_CPUS", "API_MEMORY", "VISION_CPUS", "VISION_MEMORY",
        "FLOW_CPUS", "FLOW_MEMORY", "WEB_CPUS", "WEB_MEMORY",
    }  # fmt: skip
    for name in used:
        assert re.search(rf"^{name}=\S+$", template, re.M), name


@pytest.mark.parametrize(("file", "name"), _all(*RUNTIME_FILES))
def test_image_tags_are_pinned(file, name):
    image = _services(file)[name]["image"]
    if image.startswith("ghcr.io/iulian-redinciuc/parking-"):
        tag = image.split(":", 1)[1]
        assert tag.startswith("${PARKING_VERSION")
        if file != "docker-compose.yml":  # production files: no default at all
            assert "latest" not in tag
    else:
        assert re.fullmatch(r"cloudflare/cloudflared:\d{4}\.\d+\.\d+", image)


def test_dependabot_watches_the_images():
    updates = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())["updates"]
    watched = {(u["package-ecosystem"], u["directory"]) for u in updates}
    assert {
        ("docker-compose", "/deploy"),
        ("docker", "/backend"),
        ("docker", "/frontend"),
    } <= watched


def test_web_image_runs_caddy_as_uid_1000():
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text()
    assert "USER 1000:1000" in dockerfile and "setcap -r /usr/bin/caddy" in dockerfile
    web = _services("docker-compose.server.yml")["web"]
    assert web["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": 0}


FAKE_DOCKER = """#!/bin/sh
case "$1 $2" in
  "ps -aq") echo abc123 ;;
  "ps -a") printf '%b' "$FAKE_PS" ;;
  "ps --filter") echo "  listed" ;;
  "exec -e") [ -z "$FAKE_API_DOWN" ] || exit 1; echo "$3" >>"$FAKE_LOG"; echo "$FAKE_STALE" ;;
esac
"""
HEALTHY = "parking-api running Up 20 seconds (healthy)\\nparking-tunnel running Up 9 seconds\\n"


def _boot_check(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    base = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "log"),
        "FAKE_PS": HEALTHY,
        "BOOT_LIMIT_S": "999999999",
        "BOOT_WAIT_S": "0",
    }
    return subprocess.run([str(BOOT_CHECK), *args], env=base | env, capture_output=True, text=True)


linux = pytest.mark.skipif(not Path("/proc/uptime").exists(), reason="reads /proc/uptime")


@linux
def test_boot_check_ready(tmp_path):
    r = _boot_check(tmp_path, "server", "ground")
    assert r.returncode == 0, r.stderr
    assert re.match(r"ready \d+ s after boot \(limit 999999999 s\)\n", r.stdout)
    assert (tmp_path / "log").read_text() == "ZONES=ground\n"


@linux
def test_boot_check_site_does_not_ask_the_api(tmp_path):
    r = _boot_check(tmp_path, "site")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "log").exists()


@linux
@pytest.mark.parametrize(
    ("env", "waiting_for"),
    [
        ({"FAKE_STALE": "underground"}, "stale: underground"),
        ({"FAKE_API_DOWN": "1"}, "stale: api-unreachable"),
        ({"FAKE_PS": "parking-api running Up 2 s (health: starting)\\n"}, "parking-api(running)"),
        ({"FAKE_PS": "parking-api running Up 2 minutes (unhealthy)\\n"}, "parking-api(running)"),
        ({"FAKE_PS": "parking-api exited Exited (1) 5 seconds ago\\n"}, "parking-api(exited)"),
    ],
)
def test_boot_check_reports_what_it_waits_for(tmp_path, env, waiting_for):
    r = _boot_check(tmp_path, "server", **env)
    assert r.returncode == 1
    assert f"still waiting for: {waiting_for}" in r.stderr


@linux
def test_boot_check_fails_past_the_limit(tmp_path):
    r = _boot_check(tmp_path, "server", BOOT_LIMIT_S="0")
    assert r.returncode == 1
    assert "ready" in r.stdout and "later than the limit" in r.stderr


def test_boot_check_usage(tmp_path):
    assert _boot_check(tmp_path, "everything").returncode == 2
