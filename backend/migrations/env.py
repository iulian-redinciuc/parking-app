"""Alembic environment: the URL comes from `parking.db.engine` and the tables from
`parking.db.models`. SQLite needs batch mode for ALTERs, so it's always on."""

from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlmodel import SQLModel

import parking.db.models  # noqa: F401  (registers the tables)
from parking.db.engine import default_url, make_engine
from parking.db.models import UtcDateTime

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def render_item(type_, obj, autogen_context):
    """Migrations don't import app types: UtcDateTime is plain text in the database."""
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.String(length=32)"
    if type_ == "type" and obj.__class__.__module__.startswith("sqlmodel"):
        return f"sa.{sa.String.__name__}()"
    return False


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or default_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
