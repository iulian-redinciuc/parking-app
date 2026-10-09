# Data model (SQLite)

- File: `data/db/parking.sqlite`, opened in **WAL mode** (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`) so reads never block writes.
- ORM: **SQLModel** (SQLAlchemy 2). Migrations: **Alembic** from Phase 2 (`parking db upgrade` runs at API start-up).
- Only the API process writes. Workers never touch the DB.
- All timestamps are UTC (`datetime` with tz, stored as fixed-width ISO text, `2026-10-08T18:03:09.123456+00:00`, so they sort as strings).
- Migrations live in `backend/migrations/` (`backend/alembic.ini`). The URL comes from `PARKING_DB_URL`, else `data/db/parking.sqlite` under the repo root.

## 1. Tables

### `zone_state`: a zone's count, written only when it changes
| Column | Type | Notes |
|--------|------|-------|
| id | int PK | |
| ts | datetime | indexed |
| zone_id | text | indexed with ts: `(zone_id, ts)` |
| occupied | int | |
| free | int | |
| confidence | real | |
| source | text | the `StateStore` change source `observation \| flow \| health \| tick \| config \| correction` (`config` = an admin saved a new slot file, P7.3; `correction` = an admin set a flow zone's count, P7.4); later `reset`. `startup` changes are not written (they republish restored rows) |

### `slot_state`: a slot's smoothed state, written only when it flips
| Column | Type | Notes |
|--------|------|-------|
| id | int PK | |
| ts | datetime | |
| camera_id | text | |
| slot_id | text | index `(slot_id, ts)`; latest per `(camera_id, slot_id)` is restored |
| taken | bool | |

### `flow_event`
| Column | Type | Notes |
|--------|------|-------|
| event_id | text PK | from the worker (idempotency) |
| ts | datetime | indexed |
| camera_id | text | |
| zone_id | text | |
| direction | text | `in \| out` |
| track_id | int | |
| confidence | real | |
| applied | bool | false if ignored (e.g. clamped at 0 or capacity) |

### `correction`
| Column | Type | Notes |
|--------|------|-------|
| id | int PK | |
| ts | datetime | |
| zone_id | text | |
| old_occupied | int | |
| new_occupied | int | |
| actor | text | `admin-token`, `session:<id>`, `scheduled-reset` |
| note | text | |

### `zone_minute` / `zone_hour`: rollups for history and stats
| Column | Type | Notes |
|--------|------|-------|
| zone_id | text | PK part (`total` is stored as a pseudo-zone) |
| bucket_ts | datetime | PK part, start of the minute/hour (UTC) |
| free_avg | real | time-weighted average |
| free_min | int | |
| free_max | int | |
| occupied_avg | real | |
| samples | int | minute: values that lasted > 0 s in it; hour: minutes rolled up |

Migration `0005` (P7.5), primary key `(zone_id, bucket_ts)`. `total` = the sum of the zones that have a value at that time. A minute before a zone's first `zone_state` row is skipped; one known for part of it averages over that part. The hour row is the mean of its minutes' `free_avg` / `occupied_avg`, the min of their minimums and the max of their maximums.

### `push_subscription`
| Column | Type | Notes |
|--------|------|-------|
| id | text PK | uuid4 |
| endpoint | text unique | |
| p256dh | text | |
| auth | text | |
| prefs | json | see `Prefs` in [api.md](api.md#2-public-rest-endpoints) |
| tz | text | IANA timezone from the browser |
| lang | text | |
| created_at | datetime | |
| last_seen_at | datetime | updated on any API call with this endpoint |
| on_my_way_until | datetime null | |
| on_my_way_sent | int | pushes in the current on-my-way window (cap 6, notifications.md §4); reset by each `POST on-my-way`; migration `0003` (P6.6) |
| last_sent_at | datetime null | |
| last_sent_free | int null | for "changed significantly" checks: the watched zones' summed free count in the last on-my-way push |
| last_sent_level | text null | |
| failures | int | consecutive send failures; delete at 404/410 or ≥ 5 |

### `notification_log`
| id | ts | subscription_id | kind | payload json | status (`sent \| failed \| skipped_quiet`) | error |

`subscription_id` has no foreign key on purpose: the log row stays when the subscription is deleted after a 404/410. `ts` is indexed for the 30-day prune. The almost-full 2 h cooldown is read from it too (the last `almost_full` / `sent` row per subscription); `skipped_quiet` rows record reminders and almost-full alerts dropped by quiet hours (P6.7). Both push tables come from migration `0002` (P6.1).

### `admin_session`
| id | token_hash (sha256) | created_at | expires_at | revoked_at | ip | user_agent |

Migration `0004` (P7.1). `token_hash` is unique, `expires_at` indexed for the prune; the token itself is never stored. `id` is the audit actor `session:<id>`.

### `camera_health` (latest per camera; history goes to logs only)
| camera_id PK | ts | state | issue | fps | last_frame_age_s | inference_ms_avg |

## 2. In-memory state (API process)

| Object | Holds | Rebuilt on start-up from |
|--------|-------|--------------------------|
| `SlotSmoother` | smoothed state + pending counter per slot | latest `slot_state` per slot (pending counters start empty) |
| `FlowCounter` per flow zone | occupied, events/hours since correction | latest `zone_state` per flow zone; the latest `correction` for confidence counters |
| `StateStore` | current `LotStatus`, ring buffer of (ts, free) per zone for trend | latest `zone_state` rows; trend buffer from the `zone_state` rows of the last `trend_window_min` (exact, so `zone_minute` isn't used for it) |
| `Broadcaster` | SSE client queues | — |

At start-up, before any new observation arrives, the restored state is published with `stale=true`. It becomes live when fresh observations arrive.

## 3. Writes per day (sizing)

The occupancy camera sends ~17,000 observations a day but they're **not stored**. Only changes are. A 100-space lot sees a few hundred changes a day. Rollups add 1,440 minute rows and 24 hour rows per zone per day. Expect **well under 50 MB per year**.

## 4. Retention and rollups (APScheduler jobs in the API)

| Job | Schedule | Action |
|-----|----------|--------|
| `aggregate_minutes` | every minute (second 5) | Build `zone_minute` for the minutes since the last bucket (at most 10 back, always redoing the last one) from `zone_state`, time-weighted |
| `aggregate_hours` | hourly at :02 | Roll `zone_minute` → `zone_hour` for the last 3 complete hours |
| `prune` | daily 04:00 local | Delete `slot_state`, `zone_state`, `flow_event` > 90 days (always keeping the newest `zone_state` per zone and `slot_state` per slot: they're restored at start-up and carried into the next minute); `zone_minute` > 30 days; `notification_log` > 30 days; expired `admin_session`. `zone_hour` and `correction` are kept |
| `scheduled_reset` | per zone `reset.cron` | `FlowCounter.correct(value, actor="scheduled-reset")` (P5.7) |
| `vacuum` | weekly Sunday 04:30 local | `PRAGMA optimize; VACUUM` (fine at this size) |

The jobs live in `parking/api/jobs.py` (`MaintenanceJobs`, APScheduler, "local" = `lot.timezone`), the maths in `parking/db/rollups.py`. `zone_state` stores a zone's count only when it changes, so each zone is a step function and the minute rollup is time-weighted over it: free 10 for 45 s then 12 for 15 s → 10.5. Building the minutes from `zone_state` (not from memory) means the live job and a rebuild give the same rows, and every job can redo a range (its buckets are deleted first). Minutes missed while the API was down for more than 10 minutes stay empty.

CLI: `parking db aggregate` runs the minute and hour jobs once; `--backfill` deletes both tables and rebuilds them from all of `zone_state` (minutes only within the 30-day retention, hours for all of it; the last value is carried up to now). `parking db prune [--raw-days 90] [--minute-days 30] [--log-days 30] [--vacuum]` prunes with those periods.

## 5. Backups
`parking backup` uses SQLite's **online backup API** (`sqlite3.Connection.backup`), so it's safe while the API runs. It writes `parking-YYYYMMDD.sqlite` and a tarball of `config/`. See [deployment.md](deployment.md#7-backups).
