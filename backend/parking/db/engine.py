"""SQLModel engine in WAL mode, the default database URL and `upgrade()` (data-model.md)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, event
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
MIGRATIONS = BACKEND_DIR / "migrations"
DB_FILE = Path("data/db/parking.sqlite")


def default_url(root: Path = Path(".")) -> str:
    """`PARKING_DB_URL` (environment or `<root>/deploy/.env`), else
    `sqlite:///<root>/data/db/parking.sqlite`."""
    from parking.config import Settings

    url = Settings(_env_file=root / "deploy" / ".env").parking_db_url
    return url or f"sqlite:///{(root / DB_FILE).as_posix()}"


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def make_engine(url: str) -> Engine:
    """An engine for `url`; for a SQLite file the folder is created and WAL is switched on."""
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    if is_sqlite and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False} if is_sqlite else {})
    if is_sqlite:
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """A session that commits on success and rolls back on error."""
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def alembic_config(url: str):
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False  # keep the CLI/API logging setup
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Apply the Alembic migrations up to `revision` (run by `parking db upgrade` and at API
    start-up)."""
    from alembic import command

    if make_url(url).get_backend_name() == "sqlite":
        make_engine(url).dispose()  # creates the folder
    command.upgrade(alembic_config(url), revision)
