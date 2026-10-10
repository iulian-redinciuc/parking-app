"""Compose hardening + deploy/scripts/boot-check.sh (deployment.md §4.1, P8.4) and the watchdog
(§4.2, P8.5)."""

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
        assert re.fullmatch(r"(cloudflare/cloudflared|willfarrell/autoheal):\d+\.\d+\.\d+", image)


WORKERS = [
    pytest.param(f, s, id=f"{f}:{s}")
    for f in (*RUNTIME_FILES, "docker-compose.test.yml")
    for s in _services(f)
    if s.startswith("vision-")
]


@pytest.mark.parametrize(("file", "name"), WORKERS)
def test_worker_healthcheck_is_the_loop_heartbeat(file, name):
    worker = _services(file)[name]
    check = worker["healthcheck"]
    assert check["test"] == [
        "CMD",
        "/app/backend/.venv/bin/python",
        "-m",
        "parking.workers.heartbeat",
    ]
    if file != "docker-compose.test.yml":
        # stuck -> stale after >= 20 s -> unhealthy two checks later -> restarted: about a minute
        assert (check["interval"], check["retries"]) == ("10s", 2)
        assert worker["labels"] == {"parking.autoheal": "true"}


def test_the_vision_image_writes_the_heartbeat_where_the_check_reads_it():
    from parking.workers.heartbeat import DEFAULT_PATH

    dockerfile = (ROOT / "backend" / "Dockerfile").read_text()
    assert f"ENV HEARTBEAT_FILE={DEFAULT_PATH}" in dockerfile
    env = _services("docker-compose.test.yml")["vision-occupancy"]["environment"]
    assert env["HEARTBEAT_FILE"] == str(DEFAULT_PATH)


@pytest.mark.parametrize("file", ["docker-compose.yml", "docker-compose.site.yml"])
def test_autoheal_restarts_only_labelled_parking_containers(file):
    services = _services(file)
    autoheal = services["autoheal"]
    assert autoheal["container_name"] == "parking-autoheal"
    assert autoheal["environment"]["AUTOHEAL_CONTAINER_LABEL"] == "parking.autoheal"
    assert autoheal["network_mode"] == "none" and "profiles" not in autoheal
    assert autoheal["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock"]
    assert autoheal["group_add"][0].startswith("${DOCKER_GID:?")
    labelled = {n for n, s in services.items() if "parking.autoheal" in s.get("labels", {})}
    assert labelled == {"vision-occupancy", "vision-flow"}


def test_no_docker_socket_anywhere_else():
    for file in (*RUNTIME_FILES, "docker-compose.test.yml"):
        for name, service in _services(file).items():
            if name != "autoheal":
                assert "docker.sock" not in str(service), f"{file}:{name}"


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


# --- a camera plugged into the vision host (P4.12, deployment.md §4.3) ---

CAMERA_OVERRIDES = ["docker-compose.device.yml", "docker-compose.picamera.yml"]


@pytest.mark.parametrize("file", CAMERA_OVERRIDES)
def test_camera_overrides_keep_the_hardening(file):
    services = _services(file)
    assert list(services) == ["vision-occupancy"]  # opt-in, and only the worker that needs it
    svc = services["vision-occupancy"]
    assert svc["group_add"] == ["${VIDEO_GID:-44}"]
    # nothing that would undo the base file's lock-down
    assert not {"privileged", "cap_add", "user", "read_only", "security_opt", "cap_drop"} & set(svc)
    assert "docker.sock" not in (DEPLOY / file).read_text()


def test_device_override_passes_one_device():
    svc = _services("docker-compose.device.yml")["vision-occupancy"]
    assert svc["devices"] == ["${CAMERA_DEVICE:-/dev/video0}:${CAMERA_DEVICE:-/dev/video0}"]
    assert "volumes" not in svc and "device_cgroup_rules" not in svc


def test_picamera_override_admits_only_camera_device_kinds():
    svc = _services("docker-compose.picamera.yml")["vision-occupancy"]
    assert svc["volumes"] == ["/dev:/dev", "/run/udev:/run/udev:ro"]
    rules = svc["device_cgroup_rules"]
    assert rules[0] == "c 81:* rmw" and len(rules) == 3
    assert rules[1].startswith("c ${MEDIA_MAJOR:?") and rules[2].startswith("c ${DMA_HEAP_MAJOR:?")
    assert all("*:*" not in r and r.startswith("c ") for r in rules)
    # its own image, built on the box on top of the released vision image
    assert svc["image"].startswith("parking-vision-picamera:")
    assert svc["build"]["dockerfile"] == "Dockerfile.picamera"
    dockerfile = (DEPLOY / "Dockerfile.picamera").read_text()
    assert "rpicam-apps-lite" in dockerfile and "signed-by=" in dockerfile
    assert dockerfile.rstrip().endswith("USER 1000:1000")
