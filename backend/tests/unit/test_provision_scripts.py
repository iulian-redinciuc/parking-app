"""deploy/scripts/provision.sh + prod-env.sh, T2 compose files (deployment.md §4, §9, §10)."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).parents[3] / "deploy"
PROVISION = DEPLOY / "scripts" / "provision.sh"
PROD_ENV = DEPLOY / "scripts" / "prod-env.sh"

pytestmark = pytest.mark.skipif(
    not Path("/etc/os-release").exists(), reason="the scripts are for Linux machines"
)


def _provision(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PROVISION), *args], env={**os.environ, "DRY_RUN": "1"}, capture_output=True, text=True
    )


def _prod_env(tmp_path: Path, role: str, **env: str) -> subprocess.CompletedProcess:
    fake_cli = tmp_path / "parking"
    fake_cli.write_text(
        "#!/bin/sh\necho VAPID_PUBLIC_KEY=pub-key\necho VAPID_PRIVATE_KEY=priv-key\n"
        "echo Put both in deploy/.env\n"
    )
    fake_cli.chmod(0o755)
    base = {k: v for k, v in os.environ.items() if k not in ("WORKER_TOKEN", "PARKING_VERSION")}
    base |= {"ENV_FILE": str(tmp_path / ".env"), "PARKING_CLI": str(fake_cli)}
    return subprocess.run([str(PROD_ENV), role], env=base | env, capture_output=True, text=True)


def _read_env(path: Path) -> dict[str, str]:
    lines = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
    return dict(ln.split("=", 1) for ln in lines)


def test_provision_server_dry_run():
    r = _provision("server", "--peer-key", "SITEKEY=", "--version", "v0.1.0")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "+ ufw default deny incoming" in out
    assert "+ ufw allow 51820/udp" in out
    assert "ufw allow 80/tcp" not in out and "ufw allow 443/tcp" not in out
    assert "PasswordAuthentication no" in out
    assert 'APT::Periodic::Unattended-Upgrade "1";' in out
    assert "Address = 10.77.0.1/24" in out and "ListenPort = 51820" in out
    assert "PublicKey = SITEKEY=" in out and "AllowedIPs = 10.77.0.2/32" in out
    assert "After=wg-quick@wg0.service" in out
    assert "/opt/parking/config /opt/parking/data /opt/parking/models" in out
    assert "config/ of v0.1.0 " in out


def test_provision_server_public_proxy_opens_web_ports():
    out = _provision("server", "--public-proxy").stdout
    assert "+ ufw allow 80/tcp" in out and "+ ufw allow 443/tcp" in out
    assert "the VPN is not configured" in out and "wg0.conf" not in out


def test_provision_site_dry_run():
    r = _provision("site", "--peer-key", "SERVERKEY=", "--endpoint", "vm.example.org")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "Address = 10.77.0.2/24" in out and "ListenPort" not in out
    assert "Endpoint = vm.example.org:51820" in out and "PersistentKeepalive = 25" in out
    # the lot box dials out: only SSH is open from outside the VPN
    allowed = [ln for ln in out.splitlines() if ln.startswith("+ ufw allow")]
    assert allowed == [
        "+ ufw allow 22/tcp",
        "+ ufw allow in on wg0 to any port 9000:9001 proto tcp",
    ]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["both"],
        ["site", "--peer-key", "K="],  # no --endpoint
        ["site", "--public-proxy"],
        ["server", "--version", "latest"],
        ["server", "--nope"],
    ],
)
def test_provision_rejects_bad_arguments(args):
    r = _provision(*args)
    assert r.returncode == 1
    assert r.stderr.startswith("provision: ") and "+ " not in r.stdout


def test_prod_env_server_has_fresh_secrets(tmp_path):
    r = _prod_env(tmp_path, "server", PARKING_VERSION="v0.1.0")
    assert r.returncode == 0, r.stderr
    env_file = tmp_path / ".env"
    assert env_file.stat().st_mode & 0o777 == 0o600
    env = _read_env(env_file)
    assert env.keys() == _read_env(DEPLOY / ".env.example").keys()
    assert len(env["WORKER_TOKEN"]) == 64 and len(env["ADMIN_TOKEN"]) == 64
    assert env["WORKER_TOKEN"] != env["ADMIN_TOKEN"]
    assert env["VAPID_PUBLIC_KEY"] == "pub-key" and env["VAPID_PRIVATE_KEY"] == "priv-key"
    assert env["PARKING_VERSION"] == "v0.1.0" and env["VPN_BIND_IP"] == "10.77.0.1"
    for secret in (env["WORKER_TOKEN"], env["ADMIN_TOKEN"], "priv-key"):
        assert secret not in r.stdout + r.stderr
    assert "ADMIN_PASSWORD_HASH" in r.stdout

    again = _prod_env(tmp_path, "server", PARKING_VERSION="v0.1.0")
    assert again.returncode == 1 and "not overwriting" in again.stderr
    assert _read_env(env_file) == env


def test_prod_env_site_takes_the_servers_worker_token(tmp_path):
    token = "ab" * 32
    r = _prod_env(tmp_path, "site", PARKING_VERSION="v0.1.0", WORKER_TOKEN=token)
    assert r.returncode == 0, r.stderr
    env = _read_env(tmp_path / ".env")
    assert env["WORKER_TOKEN"] == token and token not in r.stdout
    assert env["API_INTERNAL_URL"] == "http://10.77.0.1:8000" and env["VPN_BIND_IP"] == "10.77.0.2"
    # the API's secrets stay off the lot box
    assert env["ADMIN_TOKEN"] == env["VAPID_PRIVATE_KEY"] == env["ADMIN_PASSWORD_HASH"] == ""


@pytest.mark.parametrize(
    ("role", "env"),
    [
        ("server", {}),
        ("server", {"PARKING_VERSION": "latest"}),
        ("site", {"PARKING_VERSION": "v0.1.0"}),
        ("site", {"PARKING_VERSION": "v0.1.0", "WORKER_TOKEN": "short"}),
    ],
)
def test_prod_env_refuses_without_a_pinned_version_or_token(tmp_path, role, env):
    r = _prod_env(tmp_path, role, **env)
    assert r.returncode == 1 and r.stderr.startswith("prod-env: ")
    assert not (tmp_path / ".env").exists()


def _compose(name: str) -> dict:
    return yaml.safe_load((DEPLOY / name).read_text())


def test_server_compose_publishes_the_api_on_loopback_and_vpn_only():
    compose = _compose("docker-compose.server.yml")
    assert compose["name"] == "parking"
    assert set(compose["services"]) == {"api", "tunnel"}
    ports = compose["services"]["api"]["ports"]
    assert len(ports) == 2
    assert ports[0].startswith("127.0.0.1:") and ports[1].startswith("${VPN_BIND_IP:?")
    assert compose["services"]["tunnel"]["profiles"] == ["public"]
    assert "ports" not in compose["services"]["tunnel"]


def test_site_compose_has_only_the_workers_on_the_vpn_address():
    compose = _compose("docker-compose.site.yml")
    assert compose["name"] == "parking"
    services = compose["services"]
    assert set(services) == {"vision-occupancy", "vision-flow"}
    assert services["vision-flow"]["profiles"] == ["flow"]
    for name, host_port in (("vision-occupancy", 9000), ("vision-flow", 9001)):
        (port,) = services[name]["ports"]
        assert port.startswith("${VPN_BIND_IP:?") and port.endswith(f":{host_port}:9000")
        assert services[name]["environment"]["API_INTERNAL_URL"].startswith("${API_INTERNAL_URL:?")
        assert "depends_on" not in services[name]
        assert ":latest" not in services[name]["image"]
