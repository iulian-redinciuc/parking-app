"""Backups: one `parking-YYYYMMDD-HHMM.tar.gz` with the database, `config/` and the reference
images, rotation, and the restore (deployment.md §7).

Archive layout:
    manifest.json                 format, created, app_version, db_revision, files (size, sha256)
    db/parking.sqlite             copied with SQLite's online backup API (safe while the API runs)
    config/...                    lot.yaml, slot and line files (not the editor's *.bak)
    reference/...                 data/reference/ (the slot reference images), when present
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from parking import __version__

FORMAT = 1
DB_MEMBER = "db/parking.sqlite"
NAME_RE = re.compile(r"^parking-(\d{8})-(\d{4})\.tar\.gz$")
KEEP_DAILY = 14
KEEP_WEEKLY = 8


class BackupError(Exception):
    pass


def sqlite_path(url: str) -> Path:
    """The file behind a SQLite URL (the only database the backup knows)."""
    parsed = make_url(url)
    if (
        parsed.get_backend_name() != "sqlite"
        or not parsed.database
        or parsed.database == ":memory:"
    ):
        raise BackupError(f"not a SQLite file database: {parsed.render_as_string()}")
    return Path(parsed.database)


def archive_name(now: datetime) -> str:
    """`parking-YYYYMMDD-HHMM.tar.gz`, in UTC."""
    return f"parking-{now.astimezone(UTC):%Y%m%d-%H%M}.tar.gz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_db(path: Path) -> str | None:
    """Run `PRAGMA integrity_check`; returns the Alembic revision (None before any migration)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"database integrity check failed: {result}")
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None
    except sqlite3.DatabaseError as e:
        raise BackupError(f"not a readable SQLite database: {e}") from e
    finally:
        conn.close()


def _files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix != ".bak")


def create_backup(
    db: Path, config_dir: Path, reference_dir: Path, out: Path, now: datetime
) -> Path:
    """Write the archive for `now` into `out` and return its path."""
    if not db.is_file():
        raise BackupError(f"no database at {db}")
    if not (config_dir / "lot.yaml").is_file():
        raise BackupError(f"no lot.yaml in {config_dir}")
    out.mkdir(parents=True, exist_ok=True)
    target = out / archive_name(now)
    with tempfile.TemporaryDirectory(dir=out, prefix=".backup-") as tmp:
        copy = Path(tmp) / "parking.sqlite"
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        revision = _check_db(copy)

        members = [(DB_MEMBER, copy)]
        members += [
            (f"config/{p.relative_to(config_dir).as_posix()}", p) for p in _files(config_dir)
        ]
        members += [
            (f"reference/{p.relative_to(reference_dir).as_posix()}", p)
            for p in _files(reference_dir)
        ]
        manifest = {
            "format": FORMAT,
            "created": now.astimezone(UTC).isoformat(timespec="seconds"),
            "app_version": __version__,
            "db_revision": revision,
            "files": {
                name: {"size": path.stat().st_size, "sha256": _sha256(path)}
                for name, path in members
            },
        }
        data = json.dumps(manifest, indent=2).encode()
        partial = Path(tmp) / target.name
        with tarfile.open(partial, "w:gz") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            info.mtime = int(now.timestamp())
            tar.addfile(info, io.BytesIO(data))
            for name, path in members:
                tar.add(path, arcname=name, recursive=False)
        partial.replace(target)
    return target


def rotate(out: Path, keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY) -> list[Path]:
    """Delete old archives in `out`: the newest of each of the last `keep_daily` days that have
    one stays, and the newest of each of the last `keep_weekly` ISO weeks. Returns what it deleted;
    other files are never touched."""
    stamped = []
    for path in out.iterdir():
        m = NAME_RE.match(path.name)
        if m and path.is_file():
            try:
                stamped.append((datetime.strptime(m[1] + m[2], "%Y%m%d%H%M"), path))
            except ValueError:
                continue
    stamped.sort(reverse=True)
    keep: set[Path] = set()
    days: set = set()
    weeks: set = set()
    for ts, path in stamped:
        day, week = ts.date(), tuple(ts.isocalendar()[:2])
        if day not in days and len(days) < keep_daily:
            days.add(day)
            keep.add(path)
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.add(week)
            keep.add(path)
    deleted = [path for _, path in stamped if path not in keep]
    for path in deleted:
        path.unlink()
    return sorted(deleted)


@dataclass
class Restored:
    manifest: dict
    db: Path
    previous_db: Path | None = None
    files: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)  # replaced config files, as <file>.bak


def _safe_member(name: str) -> bool:
    parts = Path(name).parts
    return (
        name == DB_MEMBER
        or (len(parts) > 1 and parts[0] in ("config", "reference") and ".." not in parts)
    ) and not Path(name).is_absolute()


def restore_backup(
    archive: Path, db: Path, config_dir: Path, reference_dir: Path, now: datetime
) -> Restored:
    """Check `archive` (manifest, checksums, database integrity) and put its content in place.
    Nothing is lost: an existing database is renamed to `<name>.before-restore-<time>` and a config
    file that differs is kept as `<file>.bak`. The API must be stopped."""
    if not archive.is_file():
        raise BackupError(f"no such archive: {archive}")
    db.parent.mkdir(parents=True, exist_ok=True)
    # next to the database, not in /tmp (64 MB of memory in the containers)
    with tempfile.TemporaryDirectory(dir=db.parent, prefix=".restore-") as tmp_name:
        tmp = Path(tmp_name)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                names = [m.name for m in tar.getmembers() if m.isfile()]
                if "manifest.json" not in names:
                    raise BackupError("not a parking backup: no manifest.json")
                bad = [n for n in names if n != "manifest.json" and not _safe_member(n)]
                if bad or any(not (m.isfile() or m.isdir()) for m in tar.getmembers()):
                    raise BackupError(
                        f"unexpected entries in the archive: {bad or 'links/devices'}"
                    )
                tar.extractall(tmp, filter="data")
        except (tarfile.TarError, OSError, EOFError) as e:
            raise BackupError(f"can't read {archive.name}: {e}") from e

        try:
            manifest = json.loads((tmp / "manifest.json").read_text())
            files: dict = manifest["files"]
            fmt = manifest["format"]
        except (ValueError, KeyError, TypeError) as e:
            raise BackupError(f"bad manifest.json: {e}") from e
        if fmt != FORMAT:
            raise BackupError(f"backup format {fmt} is not supported (this version reads {FORMAT})")
        if DB_MEMBER not in files or "config/lot.yaml" not in files:
            raise BackupError("the backup has no database or no config/lot.yaml")
        if set(files) != set(names) - {"manifest.json"}:
            raise BackupError("the archive's files don't match its manifest")
        for name, meta in files.items():
            if _sha256(tmp / name) != meta.get("sha256"):
                raise BackupError(f"checksum mismatch: {name}")
        _check_db(tmp / DB_MEMBER)

        result = Restored(manifest=manifest, db=db)
        if db.exists():
            result.previous_db = db.with_name(f"{db.name}.before-restore-{now:%Y%m%d-%H%M%S}")
            db.rename(result.previous_db)
        # a WAL left from the old database must never be replayed into the restored one
        for suffix in ("-wal", "-shm"):
            db.with_name(db.name + suffix).unlink(missing_ok=True)
        shutil.copyfile(tmp / DB_MEMBER, db)

        for name in sorted(files):
            if name == DB_MEMBER:
                continue
            top, rel = name.split("/", 1)
            dest = (config_dir if top == "config" else reference_dir) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if top == "config" and dest.is_file() and _sha256(dest) != files[name]["sha256"]:
                kept = dest.with_name(dest.name + ".bak")
                dest.replace(kept)
                result.kept.append(kept)
            shutil.copyfile(tmp / name, dest)
            result.files.append(dest)
    return result
