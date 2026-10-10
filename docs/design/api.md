# API, live stream and internal contracts

Base URL in production: `https://parking-api.<domain>` (see [deployment.md](deployment.md)). All JSON uses UTC ISO 8601 timestamps with `Z`.

## 1. Shared types

### LotStatus
Returned by `GET /api/status` and sent in every SSE `status` event.

```json
{
  "v": 1,
  "lot": "main",
  "updated_at": "2026-10-07T17:05:12Z",
  "total": { "capacity": 100, "occupied": 77, "free": 23, "level": "plenty", "confidence": 0.86, "stale": false },
  "zones": [
    {
      "id": "ground", "name": "Ground", "method": "slots",
      "capacity": 40, "occupied": 28, "free": 12,
      "level": "filling", "confidence": 1.0, "stale": false,
      "trend": "filling", "updated_at": "2026-10-07T17:05:12Z",
      "slots": { "G01": true, "G02": false }
    },
    {
      "id": "underground", "name": "Underground", "method": "flow",
      "capacity": 60, "occupied": 49, "free": 11,
      "level": "filling", "confidence": 0.86, "stale": false,
      "trend": "steady", "updated_at": "2026-10-07T17:04:58Z",
      "slots": null
    }
  ]
}
```

| Field | Notes |
|-------|-------|
| `name` | Already resolved to the requested language (`?lang=` or `Accept-Language`, falling back to `en`) |
| `total.confidence` | The lowest zone confidence |
| `total.stale` | `true` if **any** zone is stale |
| `slots` | Map slot id → taken, only for `slots` zones (used by the Phase 9 slot map) |
| `trend` | `filling` / `emptying` / `steady` (see [vision.md §8](vision.md#8-fusion-confidence-trend-parkingcorefusionpy)) |
| `updated_at` (zone) | `null` until the zone has received data |

### Levels
| `level` | Rule (`free / capacity`) | UI word | Colour token |
|---------|-------------------------|---------|--------------|
| `plenty` | ≥ `levels.plenty` (0.20) | Plenty of space | `--ok` |
| `filling` | ≥ `levels.filling` (0.05) | Filling up | `--warn` |
| `almost_full` | > 0 | Almost full | `--bad` |
| `full` | = 0 | Full | `--bad` |

The level is computed **by the server**. The client only maps it to words and colours.

### Error format
```json
{ "error": { "code": "not_found", "message": "Zone 'roof' does not exist" } }
```
Codes: `bad_request` (400; also 422 for a body or query that fails validation, with `details`: `[{"type","loc","msg"}]`), `unauthorized` (401), `forbidden` (403), `not_found` (404, also unknown paths), `not_enough_data` (404, `/api/forecast` without 3 weeks of data), `method_not_allowed` (405), `conflict` (409), `rate_limited` (429, with `Retry-After` seconds), `unavailable` (503: no data received yet since start-up), `internal` (500, message `internal server error`; details only in the log). Every error, including FastAPI's own, uses this format (exception handlers in `parking/api/app.py`, P2.9).

---

## 2. Public REST endpoints

| Method | Path | Phase | Description |
|--------|------|-------|-------------|
| GET | `/healthz` | 2 | `{"status":"ok","db":true,"cameras":{"cam-ground":"ok","cam-ramp":"unknown"},"restarts":{"cam-ground":0,"cam-ramp":0},"ingest":{"observations":n,"flow_events":n,"health":n,"rejected":n,"db_errors":n},"stream":{"clients":n,"published":n,"dropped":n}}`. `unknown` = no health message since start-up; `restarts` = times each camera's worker came back as a new process since the API started (a new `started_at` in its health messages; the watchdog of [deployment.md §4.2](deployment.md#42-watchdog-for-stuck-workers-p85) or a crash); counters since start-up (`stream.dropped` = events dropped from full SSE client queues). HTTP 200 even when cameras are down, because the API itself is alive |
| GET | `/api/lot` | 2 | Static lot info (below). `location` uses `LOT_LAT`/`LOT_LON` from the settings when set, else lot.yaml; zone `capacity` is the effective one (slot count for `slots` zones) |
| GET | `/api/status` | 2 | `LotStatus`; `503 unavailable` until the first data since start-up, unless state was restored from the DB (then 200 with `stale: true`). `Cache-Control: no-cache` |
| GET | `/api/stream` | 2 | SSE (see §3) |

Zone names (`/api/lot`, `/api/status`, `/api/stream`): `?lang=` first, then the `Accept-Language` languages by `q` (primary subtag only: `ro-RO` → `ro`; `q=0` and `*` ignored); the first one that any zone has a name in wins, else `en` (a zone without that name falls back to `en`, then its first name). `/api/lot` and `/api/status` send `Vary: Accept-Language`. `?lang=` longer than 35 characters → 422.
| GET | `/api/history` | 7 | Query: `zone` (a zone id or `total`, default `total`), `from`, `to` (ISO 8601; naive = UTC; default `to` = now, `from` = `to` − 24 h, or − 30 days for `day`), `bucket=minute\|hour\|day` (default `hour`). Returns `{"zone":"ground","bucket":"hour","from":"…","to":"…","points":[{"t":"…","free_avg":12.4,"free_min":8,"free_max":15,"occupied_avg":27.6}]}`: `minute` from `zone_minute`, `hour` from `zone_hour`, `day` = lot-local days (lot.yaml `timezone`) of `zone_hour` weighted by its `samples`; `t` = bucket start (UTC), buckets from the one holding `from` up to before `to`, empty buckets left out. Unknown zone `404 not_found`; `from` ≥ `to` or more than **2000** buckets `422 bad_request` |
| GET | `/api/forecast` | 7 | Query: `zone` (default `total`), `at` (ISO, default now + 30 min). Returns `{"zone":"ground","at":"…","free_expected":10,"basis":"median of last 8 same weekday/hour","samples":6}`: the median `free_avg` of the `zone_hour` rows for the same lot-local weekday and hour 1…8 weeks before `at` (wall-clock, so it follows DST), rounded and kept within 0…capacity; `samples` = how many of the 8 had data. Fewer than 3 → `404 not_enough_data`; unknown zone `404 not_found` |

`/api/history` and `/api/forecast` answers are cached in memory for 60 s per API process (keyed by the query as sent, so a default `to`/`at` can be up to 60 s old) and sent with `Cache-Control: public, max-age=60` (P7.6, `parking/db/history.py`, `parking/api/cache.py`).
| GET | `/api/push/vapid-public-key` | 6 | `{"key":"BAx…"}`; `503 unavailable` when the VAPID keys aren't set |
| POST | `/api/push/subscriptions` | 6 | Body: `{"subscription": <PushSubscription JSON>, "prefs": Prefs, "tz": "Europe/Bucharest", "lang": "en"}` → 201 `{"id":"…"}`. Upserts by endpoint (same `id`; keys, prefs, tz, lang replaced, `failures` reset) |
| PATCH | `/api/push/subscriptions` | 6 | Body: `{"endpoint":"…","prefs":Prefs}` → 200 `{"id","prefs"}` (prefs replaced as a whole); unknown endpoint 404 |
| DELETE | `/api/push/subscriptions` | 6 | Body: `{"endpoint":"…"}` → 204, also for an unknown endpoint (idempotent) |
| POST | `/api/push/on-my-way` | 6 | Body: `{"endpoint":"…","minutes":30}` (5–120, or `0` = cancel) → 202 `{"until":"…Z"\|null,"sent":bool}`, plus an immediate push (`Urgency: high`; none on cancel). Each call starts a new window (its 6-push cap starts over); after it, pushes follow the rules in notifications.md §4 |
| POST | `/api/push/test` | 6 | Body: `{"endpoint":"…"}`. Sends one test push with the current status → 200 `{"sent":bool,"deleted":bool}`. Rate limit 3/hour per endpoint |

Push routes (`parking/api/routes/push.py`, P6.2): the subscription is found by `endpoint`; every call with one updates `last_seen_at`; an unknown endpoint is 404 (except DELETE). Without `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY`/`VAPID_SUBJECT` the key, test and on-my-way routes answer `503 unavailable` (subscriptions are still stored). Validation (422): `endpoint` an `https://` URL ≤ 2048 chars; `keys.p256dh`/`keys.auth` base64url; `tz` a valid IANA zone (`zoneinfo`); `lang` a language tag; unknown fields in `Prefs` (and the bodies) are rejected, other `PushSubscription` fields (`expirationTime`) ignored. The push payload is built by `parking/push/payload.py` from the current status in the subscription's `lang`, `tz` and `prefs.zones` (`"No data yet"` before the first data).

`GET /api/lot`:
```json
{
  "v": 1, "id": "main", "name": "Parking",
  "location": { "lat": 51.5007, "lon": -0.1246 }, "notify_radius_m": 500,
  "timezone": "Europe/Bucharest",
  "zones": [ { "id": "ground", "name": "Ground", "method": "slots", "capacity": 40 },
             { "id": "underground", "name": "Underground", "method": "flow", "capacity": 60 } ],
  "levels": { "plenty": 0.2, "filling": 0.05 },
  "privacy": { "operator": null, "contact": null }
}
```
`privacy` (P8.9) is who runs the cameras, from `PRIVACY_OPERATOR` / `PRIVACY_CONTACT` in `.env` (`null` when unset); the app's Privacy screen shows it. Clients accept an answer without it.

`Prefs` (notification preferences, all optional):
```json
{
  "proximity": true,
  "radius_m": 500,
  "zones": ["ground", "underground"],
  "schedules": [ { "days": [1, 2, 3, 4, 5], "time": "08:30" } ],
  "quiet_hours": { "from": "22:00", "to": "07:00" },
  "alert_when_almost_full": true
}
```
(`days`: 1 = Monday … 7 = Sunday, in the subscription's `tz`.) Rules: `radius_m` 100–5000 (`null` = the lot's `notify_radius_m`); `zones` ids from lot.yaml (`null` = all); `time`, `from`, `to` are `HH:MM` (00:00–23:59); `days` 1–7, 1–7 entries, stored sorted and unique; at most 10 schedules. Defaults when left out: `proximity` / `alert_when_almost_full` `false`, `radius_m` / `zones` / `quiet_hours` `null`, `schedules` `[]`; the stored prefs always have every key.

---

## 3. Live stream: `GET /api/stream` (Server-Sent Events)

Response headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

```
retry: 3000

event: status
id: 1739
data: {"v":1,"lot":"main","updated_at":"…","total":{…},"zones":[…]}

event: ping
data:

event: status
id: 1740
data: {…}
```

- **On connect:** send `retry`, then the current `status` immediately (if there is one). Zone names follow `?lang=` / `Accept-Language` like the REST routes; the broadcaster serialises each event once per language (`Broadcaster.render`).
- **On change:** a `status` event with the full `LotStatus` (it's small, so there are no diffs to get wrong).
- **Heartbeat:** an `event: ping` with empty `data:` every `sse_ping_s` (15 s), which keeps proxies and tunnels from closing idle connections and lets the app's watchdog (frontend.md §3) see that a quiet stream is still alive. A named event, not a `: ping` comment, because `EventSource` never exposes comments; clients that only listen for `status` ignore it.
- `id` is a monotonic counter. Clients don't need `Last-Event-ID` replay, because the first event is always the full current state.
- Server side: one `Broadcaster` holding an `asyncio.Queue(maxsize=10)` per client. If a client's queue is full, drop its oldest message. One slow phone must never block others.
- Implementation: `sse-starlette` `EventSourceResponse` (`parking/api/sse.py` `Broadcaster`, P2.8). The `Ingestor` publishes after every change (and once at start-up after the restore), so the "current status" exists from start-up on; restored or never-fed zones show `stale: true`. The status is serialised once per publish; `id` restarts at 1 when the API restarts. The current status is read together with the subscribe, so a client never misses or repeats an event.
- **Limits:** at most 1000 clients per API process; above that `503 unavailable` (`{"error": …}`, the browser's `EventSource` retries). A client that can't take an event or ping for 30 s is dropped. On a disconnect its queue is removed at once (`stream.clients` in `/healthz`).

---

## 4. Admin endpoints

**Auth:** `Authorization: Bearer <token>`. The token is either:
- the static `ADMIN_TOKEN` from `.env` (scripts, and Phases 5–6 before login exists), or
- a session token from `POST /api/admin/login` (random 32 bytes, stored hashed in `admin_session`, valid 7 days).

Anything else (missing, wrong, expired, revoked) is `401 unauthorized` with `WWW-Authenticate: Bearer`. Without `ADMIN_PASSWORD_HASH` the login answers `503`. All login attempts (right or wrong) count toward the 5 / 15 min. The frontend keeps the token in `sessionStorage`; any 401 on an admin call forgets it and shows the login again (`parking/api/routes/admin.py` `require_admin`, `frontend/src/api/client.ts`, P7.1).

Why not cookies: the frontend and the API can be on different sites (e.g. the GitHub Pages preview and a separate API host). Browsers, Safari especially, block third-party cookies, and cookies would also need CSRF protection. A bearer token kept in memory or `sessionStorage` avoids both.

| Method | Path | Phase | Description |
|--------|------|-------|-------------|
| POST | `/api/admin/login` | 7 | `{"password":"…"}` → `{"token":"…","expires_at":"…"}`. 5 attempts / 15 min / IP |
| GET | `/api/admin/session` | 7 | Checks the token: `{"actor":"session:<id>"\|"admin-token","expires_at":"…"\|null}` (the admin screen calls it on open) |
| POST | `/api/admin/logout` | 7 | Revokes the calling session token → `204` (a no-op for `ADMIN_TOKEN`) |
| GET | `/api/admin/cameras` | 7 | `[{"id","role","zones","state":"ok|degraded|down|unknown","issue","fps","last_frame_age_s","inference_ms_avg","unhealthy_ratio","last_health_age_s","snapshot"}]`, in lot.yaml order (see below) |
| GET | `/api/admin/cameras/{id}/snapshot?annotated=true` | 7 | `image/jpeg` with `Cache-Control: no-store` (proxied from the worker's `/control/snapshot`, 5 s timeout → 503) |
| GET | `/api/admin/cameras/{id}/slots` | 7 | Slot file JSON |
| PUT | `/api/admin/cameras/{id}/slots` | 7 | Validates, writes `config/slots/<id>.json`, keeps a `.bak`, calls the worker's `/control/reload` |
| GET / PUT | `/api/admin/cameras/{id}/lines` | 7 | Same, for line files |
| POST | `/api/admin/cameras/{id}/reference-frame` | 7 | Saves the current frame as the shift-detection reference |
| POST | `/api/admin/zones/{id}/correct` | 7 | `{"occupied": 37, "note": "manual count"}` → new zone status. `flow` zones only (409 otherwise) |
| GET | `/api/admin/corrections?limit=50` | 7 | Audit log, newest first |
| GET | `/api/admin/alerts?endpoint=…` | 7 | `{"enabled": bool, "available": bool, "issues": [{"key","kind","subject","detail","since","active","last_alert_at"}]}` (see below) |
| PUT | `/api/admin/alerts` | 7 | `{"endpoint":"…","enabled":true}` → `{"enabled":true}`; unknown endpoint `404` |

**Cameras and snapshots** (`parking/api/routes/admin.py`, P7.2):
- The list comes from each camera's latest health message: `state` is `unknown` (other fields `null`) until a worker has reported. `last_frame_age_s` is the worker's value **plus** the seconds since that message arrived, so a silent worker's frame keeps ageing; `last_health_age_s` is the time since the message itself. `snapshot` is `true` when the camera has a `control_url`. Numbers are rounded to 0.1.
- The snapshot is a plain HTTP GET to `<control_url>/control/snapshot` with `WORKER_TOKEN` (§5.2): `annotated` defaults to `true`. Unknown camera `404`; no `control_url`, no `WORKER_TOKEN`, no answer in 5 s, any non-200 (e.g. the worker's own `503` before its first frame) or a body that isn't a JPEG → `503 unavailable` with a message saying which. The full frame is passed through unscaled (at most 20 MB).
- The frontend fetches it with the bearer header and shows it through a blob URL (an `<img src>` can't send headers), revoking the previous URL on each new picture.

**Slot / line editor** (`parking/api/routes/admin.py`, P7.3):
- `GET …/slots` (occupancy cameras) and `GET …/lines` (flow cameras) return the stored file as is. The other kind for a camera, or no file yet → `404 not_found`; a file that isn't JSON → `409 conflict`. Paths are lot.yaml's `slots_file` / `lines_file` under the app root.
- `PUT` takes the whole file and validates it with the loader's models (`SlotFile` / `LineFile`: unknown keys, < 3 points, self-crossing polygons, duplicate ids) plus: `camera_id` must be the path's camera, every slot / count-zone `zone` must be one of that camera's zones, and slot ids must not be used by another camera's slot file. Any failure → `422 bad_request` and nothing is written.
- Then the file is written atomically in the slot editor's layout (one slot per line), the previous one kept as `<file>.bak` (git-ignored), and the worker's `/control/reload` is called (5 s). Answer `200 {"saved": "config/slots/cam-ground.json", "backup": true, "reloaded": true, "message": null, "slots": 17}` (`slots` only for slot files). A worker that can't be reached or refuses the file gives `reloaded: false` and its reason in `message`; the file stays saved (the worker reads it when it restarts).
- After a slot save the API also swaps its own slot ids and `slots`-zone capacities (`StateStore.replace_slot_file`): removed slots stop counting at once, new ones count from the worker's next reading, and the new status is published (`zone_state.source = config`).
- `POST …/reference-frame` calls the worker's `/control/save-reference`: `200` with the worker's `{"saved", "ts"}`; the worker's `409` (no frame yet) → `409 conflict`; unreachable or any other error → `503 unavailable`.

**Count corrections** (`parking/api/routes/admin.py`, P7.4; built ahead of P5.7):
- `POST …/zones/{id}/correct` body `{"occupied": int ≥ 0, "note": str ≤ 200 chars (optional, trimmed)}`, unknown keys rejected. Unknown zone `404`; a `slots`/`count` zone `409 conflict` (those are measured from the picture); `occupied` above the zone's capacity or a bad body `422 bad_request`. Nothing changes on an error.
- On success `FlowCounter.correct` sets the count and resets the confidence counters (confidence 1.0), the zone's `updated_at` becomes now (so a fresh API with no data yet starts answering `/api/status`), a `correction` row (actor `admin-token` or `session:<id>`) and a `zone_state` row (`source: correction`) are written, and the new status is published on `/api/stream` at once. Answer: the zone's `ZoneStatus` (in `?lang=`).
- `GET …/corrections?limit=50` (1–200, else `422`): `[{"id", "ts", "zone_id", "zone_name", "old_occupied", "new_occupied", "actor", "note"}]`, newest first; `zone_name` in `?lang=` / `Accept-Language` (the id when the zone has left lot.yaml).
- After a restart a flow zone's confidence continues from its last correction: `corrected_at` = that row's `ts`, events since = `flow_event` rows of the zone after it.

**Admin alerts** (`parking/api/routes/admin.py` + `parking/push/admin_alerts.py`, P7.8; rules in [notifications.md §5.1](notifications.md#51-admin-alerts-p78-p87)):
- `PUT /api/admin/alerts` sets `push_subscription.admin_alerts` for the subscription with that `endpoint` (this browser's, from the Alerts screen); unknown keys `422`, unknown endpoint `404`. Re-subscribing the same endpoint (`POST /api/push/subscriptions`) keeps the flag; a new endpoint (the browser renewed it) starts without it.
- `GET /api/admin/alerts`: `enabled` is that subscription's flag (`false` for no or an unknown `endpoint`), `available` whether push is configured (no VAPID keys = no alerts), `issues` the open ones, oldest first: `kind` `camera_down | camera_shifted | stale | clamps | disk | cpu_temp | api_restarted | backup_failed`, `subject` the camera or zone id (for the machine issues of P8.7: `api` or the camera id of the worker's machine), `detail` the camera issue, the clamp count, `91%`, `82 °C` or the backup's error, `since` when first seen, `active` past its grace period, `last_alert_at` the last alert for that issue (or `null`).

---

## 5. Internal endpoints (workers ↔ API)

Plain HTTP on the parking app's own private Docker network. There is **no message broker**.

### 5.1 Workers → API

Auth: `Authorization: Bearer <WORKER_TOKEN>` (from `.env`). These routes are **never** reachable from the internet: the tunnel only forwards `/api/*` and `/healthz` ([deployment.md §5](deployment.md#5-public-access-for-the-api)), and the token is checked as well.

| Method | Path | Body | Response | Sent |
|--------|------|------|----------|------|
| POST | `/internal/observations` | `observation` | 204 | after each analysed frame (occupancy) |
| POST | `/internal/flow-events` | `{"events": [flow, …]}` (1–100) | 200 `{"accepted": n, "duplicates": m}` | as soon as events happen (flow) |
| POST | `/internal/health` | `health` | 204 | every 10 s (all workers) |

Delivery rules:
- **Observations:** if the API is unreachable, the worker drops the observation (only the latest matters) and keeps going.
- **Flow events:** must not be lost. The worker keeps an **outbox** (in memory, plus `data/outbox/<camera>.jsonl` on disk so it survives a worker restart). It retries with backoff (1 → 30 s) and removes events once the API accepts them. `event_id` makes retries safe: duplicates are ignored.
- **Client** (`parking/workers/api_client.py`, P2.1): 5 s timeout; the outbox file is rewritten atomically after each accepted batch and deleted when empty; on start-up a torn last line (crash mid-write) and repeated `event_id`s are skipped. Every failure (network, 5xx, also 4xx) is retried with backoff, so a misconfigured token never loses events. Print mode writes each payload (flow events as a `{"events": […]}` batch) as one JSON line to stdout and sends nothing.
- **Payload models:** `parking/messages.py`. Unknown fields are ignored (forward compatibility); timestamps are serialised as UTC with milliseconds and `Z`, and a timestamp without a zone is read as UTC.
- **API side** (`parking/api/routes/internal.py` → `parking/api/ingest.py`, P2.7): the token is checked first (constant-time; no `WORKER_TOKEN` set = every request 401), then the body is parsed, so a request without the token always gets `401 unauthorized`. A payload that doesn't validate, or names a camera that isn't in lot.yaml with the right role, gets **422** `{"error": {"code": "bad_request", "message", "details"}}`; it's logged and counted in `/healthz` (`ingest.rejected`). Every payload goes through one lock: `StateStore` → DB rows (in a thread; a DB error is logged and counted, the in-memory state is kept) → publish the status. Flow-event duplicates are ids already in memory **or** in `flow_event` (survives restarts); clamped events are stored with `applied=false` and count as accepted.
- **Crashed worker:** there's no "last will" message. The API marks a camera `down` when it hasn't received a health message for 30 s, and its zones become `stale` after `stale_after_s`.

### 5.2 API → workers (control)

Each worker runs a tiny HTTP server on port **9000**, reachable only inside the private Docker network. The API calls it with the same `WORKER_TOKEN`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/control/snapshot?annotated=true` | Current frame as `image/jpeg` (admin snapshots) |
| POST | `/control/reload` | Re-read config and the slot/line file |
| POST | `/control/save-reference` | Save the current frame as the shift-detection reference |

Server: `parking/workers/control.py` (P2.3), stdlib `http.server` in a thread; `parking worker … --control-port` (default 9000, `0` = off) and `--control-host` (default `0.0.0.0`; the port is never published outside the Docker network). **Without `WORKER_TOKEN` the server isn't started** (a warning is logged), so there is never an open control port. Responses:
- `snapshot`: `annotated` accepts `true/1/yes/on`; the latest frame read (also an unhealthy one, so an admin can see why), annotated with the last analysis of that frame; `503 unavailable` before the first frame.
- `reload`: `200 {"camera_id", "slots", "interval_s"}`; re-reads lot.yaml (this camera's section), the slot file and `reference_empty`; the detector, the slot classifier and the source are rebuilt only if their config changed. Anything invalid → `400 bad_request` and the old setup keeps running. Flow workers (P5.6) answer `200 {"camera_id", "in_direction"}` after re-reading lot.yaml and the line file (fresh motion gate and counter, tracker reset with ids still rising).
- `save-reference`: `200 {"saved": "data/reference/<camera>.jpg", "ts"}`; `409 conflict` before the first frame. Later frames are compared with it and `shifted` clears.
- Missing/wrong token `401 unauthorized`, unknown route `404 not_found`; errors use the §1 format.

### observation
```json
{
  "v": 1, "camera_id": "cam-ground", "ts": "2026-10-07T17:05:12.120Z",
  "frame_size": [2560, 1440], "inference_ms": 143, "detections": 31,
  "slots": [ { "id": "G01", "score": 0.82, "taken": true }, { "id": "G02", "score": 0.04, "taken": false } ],
  "zone_counts": { }
}
```
`zone_counts` is filled only for `count`-method zones: `{"yard": 7}`.

### flow
```json
{
  "v": 1, "event_id": "6f1c2a1e-…", "camera_id": "cam-ramp", "ts": "2026-10-07T17:06:01.480Z",
  "direction": "in", "track_id": 4412, "cls": "car", "confidence": 0.77
}
```

### health (every 10 s)
```json
{
  "v": 1, "camera_id": "cam-ground", "ts": "…",
  "state": "ok", "issue": null,
  "fps": 0.2, "last_frame_age_s": 3.1, "inference_ms_avg": 151, "unhealthy_ratio": 0.0,
  "gate_active_ratio": null,
  "started_at": "2026-10-07T09:00:00.000Z",
  "disk_pct": 41.5, "cpu_temp_c": 58.4
}
```
`started_at` (P8.5, optional): when this worker process started. A different value than in the camera's previous message means the worker was restarted; the API counts it (`restarts` in `/healthz`). Messages without it are never counted.
`disk_pct` / `cpu_temp_c` (P8.7, optional, `null` when unknown): the worker's machine, i.e. the used share (0–100) of the filesystem holding its `data/` and the hottest CPU thermal zone in °C. The API only uses them for the `disk` / `cpu_temp` admin alerts ([notifications.md §5.1](notifications.md#51-admin-alerts-p78-p87)); they aren't stored.
`gate_active_ratio` (flow workers only, P5.6, else `null`): share of the last 10 s of frames the motion gate let through to the detector. A flow worker's `fps` is frames processed per second over the last 10 s and `inference_ms_avg` the mean gate + detector + tracker time of the frames the gate let through (`null` while the ramp is quiet). Its frame-health check runs once a second without the `frozen` check (a quiet ramp looks the same for minutes); no new frame for 5 s is `connect_failed`.
`state`: `ok | degraded | down`. `issue`: `null | black | frozen | blurry | shifted | connect_failed`. A `shifted` camera (vision.md §6) is `degraded` (unless `down`) and reports `issue: shifted` while its frames are otherwise healthy.

---

## 6. Push notification payload (Web Push, encrypted by pywebpush)

```json
{
  "title": "Parking: 23 free",
  "body": "Ground 12 · Underground ≈11 · 17:05",
  "tag": "parking-status",
  "url": "<PUBLIC_APP_URL>#/",
  "level": "plenty",
  "kind": "on_my_way"
}
```
`tag` makes a new notification replace the previous one instead of stacking. `kind`: `test | on_my_way | schedule | almost_full | admin_alert`. An `admin_alert` (P7.8) has an English title starting `Admin: `, `level: null`, a per-issue `tag` (`admin-<kind>:<id>`, so its hourly repeat and its "resolved" replace it), `url` the camera page (`#/admin/cameras/<id>`) or `#/admin`, plus `"issue": "<kind>:<id>"` and `"resolved": bool`.

---

## 7. Cross-cutting

- **CORS:** allow only `CORS_ORIGINS`; methods GET/POST/PATCH/PUT/DELETE; headers `Content-Type, Authorization`. Empty `CORS_ORIGINS` = no CORS headers at all (same-origin only).
- **Rate limits** (per client IP, moving window, in memory per API process): public GET 120/min (one budget shared by `/healthz` and all public `/api/*` GETs, incl. connecting to `/api/stream`; the push `PATCH`/`DELETE` use it too, so a settings screen saving often isn't cut off), push POST 20/hour (one budget for `subscriptions`, `on-my-way` and `test`), test push 3/hour per endpoint, login 5/15 min; `/internal/*` has none. Implemented as a FastAPI dependency (`rate_limit(limit, group)` in `parking/api/deps.py`) on the `limits` library, the engine behind slowapi: slowapi's decorators need a module-global limiter and its middleware is a `BaseHTTPMiddleware`. The client IP is `request.client.host`; behind the tunnel/proxy uvicorn must trust the proxy's `X-Forwarded-For`, otherwise every visitor shares one budget: uvicorn's proxy headers are on by default and `FORWARDED_ALLOW_IPS` says whose to trust, set by `docker-compose.server.yml` to the proxy's Docker network ([deployment.md §5 Option B](deployment.md#option-b-reverse-proxy-on-a-machine-with-a-public-ip-t2t3-server)).
- **Compression:** gzip for responses > 1 KB (Starlette's `GZipMiddleware`, which never compresses `text/event-stream`).
- **OpenAPI docs** at `/docs` (and `/openapi.json`) only when `LOG_LEVEL=DEBUG`; otherwise both are 404. No ReDoc.
