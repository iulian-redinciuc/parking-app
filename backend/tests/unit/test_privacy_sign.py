"""deploy/scripts/privacy-sign.sh (P8.9, security-privacy.md §4.1): the sign from the .env."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[3] / "deploy" / "scripts" / "privacy-sign.sh"

pytestmark = pytest.mark.skipif(
    not Path("/etc/os-release").exists(), reason="the script is for Linux machines"
)


def _run(env_file: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PRIVACY_", "PUBLIC_HOST"))}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=clean | {"ENV_FILE": str(env_file)} | env,
        capture_output=True,
        text=True,
    )


def _env(tmp_path: Path, **values: str) -> Path:
    path = tmp_path / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def test_fills_the_sign_from_the_env_file_and_escapes_it(tmp_path):
    env = _env(
        tmp_path,
        PRIVACY_OPERATOR="'Lot <Owner> & Co, Str. Exemplu 1'",
        PRIVACY_CONTACT="privacy@example.org",
        PUBLIC_HOST="parking.example.org",
    )
    out = _run(env)
    assert out.returncode == 0, out.stderr
    html = out.stdout
    assert '<html lang="en">' in html
    assert "Camera in use: counting free parking spaces" in html
    assert "no recording, no number-plate reading" in html
    assert "Lot &lt;Owner&gt; &amp; Co, Str. Exemplu 1" in html  # quotes removed, escaped
    assert "privacy@example.org" in html
    assert "https://parking.example.org/#/privacy" in html
    assert "@page { size: A4;" in html


def test_romanian_and_the_environment_wins(tmp_path):
    env = _env(tmp_path, PRIVACY_OPERATOR="x", PRIVACY_CONTACT="y", PUBLIC_HOST="a.example.org")
    out = _run(env, "ro", PUBLIC_HOST="b.example.org")
    assert out.returncode == 0, out.stderr
    assert '<html lang="ro">' in out.stdout
    assert "Zonă supravegheată video" in out.stdout
    assert "https://b.example.org/#/privacy" in out.stdout


def test_refuses_a_sign_without_operator_or_contact(tmp_path):
    out = _run(_env(tmp_path, PRIVACY_OPERATOR="", PUBLIC_HOST="a.example.org"))
    assert out.returncode == 1
    assert out.stdout == ""
    assert "PRIVACY_OPERATOR PRIVACY_CONTACT" in out.stderr


def test_unknown_language(tmp_path):
    env = _env(tmp_path, PRIVACY_OPERATOR="x", PRIVACY_CONTACT="y", PUBLIC_HOST="a.example.org")
    out = _run(env, "de")
    assert out.returncode == 1
    assert "unknown language" in out.stderr
