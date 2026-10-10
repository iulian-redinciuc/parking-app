"""`parking backup` / `parking restore` and the rotation (deployment.md §7)."""

import io
import json
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.db.backup import (
    BackupError,
    archive_name,
    create_backup,
    restore_backup,
    rotate,
    sqlite_path,
)

runner = CliRunner()
NOW = datetime(2026, 10, 10, 1, 30, tzinfo=UTC)


def _app_root(tmp_path: Path, rows: int = 3) -> Path:
    """A tiny app root: a WAL database with `rows` rows, config/ and one reference image."""
    root = tmp_path / "app"
    (root / "config" / "slots").mkdir(parents=True)
    (root / "config" / "lot.yaml").write_text("version: 1\n")
    (root / "config" / "slots" / "cam-ground.json").write_text('{"slots": []}')
    (root / "config" / "slots" / "cam-ground.json.bak").write_text("old")
    (root / "data" / "reference").mkdir(parents=True)
    (root / "data" / "reference" / "cam-ground.jpg").write_bytes(b"\xff\xd8 not a real photo")
    (root / "data" / "db").mkdir()
    conn = sqlite3.connect(root / "data" / "db" / "parking.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE zone_state (id INTEGER PRIMARY KEY, free INTEGER)")
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES ('0007')")
    conn.executemany("INSERT INTO zone_state (free) VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()
    return root


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "data" / "db" / "parking.sqlite", root / "config", root / "data" / "reference"


def _rows(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT count(*) FROM zone_state").fetchone()[0]
    finally:
        conn.close()


def test_archive_name_is_utc():
    local = datetime.fromisoformat("2026-10-10T02:05:00+03:00")
    assert archive_name(local) == "parking-20261009-2305.tar.gz"


def test_backup_contents_and_manifest(tmp_path):
    root = _app_root(tmp_path)
    path = create_backup(*_paths(root), tmp_path / "out", NOW)
    assert path.name == "parking-20261010-0130.tar.gz"
    with tarfile.open(path) as tar:
        names = set(tar.getnames())
        manifest = json.load(tar.extractfile("manifest.json"))
    assert names == {
        "manifest.json",
        "db/parking.sqlite",
        "config/lot.yaml",
        "config/slots/cam-ground.json",
        "reference/cam-ground.jpg",
    }
    assert manifest["format"] == 1 and manifest["db_revision"] == "0007"
    assert manifest["created"] == "2026-10-10T01:30:00+00:00"
    assert set(manifest["files"]) == names - {"manifest.json"}
    assert list((tmp_path / "out").iterdir()) == [path]  # no temp files left


def test_backup_while_the_database_is_being_written(tmp_path):
    root = _app_root(tmp_path, rows=5)
    db = _paths(root)[0]
    writer = sqlite3.connect(db)  # an open write transaction, like the running API
    writer.execute("INSERT INTO zone_state (free) VALUES (99)")
    try:
        path = create_backup(*_paths(root), tmp_path / "out", NOW)
    finally:
        writer.rollback()
        writer.close()
    restored = tmp_path / "new"
    restore_backup(path, *_paths(restored), NOW)
    assert _rows(_paths(restored)[0]) == 5  # the committed rows only


def test_backup_needs_database_and_config(tmp_path):
    root = _app_root(tmp_path)
    db, config, reference = _paths(root)
    with pytest.raises(BackupError, match="no database"):
        create_backup(tmp_path / "missing.sqlite", config, reference, tmp_path / "out", NOW)
    with pytest.raises(BackupError, match="no lot.yaml"):
        create_backup(db, tmp_path / "nowhere", reference, tmp_path / "out", NOW)
    with pytest.raises(BackupError, match="not a SQLite"):
        sqlite_path("postgresql://u@h/db")


def test_restore_into_an_empty_root(tmp_path):
    root = _app_root(tmp_path)
    path = create_backup(*_paths(root), tmp_path / "out", NOW)
    new = tmp_path / "new"
    result = restore_backup(path, *_paths(new), NOW)
    assert result.previous_db is None and result.kept == []
    assert _rows(new / "data" / "db" / "parking.sqlite") == 3
    assert (new / "config" / "lot.yaml").read_text() == "version: 1\n"
    assert (new / "config" / "slots" / "cam-ground.json").exists()
    assert not (new / "config" / "slots" / "cam-ground.json.bak").exists()
    assert (new / "data" / "reference" / "cam-ground.jpg").read_bytes().startswith(b"\xff\xd8")
    assert not list((new / "data" / "db").glob(".restore-*"))


def test_restore_keeps_what_it_replaces(tmp_path):
    root = _app_root(tmp_path, rows=3)
    db, config, reference = _paths(root)
    path = create_backup(db, config, reference, tmp_path / "out", NOW)
    # afterwards the app moved on: more rows, an edited config, a WAL on disk
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO zone_state (free) VALUES (7)")
    conn.commit()
    conn.close()
    (config / "lot.yaml").write_text("version: 1\n# edited\n")
    db.with_name("parking.sqlite-wal").write_bytes(b"stale")
    later = NOW + timedelta(hours=1)

    result = restore_backup(path, db, config, reference, later)
    assert _rows(db) == 3
    assert result.previous_db == db.with_name("parking.sqlite.before-restore-20261010-023000")
    assert _rows(result.previous_db) == 4
    assert not db.with_name("parking.sqlite-wal").exists()
    assert (config / "lot.yaml").read_text() == "version: 1\n"
    assert result.kept == [config / "lot.yaml.bak"]
    assert (config / "lot.yaml.bak").read_text().endswith("# edited\n")


def _repack(src: Path, dst: Path, change) -> None:
    """Copy an archive, letting `change(name, data)` return new bytes (None drops the entry)."""
    with tarfile.open(src) as old, tarfile.open(dst, "w:gz") as new:
        for member in old.getmembers():
            data = change(member.name, old.extractfile(member).read())
            if data is None:
                continue
            member.size = len(data)
            new.addfile(member, io.BytesIO(data))


def test_restore_rejects_damaged_archives(tmp_path):
    root = _app_root(tmp_path)
    good = create_backup(*_paths(root), tmp_path / "out", NOW)
    new = tmp_path / "new"

    tampered = tmp_path / "tampered.tar.gz"
    _repack(good, tampered, lambda n, d: b"version: 2\n" if n == "config/lot.yaml" else d)
    with pytest.raises(BackupError, match="checksum mismatch: config/lot.yaml"):
        restore_backup(tampered, *_paths(new), NOW)

    no_manifest = tmp_path / "no-manifest.tar.gz"
    _repack(good, no_manifest, lambda n, d: None if n == "manifest.json" else d)
    with pytest.raises(BackupError, match="no manifest"):
        restore_backup(no_manifest, *_paths(new), NOW)

    truncated = tmp_path / "truncated.tar.gz"
    truncated.write_bytes(good.read_bytes()[:200])
    with pytest.raises(BackupError, match="can't read"):
        restore_backup(truncated, *_paths(new), NOW)

    assert not (new / "data" / "db" / "parking.sqlite").exists()
    assert not (new / "config").exists()


def test_restore_rejects_paths_outside_the_app(tmp_path):
    root = _app_root(tmp_path)
    good = create_backup(*_paths(root), tmp_path / "out", NOW)
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(good) as old, tarfile.open(evil, "w:gz") as new:
        for member in old.getmembers():
            new.addfile(member, old.extractfile(member))
        info = tarfile.TarInfo("config/../../escaped.txt")
        info.size = 2
        new.addfile(info, io.BytesIO(b"hi"))
    with pytest.raises(BackupError, match="unexpected entries"):
        restore_backup(evil, *_paths(tmp_path / "new"), NOW)
    assert not (tmp_path / "escaped.txt").exists()


def _touch(out: Path, when: datetime) -> Path:
    path = out / archive_name(when)
    path.write_bytes(b"x")
    return path


def test_rotate_keeps_14_daily_and_8_weekly(tmp_path):
    start = datetime(2026, 10, 10, 1, 30, tzinfo=UTC)  # a Saturday
    paths = {d: _touch(tmp_path, start - timedelta(days=d)) for d in range(100)}
    other = tmp_path / "notes.txt"
    other.write_text("keep me")

    deleted = rotate(tmp_path)
    kept = sorted(d for d, p in paths.items() if p.exists())
    # the last 14 days, then the newest (Sunday) archive of each older week up to 8 weeks
    assert kept == [*range(14), 20, 27, 34, 41, 48]
    assert len(deleted) == 100 - len(kept) and other.exists()
    assert rotate(tmp_path) == []  # stable


def test_rotate_keeps_the_newest_of_a_day(tmp_path):
    day = datetime(2026, 10, 10, tzinfo=UTC)
    early, late = _touch(tmp_path, day.replace(hour=1)), _touch(tmp_path, day.replace(hour=14))
    assert rotate(tmp_path) == [early] and late.exists()
    assert rotate(tmp_path, keep_daily=0, keep_weekly=0) == [late]


def test_cli_backup_and_restore(tmp_path, monkeypatch):
    root = _app_root(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.delenv("PARKING_DB_URL", raising=False)
    (root / "data" / "backups").mkdir()
    old = _touch(root / "data" / "backups", NOW - timedelta(days=400))

    result = runner.invoke(
        app, ["backup", "--out", "data/backups", "--keep-daily", "1", "--keep-weekly", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "backup written: data/backups/parking-" in result.output
    assert f"rotated out: {old.name}" in result.output
    (archive,) = (root / "data" / "backups").glob("parking-*.tar.gz")

    new = tmp_path / "new"
    new.mkdir()
    monkeypatch.chdir(new)
    result = runner.invoke(app, ["restore", str(archive)])
    assert result.exit_code == 0, result.output
    assert "database revision 0007" in result.output
    assert _rows(new / "data" / "db" / "parking.sqlite") == 3
    assert (new / "config" / "lot.yaml").exists()

    result = runner.invoke(app, ["restore", str(tmp_path / "nope.tar.gz")])
    assert result.exit_code == 1 and "restore failed: no such archive" in result.output


def test_cli_backup_fails_without_a_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PARKING_DB_URL", raising=False)
    result = runner.invoke(app, ["backup", "--out", "out"])
    assert result.exit_code == 1 and "backup failed: no database" in result.output
