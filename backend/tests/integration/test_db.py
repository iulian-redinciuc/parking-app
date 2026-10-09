"""P2.6: temp DB, upgrade, write, read back (data-model.md)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlmodel import SQLModel, select
from typer.testing import CliRunner

from parking.cli import app
from parking.core.fusion import SlotChange, ZoneChange
from parking.db import repo
from parking.db.engine import make_engine, session_scope, upgrade
from parking.db.models import FlowEvent, ZoneState
from parking.messages import CameraHealthMsg

T0 = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
TABLES = {
    "zone_state",
    "slot_state",
    "camera_health",
    "flow_event",
    "correction",
    "push_subscription",
    "notification_log",
}


@pytest.fixture
def engine(tmp_path):
    url = f"sqlite:///{tmp_path}/db/parking.sqlite"
    upgrade(url)
    engine = make_engine(url)
    yield engine
    engine.dispose()


def zone(ts, zone_id, occupied, source="observation", count_changed=True):
    return ZoneChange(
        ts=ts,
        zone_id=zone_id,
        occupied=occupied,
        free=34 - occupied,
        level="plenty",
        confidence=1.0,
        stale=False,
        trend="steady",
        count_changed=count_changed,
        source=source,
    )


def test_upgrade_creates_tables_in_wal_mode(engine, tmp_path):
    assert (tmp_path / "db" / "parking.sqlite").is_file()
    with engine.connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        assert TABLES | {"alembic_version"} == tables
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL


def test_migration_matches_models(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == []


def test_upgrade_twice_is_a_no_op(engine, tmp_path):
    upgrade(f"sqlite:///{tmp_path}/db/parking.sqlite")


def test_record_changes_and_read_back_latest(engine):
    changes = [
        SlotChange(T0, "cam-ground", "g-01", True),
        SlotChange(T0, "cam-ground", "g-02", False),
        zone(T0, "ground", 1),
        zone(T0, "ground", 1, source="tick", count_changed=False),  # stale/level only
    ]
    later = T0 + timedelta(seconds=30)
    with session_scope(engine) as s:
        assert repo.record_changes(s, changes) == 3
    with session_scope(engine) as s:
        assert repo.record_changes(s, [SlotChange(later, "cam-ground", "g-01", False)]) == 1
        assert repo.record_changes(s, [zone(later, "ground", 0)]) == 1
        assert repo.record_changes(s, [zone(later, "ground", 7, source="startup")]) == 0

    with session_scope(engine) as s:
        assert repo.latest_slot_states(s) == {"cam-ground": {"g-01": False, "g-02": False}}
        latest = repo.latest_zone_states(s)
        assert list(latest) == ["ground"]
        assert (latest["ground"].occupied, latest["ground"].free) == (0, 34)
        assert latest["ground"].ts == later
        assert latest["ground"].ts.tzinfo is not None
        assert len(s.exec(select(ZoneState)).all()) == 2


def test_empty_db_reads_back_empty(engine):
    with session_scope(engine) as s:
        assert repo.latest_slot_states(s) == {}
        assert repo.latest_zone_states(s) == {}
        assert repo.camera_health(s) == {}


def test_upsert_camera_health_keeps_one_row_per_camera(engine):
    first = CameraHealthMsg(camera_id="cam-ground", ts=T0, state="ok", fps=0.2)
    second = CameraHealthMsg(
        camera_id="cam-ground", ts=T0 + timedelta(seconds=10), state="degraded", issue="blurry"
    )
    with session_scope(engine) as s:
        repo.upsert_camera_health(s, first)
    with session_scope(engine) as s:
        repo.upsert_camera_health(s, second)
    with session_scope(engine) as s:
        rows = repo.camera_health(s)
        assert list(rows) == ["cam-ground"]
        row = rows["cam-ground"]
        assert (row.state, row.issue, row.fps, row.ts) == ("degraded", "blurry", None, second.ts)


def test_timestamps_are_stored_as_sortable_utc_text(engine):
    local = datetime(2026, 10, 8, 21, 0, tzinfo=timezone(timedelta(hours=3)))
    with session_scope(engine) as s:
        repo.record_changes(s, [zone(local, "ground", 2), zone(datetime(2026, 10, 8), "x", 1)])
    with engine.connect() as conn:
        raw = conn.exec_driver_sql("SELECT ts FROM zone_state ORDER BY ts").scalars().all()
    assert raw == ["2026-10-08T00:00:00.000000+00:00", "2026-10-08T18:00:00.000000+00:00"]


def test_flow_event_id_is_the_primary_key(engine):
    def event():
        return FlowEvent(
            event_id="e1",
            ts=T0,
            camera_id="cam-ramp",
            zone_id="underground",
            direction="in",
            track_id=3,
            confidence=0.9,
            applied=True,
        )

    with session_scope(engine) as s:
        s.add(event())
    with pytest.raises(sa.exc.IntegrityError), session_scope(engine) as s:
        s.add(event())


def test_cli_db_upgrade(tmp_path):
    url = f"sqlite:///{tmp_path}/cli.sqlite"
    result = CliRunner().invoke(app, ["db", "upgrade", "--url", url])
    assert result.exit_code == 0, result.output
    engine = make_engine(url)
    with engine.connect() as conn:
        assert set(sa.inspect(conn).get_table_names()) >= TABLES
    engine.dispose()


def test_cli_db_upgrade_reports_errors(tmp_path):
    url = f"sqlite:///{tmp_path}/cli.sqlite"
    result = CliRunner().invoke(app, ["db", "upgrade", "--url", url, "--revision", "nope"])
    assert result.exit_code == 1
    assert "upgrade failed" in result.output
