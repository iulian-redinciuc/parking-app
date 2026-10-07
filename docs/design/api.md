# API, live stream and MQTT contracts

Base URL in production: `https://parking-api.<domain>` (see [deployment.md](deployment.md)). All JSON uses UTC ISO 8601 timestamps with `Z`.

## 1. Shared types

### LotStatus
Returned by `GET /api/status`, sent in every SSE `status` event and published retained to `parking/<lot>/status`.

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
Codes: `bad_request` (400), `unauthorized` (401), `forbidden` (403), `not_found` (404), `conflict` (409), `rate_limited` (429), `unavailable` (503: no data received yet since start-up).

---

## 2. Public REST endpoints

| Method | Path | Phase | Description |
|--------|------|-------|-------------|
| GET | `/healthz` | 2 | `{"status":"ok","mqtt":true,"db":true,"cameras":{"cam-ground":"ok"}}`. HTTP 200 even when cameras are down, because the API itself is alive |
| GET | `/api/lot` | 2 | Static lot info (below) |
| GET | `/api/status` | 2 | `LotStatus`; `503 unavailable` before the first observation |
| GET | `/api/stream` | 2 | SSE (see §3) |
| GET | `/api/history` | 7 | Query: `zone` (or `total`), `from`, `to`, `bucket=minute\|hour\|day`. Returns `{"zone":"ground","bucket":"hour","points":[{"t":"…","free_avg":12.4,"free_min":8,"occupied_avg":27.6}]}`. Max 2000 points |
| GET | `/api/forecast` | 7 | Query: `zone`, `at` (ISO, default now + 30 min). Returns `{"zone":"ground","at":"…","free_expected":10,"basis":"median of last 8 same weekday/hour"}` |
| GET | `/api/push/vapid-public-key` | 6 | `{"key":"BAx…"}` |
| POST | `/api/push/subscriptions` | 6 | Body: `{"subscription": <PushSubscription JSON>, "prefs": Prefs, "tz": "Europe/Bucharest", "lang": "en"}` → 201 `{"id":"…"}`. Upserts by endpoint |
| PATCH | `/api/push/subscriptions` | 6 | Body: `{"endpoint":"…","prefs":Prefs}` |
| DELETE | `/api/push/subscriptions` | 6 | Body: `{"endpoint":"…"}` → 204 |
| POST | `/api/push/on-my-way` | 6 | Body: `{"endpoint":"…","minutes":30}` (5–120) → 202, plus an immediate push |
| POST | `/api/push/test` | 6 | Body: `{"endpoint":"…"}`. Sends one test push. Rate limit 3/hour per endpoint |

`GET /api/lot`:
```json
{
  "v": 1, "id": "main", "name": "Parking",
  "location": { "lat": 51.5007, "lon": -0.1246 }, "notify_radius_m": 500,
  "timezone": "Europe/Bucharest",
  "zones": [ { "id": "ground", "name": "Ground", "method": "slots", "capacity": 40 },
             { "id": "underground", "name": "Underground", "method": "flow", "capacity": 60 } ],
  "levels": { "plenty": 0.2, "filling": 0.05 }
}
```

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
(`days`: 1 = Monday … 7 = Sunday, in the subscription's `tz`.)

---

## 3. Live stream: `GET /api/stream` (Server-Sent Events)

Response headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

```
retry: 3000

event: status
id: 1739
data: {"v":1,"lot":"main","updated_at":"…","total":{…},"zones":[…]}

: ping

event: status
id: 1740
data: {…}
```

- **On connect:** send `retry`, then the current `status` immediately (if there is one).
- **On change:** a `status` event with the full `LotStatus` (it's small, so there are no diffs to get wrong).
- **Heartbeat:** a `: ping` comment every `sse_ping_s` (15 s), which keeps proxies and tunnels from closing idle connections.
- `id` is a monotonic counter. Clients don't need `Last-Event-ID` replay, because the first event is always the full current state.
- Server side: one `Broadcaster` holding an `asyncio.Queue(maxsize=10)` per client. If a client's queue is full, drop its oldest message. One slow phone must never block others.
- Implementation: `sse-starlette` `EventSourceResponse`.

---

## 4. Admin endpoints

**Auth:** `Authorization: Bearer <token>`. The token is either:
- the static `ADMIN_TOKEN` from `.env` (scripts, and Phases 5–6 before login exists), or
- a session token from `POST /api/admin/login` (random 32 bytes, stored hashed in `admin_session`, valid 7 days).

Why not cookies: the frontend (github.io) and API (your domain) are different sites. Browsers, Safari especially, block third-party cookies, and cookies would also need CSRF protection. A bearer token kept in memory or `sessionStorage` avoids both.

| Method | Path | Phase | Description |
|--------|------|-------|-------------|
| POST | `/api/admin/login` | 7 | `{"password":"…"}` → `{"token":"…","expires_at":"…"}`. 5 attempts / 15 min / IP |
| POST | `/api/admin/logout` | 7 | Revokes the session token |
| GET | `/api/admin/cameras` | 7 | `[{"id","role","state":"ok|degraded|down","issue","fps","last_frame_age_s","inference_ms_avg"}]` |
| GET | `/api/admin/cameras/{id}/snapshot?annotated=true` | 7 | `image/jpeg` (worker round trip via MQTT, 5 s timeout → 503) |
| GET | `/api/admin/cameras/{id}/slots` | 7 | Slot file JSON |
| PUT | `/api/admin/cameras/{id}/slots` | 7 | Validates, writes `config/slots/<id>.json`, keeps a `.bak`, sends `reload` to the worker |
| GET / PUT | `/api/admin/cameras/{id}/lines` | 7 | Same, for line files |
| POST | `/api/admin/cameras/{id}/reference-frame` | 7 | Saves the current frame as the shift-detection reference |
| POST | `/api/admin/zones/{id}/correct` | 5 | `{"occupied": 37, "note": "manual count"}` → new zone status. `flow` zones only (409 otherwise) |
| GET | `/api/admin/corrections?limit=50` | 7 | Audit log |

---

## 5. MQTT (internal broker)

All topics start with `parking/<lot>/`. All payloads are JSON with `"v": 1` unless noted.

| Topic | QoS | Retained | Publisher → subscriber |
|-------|-----|----------|------------------------|
| `camera/<cam>/observation` | 1 | no | occupancy worker → API |
| `camera/<cam>/flow` | 1 | no | flow worker → API |
| `camera/<cam>/health` | 0 | yes | worker → API |
| `camera/<cam>/cmd` | 1 | no | API → worker |
| `camera/<cam>/snapshot/<request_id>` | 0 | no | worker → API (**binary JPEG**) |
| `status` | 1 | yes | API → anyone (bridged to the home broker for Home Assistant) |

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
  "fps": 0.2, "last_frame_age_s": 3.1, "inference_ms_avg": 151, "unhealthy_ratio": 0.0
}
```
`state`: `ok | degraded | down`. `issue`: `null | black | frozen | blurry | shifted | connect_failed`.
The worker also sets an MQTT **Last Will** on this topic with `{"v":1,"camera_id":…,"state":"down","issue":"connect_failed"}`, so a crashed worker shows as down at once.

### cmd
```json
{ "v": 1, "cmd": "reload" }
{ "v": 1, "cmd": "snapshot", "request_id": "a1b2", "annotated": true }
{ "v": 1, "cmd": "save_reference" }
```

---

## 6. Push notification payload (Web Push, encrypted by pywebpush)

```json
{
  "title": "Parking: 23 free",
  "body": "Ground 12 · Underground ≈11 · 17:05",
  "tag": "parking-status",
  "url": "https://iulian-redinciuc.github.io/parking-app/#/",
  "level": "plenty",
  "kind": "on_my_way"
}
```
`tag` makes a new notification replace the previous one instead of stacking. `kind`: `test | on_my_way | schedule | almost_full | admin_alert`.

---

## 7. Cross-cutting

- **CORS:** allow only `CORS_ORIGINS`; methods GET/POST/PATCH/PUT/DELETE; headers `Content-Type, Authorization`.
- **Rate limits** (slowapi, per IP): public GET 120/min, push POST 20/hour, login 5/15 min.
- **Compression:** gzip for JSON responses > 1 KB (not for SSE).
- **OpenAPI docs** at `/docs`, enabled only when `LOG_LEVEL=DEBUG` or on the LAN.
