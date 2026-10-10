"""P7.5: rollup and retention jobs on a temp DB (data-model.md §4): `zone_minute` /
`zone_hour` fill up from `zone_state`, `--backfill` rebuilds the same rows, and prune removes
rows past (short) retention periods but keeps the newest state per zone and slot."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from typer.testing import CliRunner

from parking.api.app import create_app
from parking.api.jobs import MaintenanceJobs, vacuum
from parking.cli import app as cli
from parking.config import Settings, load_config
from parking.core.clock import FakeClock
from parking.db import rollups
from parking.db.engine import make_engine, session_scope, upgrade
from parking.db.models import (
    AdminSession,
    Correction,
    FlowEvent,
    NotificationLog,
    SlotState,
    ZoneHour,
    ZoneMinute,
    ZoneState,
)

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
WORKER = "worker-token"
LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 1, lon: 2}, timezone: Europe/Bucharest}
zones:
  - {id: ground, name: {en: Ground}, method: count, capacity: 40}
  - {id: underground, name: {en: Underground}, method: flow, capacity: 60}
cameras:
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "file:y.jpg"
    lines_file: config/lines/cam-ramp.json
    detector: {model: models/x}
"""


@pytest.fixture
def lot(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    return tmp_path


@pytest.fixture
def url(lot):
    url = f"sqlite:///{lot}/parking.sqlite"
    upgrade(url)
    return url


@pytest.fixture
def engine(url):
    engine = make_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def config(lot):
    return load_config(lot / "config" / "lot.yaml", {})


def state(ts, zone_id, free, capacity=40):
    return ZoneState(
        ts=ts, zone_id=zone_id, occupied=capacity - free, free=free, confidence=1, source="x"
    )


def all_rows(engine, model, order=None):
    with session_scope(engine) as session:
        query = select(model)
        if order is not None:
            query = query.order_by(*order)
        return [r.model_dump() for r in session.exec(query)]


def minutes(engine, zone_id=None):
    rows = all_rows(engine, ZoneMinute, (ZoneMinute.bucket_ts, ZoneMinute.zone_id))
    return [r for r in rows if zone_id is None or r["zone_id"] == zone_id]


def seed_timeline(engine):
    """ground: 10 free from 11:59:30, 12 free from 12:00:45; underground: 50 free from 12:01:30."""
    with session_scope(engine) as session:
        session.add(state(T0 - timedelta(seconds=30), "ground", 10))
        session.add(state(T0 + timedelta(seconds=45), "ground", 12))
        session.add(state(T0 + timedelta(seconds=90), "underground", 50, capacity=60))


def test_minute_job_fills_zone_minute_time_weighted_with_total(engine, config):
    seed_timeline(engine)
    jobs = MaintenanceJobs(engine, config, FakeClock(T0))
    # 11:59 (ground known from :30) .. 12:02; underground from 12:01
    assert jobs.run_minutes(T0 + timedelta(minutes=3, seconds=5)) == 4 + 2 + 4

    ground = minutes(engine, "ground")
    assert [(m["bucket_ts"], m["free_avg"], m["samples"]) for m in ground] == [
        (T0 - timedelta(minutes=1), 10, 1),
        (T0, 10.5, 2),  # the guide's example: 10 for 45 s, 12 for 15 s
        (T0 + timedelta(minutes=1), 12, 1),
        (T0 + timedelta(minutes=2), 12, 1),
    ]
    assert [m["free_avg"] for m in minutes(engine, "underground")] == [50, 50]
    total = minutes(engine, "total")
    assert [m["free_avg"] for m in total] == [10, 10.5, 37, 62]  # 12 + 50 from 12:01:30 on
    assert [m["free_max"] for m in total] == [10, 12, 62, 62]
    assert [m["occupied_avg"] for m in total] == [30, 29.5, 33, 38]


def test_minute_job_continues_from_the_last_bucket_and_redoes_the_last_minute(engine, config):
    seed_timeline(engine)
    jobs = MaintenanceJobs(engine, config, FakeClock(T0))
    jobs.run_minutes(T0 + timedelta(minutes=3))
    before = minutes(engine)
    # a late row in the last closed minute: the next run picks it up
    with session_scope(engine) as session:
        session.add(state(T0 + timedelta(minutes=2, seconds=30), "ground", 0))
    assert jobs.run_minutes(T0 + timedelta(minutes=3, seconds=30)) == 3
    after = minutes(engine)
    assert len(after) == len(before)
    last_ground = next(
        m for m in after if m["zone_id"] == "ground" and m["bucket_ts"] == T0 + timedelta(minutes=2)
    )
    assert last_ground["free_avg"] == 6


def test_minute_job_catches_up_at_most_ten_minutes(engine, config):
    seed_timeline(engine)
    jobs = MaintenanceJobs(engine, config, FakeClock(T0))
    jobs.run_minutes(T0 + timedelta(minutes=1))
    jobs.run_minutes(T0 + timedelta(hours=2))  # the API was down for two hours
    stamps = sorted({m["bucket_ts"] for m in minutes(engine, "ground")})
    assert stamps[:2] == [T0 - timedelta(minutes=1), T0]
    assert stamps[2] == T0 + timedelta(hours=2) - timedelta(minutes=10)
    assert stamps[-1] == T0 + timedelta(hours=2) - timedelta(minutes=1)


def test_hour_job_rolls_minutes_into_zone_hour(engine, config):
    seed_timeline(engine)
    jobs = MaintenanceJobs(engine, config, FakeClock(T0))
    clock = T0
    while clock < T0 + timedelta(hours=1, minutes=1):
        clock += timedelta(minutes=1)
        jobs.run_minutes(clock)
    # 11:00 (ground + total, from the 11:59 minute) and 12:00 (all three)
    assert jobs.run_hours(T0 + timedelta(hours=1, minutes=2)) == 5
    hours = {r["zone_id"]: r for r in all_rows(engine, ZoneHour) if r["bucket_ts"] == T0}
    assert set(hours) == {"ground", "underground", "total"}
    ground = hours["ground"]
    assert ground["bucket_ts"] == T0
    assert ground["samples"] == 60
    assert ground["free_avg"] == pytest.approx((10.5 + 59 * 12) / 60, abs=1e-3)
    assert (ground["free_min"], ground["free_max"]) == (10, 12)
    assert hours["underground"]["samples"] == 59  # known from 12:01:30
    # idempotent: a second run rewrites the same rows
    assert jobs.run_hours(T0 + timedelta(hours=1, minutes=3)) == 5
    assert len(all_rows(engine, ZoneHour)) == 5


def test_backfill_rebuilds_what_the_jobs_wrote_and_keeps_minutes_within_retention(engine, config):
    seed_timeline(engine)
    with session_scope(engine) as session:
        session.add(state(T0 + timedelta(hours=2, seconds=20), "ground", 3))
    jobs = MaintenanceJobs(engine, config, FakeClock(T0))
    now = T0 + timedelta(hours=3, seconds=5)
    t = T0
    while t + timedelta(minutes=1) <= now:
        t += timedelta(minutes=1)
        jobs.run_minutes(t)
    for h in range(4):
        jobs.run_hours(T0 + timedelta(hours=h, minutes=2))
    live_minutes, live_hours = minutes(engine), all_rows(engine, ZoneHour)

    zone_ids = [z.id for z in config.zones]
    with session_scope(engine) as session:
        written = rollups.backfill(session, zone_ids, now)
    assert written == (len(live_minutes), len(live_hours))
    assert minutes(engine) == live_minutes
    assert sorted(all_rows(engine, ZoneHour), key=str) == sorted(live_hours, key=str)

    # with a 1-hour minute retention only the last hour of minutes is stored; hours all are
    short = rollups.Retention(minute=timedelta(hours=1))
    with session_scope(engine) as session:
        rollups.backfill(session, zone_ids, now, short)
    assert min(m["bucket_ts"] for m in minutes(engine)) >= now - timedelta(hours=1)
    assert len(all_rows(engine, ZoneHour)) == len(live_hours)


def test_prune_with_a_short_retention(engine, config):
    now = T0 + timedelta(days=10)
    old, new = T0, now - timedelta(hours=1)
    with session_scope(engine) as session:
        for ts, free in ((old, 1), (old + timedelta(minutes=1), 2), (new, 3)):
            session.add(state(ts, "ground", free))
        session.add(state(old, "underground", 5, capacity=60))  # newest of its zone: kept
        session.add(SlotState(ts=old, camera_id="cam", slot_id="A", taken=True))
        session.add(
            SlotState(ts=old + timedelta(seconds=1), camera_id="cam", slot_id="A", taken=False)
        )
        session.add(SlotState(ts=old, camera_id="cam", slot_id="B", taken=True))
        for i, ts in enumerate((old, new)):
            session.add(
                FlowEvent(
                    event_id=f"e{i}",
                    ts=ts,
                    camera_id="cam-ramp",
                    zone_id="underground",
                    direction="in",
                    track_id=i,
                    confidence=0.9,
                    applied=True,
                )
            )
            session.add(
                NotificationLog(ts=ts, subscription_id="s", kind="test", payload={}, status="sent")
            )
            for model in (ZoneMinute, ZoneHour):
                session.add(
                    model(
                        zone_id="ground",
                        bucket_ts=floor(ts),
                        free_avg=1,
                        free_min=1,
                        free_max=1,
                        occupied_avg=1,
                        samples=1,
                    )
                )
        session.add(
            Correction(ts=old, zone_id="underground", old_occupied=0, new_occupied=1, actor="x")
        )
        session.add(
            AdminSession(token_hash="a", created_at=old, expires_at=old + timedelta(days=7))
        )
        session.add(
            AdminSession(token_hash="b", created_at=new, expires_at=new + timedelta(days=7))
        )

    retention = rollups.Retention(
        raw=timedelta(days=2), minute=timedelta(days=1), log=timedelta(days=3)
    )
    deleted = MaintenanceJobs(engine, config, FakeClock(now), retention).run_prune(now)
    assert deleted == {
        "zone_state": 2,
        "slot_state": 1,
        "flow_event": 1,
        "zone_minute": 1,
        "notification_log": 1,
        "admin_session": 1,
    }
    assert [(r["zone_id"], r["free"]) for r in all_rows(engine, ZoneState)] == [
        ("ground", 3),
        ("underground", 5),
    ]
    assert {(r["slot_id"], r["taken"]) for r in all_rows(engine, SlotState)} == {
        ("A", False),
        ("B", True),
    }
    assert [r["event_id"] for r in all_rows(engine, FlowEvent)] == ["e1"]
    assert len(all_rows(engine, ZoneHour)) == 2  # hours and corrections are kept
    assert len(all_rows(engine, Correction)) == 1
    assert [r["token_hash"] for r in all_rows(engine, AdminSession)] == ["b"]
    vacuum(engine)  # runs outside a transaction
    assert MaintenanceJobs(engine, config, FakeClock(now), retention).run_prune(
        now
    ) == dict.fromkeys(deleted, 0)


def floor(ts):
    return rollups.floor_minute(ts)


def test_default_retention_matches_data_model():
    r = rollups.Retention()
    assert (r.raw, r.minute, r.log) == (timedelta(days=90), timedelta(days=30), timedelta(days=30))


def test_cli_aggregate_backfill_and_prune(lot, url, engine):
    now = datetime.now(UTC)
    with session_scope(engine) as session:
        session.add(state(now - timedelta(days=40), "ground", 1))
        session.add(state(now - timedelta(days=35), "ground", 2))
        session.add(state(now - timedelta(hours=3), "ground", 4))
    config = str(lot / "config" / "lot.yaml")
    runner = CliRunner()
    result = runner.invoke(cli, ["db", "aggregate", "--backfill", "--config", config, "--url", url])
    assert result.exit_code == 0, result.output
    assert "zone_minute: " in result.output
    stored = minutes(engine, "ground")
    assert len(stored) >= 30 * 1440 - 1  # only the last 30 days of minutes
    assert len(all_rows(engine, ZoneHour)) >= 2 * 40 * 24 - 2  # ground + total, 40 days

    result = runner.invoke(
        cli,
        [
            "db",
            "prune",
            "--raw-days",
            "1",
            "--minute-days",
            "0.1",
            "--vacuum",
            "--config",
            config,
            "--url",
            url,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "zone_state: 2 row(s) deleted" in result.output
    assert min(m["bucket_ts"] for m in minutes(engine)) >= now - timedelta(days=0.1, minutes=1)

    result = runner.invoke(cli, ["db", "aggregate", "--config", config, "--url", url])
    assert result.exit_code == 0, result.output


def test_retention_report_counts_only_rows_the_prune_missed(engine):
    """P8.9: the report is the proof that the retention jobs delete (security-privacy.md §4.1)."""
    now = T0 + timedelta(days=200)
    old = now - timedelta(days=120)
    with session_scope(engine) as session:
        # kept on purpose although old: the newest row of its zone / slot
        session.add(state(old, "underground", 5, capacity=60))
        session.add(SlotState(ts=old, camera_id="cam", slot_id="B", taken=True))
        # inside the periods, or past them by less than the daily job's grace
        session.add(state(now - timedelta(days=90, hours=20), "ground", 1))
        session.add(state(now - timedelta(days=1), "ground", 2))
        session.add(
            NotificationLog(
                ts=now - timedelta(days=29),
                subscription_id="s",
                kind="test",
                payload={},
                status="sent",
            )
        )
        session.add(
            AdminSession(token_hash="a", created_at=now, expires_at=now - timedelta(hours=2))
        )
        session.add(
            Correction(ts=old, zone_id="underground", old_occupied=0, new_occupied=1, actor="x")
        )
    with session_scope(engine) as session:
        report = {r.table: r for r in rollups.retention_report(session, now)}
    assert [r.overdue for r in report.values()] == [0] * 9
    assert (report["zone_state"].rows, report["zone_state"].oldest) == (3, old)
    assert report["zone_state"].keep == "90 days" and report["zone_minute"].keep == "30 days"
    assert report["admin_session"].keep == "until it expires"
    assert (report["correction"].rows, report["correction"].overdue) == (1, 0)
    assert report["flow_event"].oldest is None and report["push_subscription"].rows == 0

    # the same rows a prune would have deleted long ago: every one is reported
    with session_scope(engine) as session:
        # (the newest row of a zone / slot is the one with the highest id)
        session.add(state(old - timedelta(days=1), "ground", 3))
        session.add(state(now - timedelta(hours=1), "ground", 4))
        session.add(
            SlotState(ts=old - timedelta(days=1), camera_id="cam", slot_id="B", taken=False)
        )
        session.add(
            SlotState(ts=now - timedelta(hours=1), camera_id="cam", slot_id="B", taken=True)
        )
        session.add(
            FlowEvent(
                event_id="e",
                ts=old,
                camera_id="cam-ramp",
                zone_id="underground",
                direction="in",
                track_id=1,
                confidence=0.9,
                applied=True,
            )
        )
        session.add(
            ZoneMinute(
                zone_id="ground",
                bucket_ts=now - timedelta(days=40),
                free_avg=1,
                free_min=1,
                free_max=1,
                occupied_avg=1,
                samples=1,
            )
        )
        session.add(
            NotificationLog(ts=old, subscription_id="s", kind="test", payload={}, status="sent")
        )
        session.add(
            AdminSession(token_hash="b", created_at=old, expires_at=now - timedelta(days=3))
        )
    with session_scope(engine) as session:
        report = {r.table: r.overdue for r in rollups.retention_report(session, now)}
        assert report == {
            "zone_state": 1,
            "slot_state": 2,  # slot B's first row is no longer its newest
            "flow_event": 1,
            "zone_minute": 1,
            "notification_log": 1,
            "admin_session": 1,
            "zone_hour": 0,
            "correction": 0,
            "push_subscription": 0,
        }
        rollups.prune(session, now)
    with session_scope(engine) as session:
        assert sum(r.overdue for r in rollups.retention_report(session, now)) == 0


def test_cli_retention_fails_until_pruned(lot, url, engine):
    now = datetime.now(UTC)
    with session_scope(engine) as session:
        session.add(state(now - timedelta(days=100), "ground", 1))
        session.add(state(now - timedelta(hours=3), "ground", 4))
    args = ["--config", str(lot / "config" / "lot.yaml"), "--url", url]
    runner = CliRunner()
    result = runner.invoke(cli, ["db", "retention", *args])
    assert result.exit_code == 1, result.output
    assert "OVERDUE 1 row(s)" in result.output and "1 row(s) past their period" in result.output
    assert runner.invoke(cli, ["db", "prune", *args]).exit_code == 0
    result = runner.invoke(cli, ["db", "retention", *args])
    assert result.exit_code == 0, result.output
    assert "retention: ok" in result.output and "OVERDUE" not in result.output
    assert "zone_hour" in result.output and "kept: hourly averages" in result.output


def test_api_runs_the_jobs_on_ingested_changes(lot):
    clock = FakeClock(T0)
    settings = Settings(_env_file=None, worker_token=WORKER)
    url = f"sqlite:///{lot}/api.sqlite"
    app = create_app(lot / "config" / "lot.yaml", settings, clock=clock, db_url=url, tick=False)
    with TestClient(app) as client:
        rt = client.app.state.runtime
        assert rt.jobs is not None
        assert str(rt.jobs.tz) == "Europe/Bucharest"
        for i, second in enumerate((10, 40)):
            clock.set(T0 + timedelta(seconds=second))
            event = {
                "v": 1,
                "event_id": f"e{i}",
                "camera_id": "cam-ramp",
                "ts": clock.now().isoformat(),
                "direction": "in",
                "track_id": i,
                "cls": "car",
                "confidence": 0.9,
            }
            r = client.post(
                "/internal/flow-events",
                json={"events": [event]},
                headers={"Authorization": f"Bearer {WORKER}"},
            )
            assert r.status_code == 200, r.text
        clock.set(T0 + timedelta(minutes=1, seconds=5))
        assert rt.jobs.run_minutes(clock.now()) == 2  # underground + total
    engine = make_engine(url)
    rows = minutes(engine, "underground")
    engine.dispose()
    # 59 free from :10, 58 free from :40 (known for 50 s)
    assert rows[0]["free_avg"] == pytest.approx((30 * 59 + 20 * 58) / 50, abs=1e-3)
