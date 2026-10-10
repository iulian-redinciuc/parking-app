"""deploy/scripts/security-check.sh (P8.8, security-privacy.md §2): the on-machine checks."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parents[3] / "deploy"
SCRIPT = DEPLOY / "scripts" / "security-check.sh"
TOKEN = "a1" * 32
HASH = "'$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21lc2FsdA$" + "x" * 43 + "'"

pytestmark = pytest.mark.skipif(
    not Path("/etc/os-release").exists(), reason="the script is for Linux machines"
)


def _env(tmp_path: Path, mode: int = 0o600, **values: str) -> Path:
    base = {
        "LOG_LEVEL": "INFO",
        "WORKER_TOKEN": TOKEN,
        "ADMIN_TOKEN": "b2" * 32,
        "ADMIN_PASSWORD_HASH": HASH,
        "VAPID_PRIVATE_KEY": "k" * 43,
        "PUBLIC_HOST": "parking.example.org",
        "CORS_ORIGINS": "https://parking.example.org",
        "VPN_BIND_IP": "10.77.0.2",
    }
    path = tmp_path / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in (base | values).items()))
    path.chmod(mode)
    return path


def _run(tmp_path: Path, mode: str, env_file: Path, ports: str = "", **env: str):
    """The script with a fake `curl` (401 for /internal and /control, else 404) and `docker`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(
        '#!/bin/sh\ncase "$*" in */internal/*|*/control/*) printf 401;; *) printf 404;; esac\n'
    )
    (tmp_path / "ports").write_text(ports + "\n")
    (bin_dir / "docker").write_text(f"#!/bin/sh\ncat {tmp_path / 'ports'}\n")
    for tool in ("curl", "docker"):
        (bin_dir / tool).chmod(0o755)
    path = f"{bin_dir}:{os.environ['PATH']}"
    return subprocess.run(
        [str(SCRIPT), mode],
        env={"PATH": path, "ENV_FILE": str(env_file)} | env,
        capture_output=True,
        text=True,
    )


SERVER_PORTS = (
    "parking-api 127.0.0.1:8000->8000/tcp, 10.77.0.1:8000->8000/tcp\n"
    "parking-web 0.0.0.0:80->80/tcp, [::]:80->80/tcp, 0.0.0.0:443->443/tcp, 0.0.0.0:443->443/udp"
)


def test_usage_without_a_mode():
    r = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 2 and "security-check.sh outside https://" in r.stderr


def test_outside_needs_an_https_address():
    r = subprocess.run([str(SCRIPT), "outside", "http://example.org"], capture_output=True)
    assert r.returncode == 2


def test_server_passes_on_a_production_env(tmp_path):
    r = _run(tmp_path, "server", _env(tmp_path), SERVER_PORTS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all checks passed" in r.stdout
    assert "ok    /internal/health without a token: 401" in r.stdout
    assert "ok    CORS_ORIGINS: https://parking.example.org" in r.stdout
    assert TOKEN not in r.stdout  # never prints a secret


@pytest.mark.parametrize(
    ("mode", "values", "message"),
    [
        (0o644, {}, "mode: got '644', wanted 600"),
        (0o600, {"ADMIN_TOKEN": "short"}, "ADMIN_TOKEN is missing or shorter"),
        (0o600, {"ADMIN_PASSWORD_HASH": ""}, "ADMIN_PASSWORD_HASH is missing"),
        (0o600, {"LOG_LEVEL": "debug"}, "LOG_LEVEL=DEBUG"),
        (0o600, {"DEBUG_CAPTURE": "true"}, "DEBUG_CAPTURE=true"),
        (0o600, {"CORS_ORIGINS": "*"}, "CORS_ORIGINS: got '*'"),
    ],
)
def test_server_fails_on_a_weak_env(tmp_path, mode, values, message):
    r = _run(tmp_path, "server", _env(tmp_path, mode, **values), SERVER_PORTS)
    assert r.returncode == 1
    assert f"FAIL  {message}" in r.stdout.replace(str(tmp_path / ".env") + " ", "")


@pytest.mark.parametrize(
    "ports",
    [
        "parking-api 0.0.0.0:8000->8000/tcp",
        "parking-api 127.0.0.1:8000->8000/tcp, [::]:8000->8000/tcp",
        "parking-web 0.0.0.0:443->443/tcp, 0.0.0.0:2019->2019/tcp",
    ],
)
def test_a_port_published_on_every_interface_fails(tmp_path, ports):
    r = _run(tmp_path, "server", _env(tmp_path), ports)
    assert r.returncode == 1 and "on every interface" in r.stdout


def test_fingerprints_and_secrets_equal_to_dev(tmp_path):
    env_file = _env(tmp_path)
    r = _run(tmp_path, "fingerprints", env_file)
    assert r.returncode == 0
    assert f"WORKER_TOKEN {hashlib.sha256(TOKEN.encode()).hexdigest()}" in r.stdout.splitlines()
    assert TOKEN not in r.stdout
    dev = tmp_path / "dev.fp"
    dev.write_text(r.stdout)

    same = _run(tmp_path, "server", env_file, SERVER_PORTS, DEV_FINGERPRINTS=str(dev))
    assert same.returncode == 1 and "FAIL  WORKER_TOKEN is the dev machine's" in same.stdout

    new = {"WORKER_TOKEN": "c3" * 32, "ADMIN_TOKEN": "d4" * 32, "VAPID_PRIVATE_KEY": "z" * 43}
    fresh = _env(tmp_path, **new)
    r = _run(tmp_path, "server", fresh, SERVER_PORTS, DEV_FINGERPRINTS=str(dev))
    assert "ok    WORKER_TOKEN differs from dev" in r.stdout
    assert "FAIL  ADMIN_PASSWORD_HASH is the dev machine's" in r.stdout  # still the same hash


def test_site_checks_the_control_ports(tmp_path):
    ports = "parking-vision-occupancy 10.77.0.2:9000->9000/tcp\nparking-autoheal "
    r = _run(tmp_path, "site", _env(tmp_path), ports)
    assert r.returncode == 0, r.stdout
    assert "ok    /control/snapshot on 10.77.0.2:9000 without a token: 401" in r.stdout


def test_the_proxy_forbids_framing_the_app():
    caddyfile = (DEPLOY / "Caddyfile").read_text()
    assert "Content-Security-Policy \"frame-ancestors 'none'\"" in caddyfile
