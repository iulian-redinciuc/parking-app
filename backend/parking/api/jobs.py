"""Rollup and retention jobs (data-model.md §4): an APScheduler `AsyncIOScheduler` started in
the API lifespan, next to the push reminders.

| Job | When | Does |
|-----|------|------|
| `aggregate-minutes` | every minute at :05 s | `zone_minute` since the last bucket (≤ 10 min) |
| `aggregate-hours` | hourly at :02 | `zone_hour` for the last 3 complete hours |
| `prune` | daily 04:00 lot time | `rollups.prune` with the default retention |
| `vacuum` | Sunday 04:30 lot time | `PRAGMA optimize; VACUUM` |
| `reset-<zone>` | the zone's `reset.cron`, lot time | the flow count set to `reset.value` (P5.7) |
| `forecast` | daily 03:30 lot time | weather refreshed, forecast models retrained (forecast.md) |

Each job's synchronous core (`run_minutes(now)`, ...) runs in a thread; tests call them with
a fake clock. The minute job starts at second 5 so the ingest writes of the closed minute are
in. Missing minutes further back than the catch-up stay empty (the API was down: nothing is
known) until `parking db aggregate --backfill`. The scheduled flow-zone reset goes through
`Ingestor.correct` (actor `scheduled-reset`, `zone_state` source `reset`), so it is logged in
`correction` and published like an admin's correction; it needs an `ingestor`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Engine, text

from parking.api.ingest import Ingestor
from parking.config import LotConfig, crontab_trigger, total_key
from parking.core.clock import Clock
from parking.db import rollups
from parking.db.engine import session_scope
from parking.db.models import ZoneMinute
from parking.forecast import model as forecast_model
from parking.forecast import weather
from parking.forecast.days import calendar_of

log = logging.getLogger(__name__)

MINUTE_CATCH_UP = timedelta(minutes=10)
HOUR_REDO = timedelta(hours=3)
JOB_MISFIRE_S = 60
RESET_ACTOR = "scheduled-reset"


def lot_timezone(config: LotConfig) -> ZoneInfo:
    """The lot's IANA time zone; UTC (with a warning) if it isn't a valid one."""
    try:
        return ZoneInfo(config.lot.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("lot.timezone %r is not an IANA time zone: using UTC", config.lot.timezone)
        return ZoneInfo("UTC")


def vacuum(engine: Engine) -> None:
    """`PRAGMA optimize; VACUUM` (outside a transaction)."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("PRAGMA optimize"))
        conn.execute(text("VACUUM"))


class MaintenanceJobs:
    def __init__(
        self,
        engine: Engine,
        config: LotConfig,
        clock: Clock,
        retention: rollups.Retention | None = None,
        ingestor: Ingestor | None = None,
        more: Sequence[tuple[LotConfig, Ingestor | None]] = (),
    ):
        """`config` / `ingestor` are the default lot's, `more` the other lots' (one set of
        jobs serves them all: the rollup tables are shared, multi-lot.md §4)."""
        self.engine = engine
        lots = [(config, ingestor), *more]
        configs = [c for c, _ in lots]
        self.configs = configs
        self.zone_ids = [z.id for c in configs for z in c.zones]
        # each lot's total pseudo-zone in the rollups -> its zone ids
        self.totals = {total_key(configs, c.lot.id): [z.id for z in c.zones] for c in configs}
        # zone id -> its enabled scheduled reset (flow zones only, config.md §1)
        self.resets = {
            z.id: z.reset for c in configs for z in c.zones if z.reset and z.reset.enabled
        }
        self.ingestor = ingestor
        # zone id -> the ingestor and time zone of its lot (the scheduled resets)
        self._ingestors = {z.id: i for c, i in lots for z in c.zones}
        self._zone_tz = {z.id: lot_timezone(c) for c in configs for z in c.zones}
        self.tz = lot_timezone(config)  # prune and vacuum run on the default lot's clock
        self.clock = clock
        self.retention = retention or rollups.Retention()
        self._scheduler: AsyncIOScheduler | None = None

    @classmethod
    def for_lots(
        cls,
        engine: Engine,
        lots: Sequence[LotConfig],
        clock: Clock,
        retention: rollups.Retention | None = None,
    ) -> MaintenanceJobs:
        """The jobs of every lot of a config file, without ingestors (the CLI: no resets)."""
        return cls(engine, lots[0], clock, retention, more=[(c, None) for c in lots[1:]])

    # --- synchronous cores ---

    def run_minutes(self, now: datetime) -> int:
        end = rollups.floor_minute(now)
        with session_scope(self.engine) as session:
            last = rollups.last_bucket(session, ZoneMinute)
            start = end - MINUTE_CATCH_UP
            if last is not None:
                start = max(start, min(last + rollups.MINUTE, end - rollups.MINUTE))
            return rollups.aggregate_minutes(session, self.zone_ids, start, end, self.totals)

    def run_hours(self, now: datetime) -> int:
        end = rollups.floor_hour(now)
        with session_scope(self.engine) as session:
            return rollups.aggregate_hours(session, end - HOUR_REDO, end)

    def run_prune(self, now: datetime) -> dict[str, int]:
        with session_scope(self.engine) as session:
            deleted = rollups.prune(session, now, self.retention)
        log.info("prune: %s", ", ".join(f"{t} {n}" for t, n in deleted.items()))
        return deleted

    def run_vacuum(self) -> None:
        vacuum(self.engine)
        log.info("vacuum done")

    def run_forecast(self, now: datetime, weather_client=None) -> list[forecast_model.Report]:
        """Per lot with `forecast.model`: refresh its weather (if `forecast.weather`), then
        train and check a model for each zone and the lot's total (forecast.md §5). A failed
        weather request is logged; training goes on with what is stored."""
        reports = []
        for config in self.configs:
            cfg, lot_id = config.forecast, config.lot.id
            if not cfg.model:
                continue
            total = total_key(self.configs, lot_id)
            zone_ids = [*self.totals[total], total]
            if cfg.weather:
                with session_scope(self.engine) as session:
                    since = forecast_model.oldest_hour(session, zone_ids)
                try:
                    location = config.lot.location
                    hours = weather.refresh(
                        self.engine, lot_id, location.lat, location.lon, now, since, weather_client
                    )
                    log.info("forecast: %d weather hour(s) stored for lot %s", hours, lot_id)
                except Exception as e:
                    log.warning("forecast: weather refresh failed for lot %s: %s", lot_id, e)
            cal = calendar_of(cfg)
            tz = lot_timezone(config)
            with session_scope(self.engine) as session:
                wx = weather.load(session, lot_id) if cfg.weather else {}
            for zone_id in zone_ids:
                with session_scope(self.engine) as session:
                    reports.append(forecast_model.train(session, zone_id, tz, cal, wx, now))
        return reports

    async def run_reset(self, zone_id: str) -> None:
        """Set the zone's count to its `reset.value` (a `correction` row, published)."""
        value = self.resets[zone_id].value
        row = await self._ingestors[zone_id].correct(
            zone_id, value, RESET_ACTOR, f"scheduled reset ({self.resets[zone_id].cron})", "reset"
        )
        log.info("scheduled reset: zone %s %d -> %d", zone_id, row.old_occupied, row.new_occupied)

    # --- scheduling ---

    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        jobs = {
            "aggregate-minutes": (self._job(self.run_minutes), CronTrigger(second=5)),
            "aggregate-hours": (self._job(self.run_hours), CronTrigger(minute=2)),
            "prune": (self._job(self.run_prune), CronTrigger(hour=4, timezone=self.tz)),
            "vacuum": (
                self._job(lambda _now: self.run_vacuum()),
                CronTrigger(day_of_week="sun", hour=4, minute=30, timezone=self.tz),
            ),
        }
        if any(c.forecast.model for c in self.configs):
            jobs["forecast"] = (
                self._job(self.run_forecast),
                CronTrigger(hour=3, minute=30, timezone=self.tz),
            )
        for zone_id, reset in self.resets.items():
            if self._ingestors[zone_id] is None:
                log.warning("no ingestor: the scheduled reset of %s is off", zone_id)
                continue
            jobs[f"reset-{zone_id}"] = (
                self._reset_job(zone_id),
                crontab_trigger(reset.cron, self._zone_tz[zone_id]),
            )
        for job_id, (func, trigger) in jobs.items():
            self._scheduler.add_job(
                func,
                trigger,
                id=job_id,
                name=job_id,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=JOB_MISFIRE_S,
            )
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._scheduler = None

    def _reset_job(self, zone_id: str):
        async def run() -> None:
            try:
                await self.run_reset(zone_id)
            except Exception:
                log.exception("scheduled reset of zone %s failed", zone_id)

        return run

    def _job(self, core):
        async def run() -> None:
            try:
                await anyio.to_thread.run_sync(core, self.clock.now())
            except Exception:
                log.exception("maintenance job failed")

        return run
