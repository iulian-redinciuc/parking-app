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
| source | text | the `StateStore` change source `observation \| flow \| health \| tick`; later `correction \| reset`. `startup` changes are not written (they republish restored rows) |

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
| samples | int | |

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
| last_sent_at | datetime null | |
| last_sent_free | int null | for "changed significantly" checks |
| last_sent_level | text null | |
| failures | int | consecutive send failures; delete at 404/410 or ≥ 5 |

### `notification_log`
| id | ts | subscription_id | kind | payload json | status (`sent \| failed \| skipped_quiet`) | error |

`subscription_id` has no foreign key on purpose: the log row stays when the subscription is deleted after a 404/410. `ts` is indexed for the 30-day prune. Both push tables come from migration `0002` (P6.1).

### `admin_session`
| id | token_hash (sha256) | created_at | expires_at | revoked_at | ip | user_agent |

### `camera_health` (latest per camera; history goes to logs only)
| camera_id PK | ts | state | issue | fps | last_frame_age_s | inference_ms_avg |

## 2. In-memory state (API process)

| Object | Holds | Rebuilt on start-up from |
|--------|-------|--------------------------|
| `SlotSmoother` | smoothed state + pending counter per slot | latest `slot_state` per slot (pending counters start empty) |
| `FlowCounter` per flow zone | occupied, events/hours since correction | latest `zone_state` per flow zone; the latest `correction` for confidence counters |
| `StateStore` | current `LotStatus`, ring buffer of (ts, free) per zone for trend | latest `zone_state` rows; trend buffer from the `zone_state` rows of the last `trend_window_min` (`zone_minute` once it exists, P7.5) |
| `Broadcaster` | SSE client queues | — |

At start-up, before any new observation arrives, the restored state is published with `stale=true`. It becomes live when fresh observations arrive.

## 3. Writes per day (sizing)

The occupancy camera sends ~17,000 observations a day but they're **not stored**. Only changes are. A 100-space lot sees a few hundred changes a day. Rollups add 1,440 minute rows and 24 hour rows per zone per day. Expect **well under 50 MB per year**.

## 4. Retention and rollups (APScheduler jobs in the API)

| Job | Schedule | Action |
|-----|----------|--------|
| `aggregate_minutes` | every minute | Build `zone_minute` for the previous minute from the in-memory timeline (time-weighted) |
| `aggregate_hours` | hourly at :02 | Roll `zone_minute` → `zone_hour` |
| `prune` | daily 04:00 local | Delete `slot_state`, `zone_state`, `flow_event` > 90 days; `zone_minute` > 30 days; `notification_log` > 30 days; expired `admin_session` |
| `scheduled_reset` | per zone `reset.cron` | `FlowCounter.correct(value, actor="scheduled-reset")` |
| `vacuum` | weekly Sunday 04:30 | `PRAGMA optimize; VACUUM` (fine at this size) |

## 5. Backups
`parking backup` uses SQLite's **online backup API** (`sqlite3.Connection.backup`), so it's safe while the API runs. It writes `parking-YYYYMMDD.sqlite` and a tarball of `config/`. See [deployment.md](deployment.md#7-backups).
