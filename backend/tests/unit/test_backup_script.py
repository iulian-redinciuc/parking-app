"""deploy/backup.sh with stand-ins for docker and rclone (deployment.md §7), plus the cron entry.

With a real rclone (on PATH or `RCLONE_BIN`) one test also goes through an actual crypt remote.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parents[3] / "deploy"
SCRIPT = DEPLOY / "backup.sh"
ARCHIVE = "parking-20261010-0130.tar.gz"

pytestmark = pytest.mark.skipif(
    not Path("/etc/os-release").exists(), reason="the scripts are for Linux machines"
)

# docker: `exec` writes an archive like the API container would, `run` records the restore call
FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >>"$FAKE_LOG"
case "$1" in
  exec) [ -z "$FAKE_BACKUP_FAILS" ] || exit 1
        mkdir -p "$BACKUP_ROOT/data/backups"
        echo archive >"$BACKUP_ROOT/data/backups/$FAKE_ARCHIVE" ;;
  ps) [ -z "$FAKE_API_RUNNING" ] || echo abc123 ;;
  inspect) [ -n "$FAKE_IMAGE" ] || exit 1; echo "$FAKE_IMAGE" ;;
esac
"""

# rclone: the remote "parking-backup:" is the folder $FAKE_REMOTE_DIR, its type $FAKE_REMOTE_TYPE
FAKE_RCLONE = """#!/bin/bash
shift 2                                   # --config FILE
echo "rclone $*" >>"$FAKE_LOG"
path() {
  case "$1" in parking-backup:*) echo "$FAKE_REMOTE_DIR/${1#*:}" ;; *) echo "$1" ;; esac
}
cmd=$1; shift
include='*'
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --include) include=$2; shift 2 ;;
    --backup-dir | --min-age) shift 2 ;;
    --files-only | --long) shift ;;
    *) args+=("$(path "$1")"); shift ;;
  esac
done
case "$cmd" in
  listremotes) [ -z "$FAKE_REMOTE_TYPE" ] || echo "parking-backup: $FAKE_REMOTE_TYPE" ;;
  copy) [ -z "$FAKE_UPLOAD_FAILS" ] || exit 1
        mkdir -p "${args[1]}"
        for f in "${args[0]}"/$include; do [ ! -f "$f" ] || cp "$f" "${args[1]}/"; done ;;
  copyto) mkdir -p "$(dirname "${args[1]}")" && cp "${args[0]}" "${args[1]}" ;;
  lsf) for f in "${args[0]}"/$include; do [ ! -f "$f" ] || basename "$f"; done ;;
  delete) ;;
esac
"""


@pytest.fixture
def box(tmp_path: Path) -> dict[str, str]:
    """A machine: app root with deploy/.env + rclone.conf, fake docker/rclone, an empty remote."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("rclone", FAKE_RCLONE)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    root = tmp_path / "root"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / ".env").write_text("PARKING_VERSION=v0.2.0\nVAPID_PRIVATE_KEY=dummy\n")
    (root / "deploy" / "rclone.conf").write_text("[parking-backup]\ntype = crypt\n")
    (tmp_path / "remote").mkdir()
    keep = {k: v for k, v in os.environ.items() if not k.startswith(("BACKUP_", "FAKE_", "RCLONE"))}
    return keep | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BACKUP_ROOT": str(root),
        "BACKUP_ENV_FILE": str(root / "deploy" / ".env"),
        "BACKUP_RCLONE_CONFIG": str(root / "deploy" / "rclone.conf"),
        "BACKUP_ENV_NAME": "server",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_REMOTE_DIR": str(tmp_path / "remote"),
        "FAKE_REMOTE_TYPE": "crypt",
        "FAKE_ARCHIVE": ARCHIVE,
        "FAKE_IMAGE": "ghcr.io/iulian-redinciuc/parking-api:v0.2.0",
    }


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True)


def _log(env: dict[str, str]) -> str:
    path = Path(env["FAKE_LOG"])
    return path.read_text() if path.exists() else ""


def test_nightly_run_uploads_archive_and_env(box):
    r = _run(box)
    assert r.returncode == 0, r.stderr
    remote = Path(box["FAKE_REMOTE_DIR"])
    assert (remote / "backups" / ARCHIVE).exists()
    assert "VAPID_PRIVATE_KEY=dummy" in (remote / "env" / "server.env").read_text()
    log = _log(box)
    assert (
        "docker exec parking-api /app/backend/.venv/bin/parking backup --out /app/data/backups"
        in log
    )
    assert "--backup-dir parking-backup:env-old/" in log  # a replaced .env is kept
    assert "rclone delete --min-age 60d --include parking-*.tar.gz parking-backup:backups" in log
    assert f"{ARCHIVE} and .env copied to parking-backup (encrypted)" in r.stdout


def _last_run(env: dict[str, str]) -> dict:
    return json.loads((Path(env["BACKUP_ROOT"]) / "data" / "backups" / "last-run.json").read_text())


def test_run_leaves_its_outcome_for_the_backup_failed_alert(box):
    """`data/backups/last-run.json` (notifications.md §5.1): ok, local_only or failed."""
    assert _run(box).returncode == 0
    last = _last_run(box)
    assert last["status"] == "ok" and last["message"] == ""
    assert abs(datetime.now(UTC) - datetime.fromisoformat(last["ts"])) < timedelta(minutes=1)
    assert _run(box | {"FAKE_REMOTE_TYPE": ""}).returncode == 3
    assert _last_run(box) | {"ts": ""} == {
        "ts": "",
        "status": "local_only",
        "message": f'not copied off this machine (no remote "parking-backup" in '
        f"{box['BACKUP_RCLONE_CONFIG']})",
    }
    assert _run(box | {"FAKE_UPLOAD_FAILS": "1"}).returncode == 1
    assert _last_run(box)["status"] == "failed"
    assert _last_run(box)["message"] == "the upload failed"
    assert _run(box | {"FAKE_BACKUP_FAILS": "1"}).returncode == 1
    assert _last_run(box)["message"] == "the local backup failed"
    # list / env / restore are not runs
    before = _last_run(box)
    _run(box, "list")
    assert _last_run(box) == before


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"FAKE_REMOTE_TYPE": "sftp"}, "is of type sftp, not crypt: refusing to upload"),
        ({"FAKE_REMOTE_TYPE": ""}, 'no remote "parking-backup"'),
        ({"BACKUP_RCLONE_CONFIG": "/nonexistent/rclone.conf"}, "no /nonexistent/rclone.conf"),
        ({"RCLONE": "rclone-not-installed"}, "rclone is not installed"),
    ],
)
def test_run_without_an_encrypted_remote_keeps_the_local_archive_and_exits_3(box, change, message):
    r = _run(box | change)
    assert r.returncode == 3
    assert "NOT copied off this machine" in r.stderr and message in r.stderr
    assert (Path(box["BACKUP_ROOT"]) / "data" / "backups" / ARCHIVE).exists()
    assert not list(Path(box["FAKE_REMOTE_DIR"]).iterdir())  # nothing left unencrypted


def test_run_fails_when_the_backup_or_the_upload_fails(box):
    r = _run(box | {"FAKE_BACKUP_FAILS": "1"})
    assert r.returncode == 1 and "the local backup failed" in r.stderr
    assert "rclone" not in _log(box)
    r = _run(box | {"FAKE_UPLOAD_FAILS": "1"})
    assert r.returncode == 1 and "the upload failed" in r.stderr


def test_list(box):
    _run(box)
    r = _run(box, "list")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count(ARCHIVE) == 2 and "  server\n" in r.stdout
    r = _run(box | {"FAKE_REMOTE_TYPE": ""}, "list")
    assert r.returncode == 0 and "Remote: not available" in r.stdout


def test_env_fetch_never_overwrites(box):
    _run(box)
    env_file = Path(box["BACKUP_ENV_FILE"])
    r = _run(box, "env")
    assert r.returncode == 1 and "not overwriting" in r.stderr
    env_file.unlink()
    r = _run(box, "env")
    assert r.returncode == 0, r.stderr
    assert "VAPID_PRIVATE_KEY=dummy" in env_file.read_text()
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_restore_fetches_the_newest_archive_and_runs_the_cli(box):
    _run(box)
    backups = Path(box["BACKUP_ROOT"]) / "data" / "backups"
    shutil.rmtree(backups)  # a new machine: the archive is only on the remote
    r = _run(box, "restore")
    assert r.returncode == 0, r.stderr
    assert (backups / ARCHIVE).exists()
    run = [line for line in _log(box).splitlines() if line.startswith("docker run")][-1]
    assert "--network none --user 1000:1000 --read-only" in run
    assert run.endswith(
        "ghcr.io/iulian-redinciuc/parking-api:v0.2.0 /app/backend/.venv/bin/parking restore "
        f"/app/data/backups/{ARCHIVE}"
    )
    # no API container on this machine: the image is the release pinned in the restored .env
    r = _run(box | {"FAKE_IMAGE": ""}, "restore", ARCHIVE)
    assert r.returncode == 0 and "with ghcr.io/iulian-redinciuc/parking-api:v0.2.0" in r.stdout


def test_restore_refuses_while_the_api_runs_or_for_a_bad_name(box):
    _run(box)
    r = _run(box | {"FAKE_API_RUNNING": "1"}, "restore")
    assert r.returncode == 1 and "parking-api is running: stop it first" in r.stderr
    r = _run(box, "restore", "../../etc/passwd")
    assert r.returncode == 1 and "not an archive name" in r.stderr
    r = _run(box, "restore", "parking-20200101-0000.tar.gz")
    assert r.returncode == 1 and "is not on the remote" in r.stderr
    assert "docker run" not in _log(box)
    assert _run(box, "bogus").returncode == 1


REAL_RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone")


@pytest.mark.skipif(not REAL_RCLONE, reason="needs rclone (PATH or RCLONE_BIN)")
def test_with_a_real_crypt_remote(box, tmp_path):
    conf, raw = box["BACKUP_RCLONE_CONFIG"], tmp_path / "raw"
    Path(conf).unlink()
    for name, kind, extra in (
        ("plain", "alias", [f"remote={raw}"]),
        ("parking-backup", "crypt", [f"remote={raw}", "password=secret-one", "--obscure"]),
    ):
        cmd = [REAL_RCLONE, "--config", conf, "config", "create", name, kind, *extra]
        subprocess.run(cmd, check=True, capture_output=True)
    env = box | {"RCLONE": REAL_RCLONE}

    r = _run(env | {"BACKUP_REMOTE": "plain"})
    assert r.returncode == 3 and "not crypt" in r.stderr and not raw.exists()

    r = _run(env)
    assert r.returncode == 0, r.stderr
    stored = [p for p in raw.rglob("*") if p.is_file()]
    assert len(stored) == 2
    for path in stored:  # names and contents are encrypted
        assert "parking" not in path.name and "env" not in path.name
        assert b"VAPID" not in path.read_bytes() and b"archive" not in path.read_bytes()

    Path(env["BACKUP_ENV_FILE"]).write_text("PARKING_VERSION=v0.3.0\n")
    assert _run(env).returncode == 0  # the replaced .env moves to env-old/
    shutil.rmtree(Path(box["BACKUP_ROOT"]) / "data")
    Path(env["BACKUP_ENV_FILE"]).unlink()
    assert _run(env, "env").returncode == 0
    assert Path(env["BACKUP_ENV_FILE"]).read_text() == "PARKING_VERSION=v0.3.0\n"
    old = subprocess.run(
        [REAL_RCLONE, "--config", conf, "cat", "--include", "server.env", "parking-backup:env-old"],
        capture_output=True,
        text=True,
    )
    assert "VAPID_PRIVATE_KEY=dummy" in old.stdout
    r = _run(env, "restore")
    assert r.returncode == 0, r.stderr
    assert (Path(box["BACKUP_ROOT"]) / "data" / "backups" / ARCHIVE).read_text() == "archive\n"


def test_provision_installs_the_cron_entry_on_the_server_only():
    def dry(role: str) -> str:
        script = DEPLOY / "scripts" / "provision.sh"
        return subprocess.run(
            [str(script), role], env={**os.environ, "DRY_RUN": "1"}, capture_output=True, text=True
        ).stdout

    server, site = dry("server"), dry("site")
    assert "+ apt-get install -y cron rclone" in server
    assert "+ write /etc/cron.d/parking-backup (mode 644)" in server
    assert "30 3 * * * " in server
    assert "/opt/parking/deploy/backup.sh 2>&1 | logger -t parking-backup" in server
    assert "parking-backup" not in site and "rclone" not in site


def test_rclone_conf_is_git_ignored():
    ignore = (DEPLOY.parent / ".gitignore").read_text().splitlines()
    assert "deploy/rclone.conf" in ignore and "data/*" in ignore
