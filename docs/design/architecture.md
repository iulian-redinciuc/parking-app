# Architecture

## 0. Environments and isolation

- **Development / testing** happens on Iulian's **Raspberry Pi 5**. There the app is **completely self-contained**: it does **not** use, connect to, or change anything already installed on the Pi. Everything runs in its **own Docker Compose project** (`parking`), on its **own Docker networks**, with its **own data** under this repo folder ([deployment.md §1](deployment.md#1-development-on-the-raspberry-pi)).
- **Production** runs **somewhere else, not decided yet** ([deployment.md §3](deployment.md#3-production-topologies-to-be-chosen)). Nothing in the code may assume the Pi: CPU type, AI runtime, resource limits and public entry are all configuration.

## 1. Components

| Component | Runs where | Responsibility | Talks to |
|-----------|-----------|----------------|----------|
| **vision-occupancy** worker | Dev: Pi. Prod: vision host (at the lot, or cloud in T3) | Grabs frames from Camera B, detects vehicles, scores each space, sends results to the API | Camera (RTSP/HTTP) → API (HTTP, internal) |
| **vision-flow** worker | Dev: Pi. Prod: vision host | Reads Camera A's sub-stream, detects + tracks vehicles, sends in/out events to the API | Camera (RTSP) → API (HTTP, internal) |
| **API** | Dev: Pi. Prod: lot box (T1) or server/VM (T2/T3) | Receives worker results, smoothing, flow counting, fusion into zone counts, SQLite history, REST + SSE, Web Push, admin | workers, SQLite, phones (through tunnel), push services |
| **SQLite** | Same machine as the API, file in `data/db/` | History, subscriptions, corrections | API only |
| **Public entry** | Same machine as the API: its own `parking-tunnel` container, or a `parking-caddy` reverse proxy | Public HTTPS hostname for the API; forwards only `/api/*` and `/healthz` | API |
| **Frontend (PWA)** | Dev: GitHub Pages preview. Prod: decided in Phase 8 | Live screen, notifications settings, stats, admin | API over HTTPS |

## 2. Data flow

```mermaid
sequenceDiagram
  autonumber
  participant Cam as Camera B
  participant W as vision-occupancy
  participant API as API
  participant DB as SQLite
  participant P as Phone (PWA)

  P->>API: GET /api/status (on open)
  API-->>P: current LotStatus
  P->>API: GET /api/stream (SSE, stays open)
  loop every 5 s
    W->>Cam: grab frame
    W->>W: detect vehicles → score slots
    W->>API: POST /internal/observations (slot scores, taken flags)
  end
  API->>API: smoothing (3 consistent readings) → fusion
  alt zone count changed
    API->>DB: insert zone_state row
    API-->>P: SSE event: status
  end
```

Flow camera events follow the same path: `POST /internal/flow-events` → API `FlowCounter` → fusion → SSE.

## 3. Ground rules

1. **Workers never send images** to the API, except a single JPEG an admin asks for (`GET /control/snapshot` on the worker). Results are small JSON.
2. **The API is the only writer** to SQLite and the only component the internet can reach. Its `/internal/*` routes need the worker token **and** are blocked at the tunnel (see [deployment.md §5](deployment.md#5-public-access-for-the-api)).
3. **Configuration is file-based** (`config/lot.yaml` + `config/slots/*.json`), committed to git. **Secrets and the lot location come from `.env`** (git-ignored).
4. **Time is UTC** everywhere in storage and payloads (ISO 8601 with `Z`). Only the UI converts to local time.
5. **Pixel coordinates** in slot and line files are relative to the reference image size stored in that file. Workers rescale if the live frame size differs.
6. **Every payload has a version field `v`** so it can evolve.
7. **Workers are stateless** apart from short-term buffers. All counting state that must survive a restart (flow counts, smoothing results) lives in the API and is persisted.
8. **Fail visible, not silent.** If data is old or a camera is unhealthy, the UI says so (see `stale` and `confidence` in [api.md](api.md#lotstatus)).

## 4. Backend module map (`backend/parking/`)

```
backend/
├── pyproject.toml
├── alembic.ini
├── migrations/                  # Alembic migrations (from Phase 2)
├── parking/
│   ├── __init__.py              # __version__
│   ├── cli.py                   # Typer app: `parking ...` (all commands, see §6)
│   ├── config.py                # pydantic models for lot.yaml, slot/line files, Settings (.env)
│   ├── geometry.py              # polygon/line helpers on top of shapely + numpy
│   ├── messages.py              # pydantic payload models shared by workers and API
│   ├── vision/
│   │   ├── detector.py          # Detection dataclass, Detector protocol, YoloDetector
│   │   ├── occupancy.py         # slot scoring, zone counting
│   │   ├── flow.py              # TwoLineCounter (per-track crossing state machine)
│   │   ├── tracking.py          # wrapper around ByteTrack (via ultralytics)
│   │   ├── motion.py            # MotionGate (MOG2)
│   │   ├── sources.py           # FrameSource protocol + file/folder/snapshot/rtsp sources
│   │   ├── health.py            # black / frozen / blurry frame checks
│   │   ├── shift.py             # camera shift detection vs reference frame (ORB)
│   │   ├── annotate.py          # draw slots, detections, lines, totals on a frame
│   │   ├── bootstrap.py         # propose slots from detections
│   │   └── evaluate.py          # metrics for occupancy and flow
│   ├── workers/
│   │   ├── base.py              # loop, heartbeat, graceful shutdown
│   │   ├── api_client.py        # HTTP client to the API: retries, outbox for flow events
│   │   ├── control.py           # tiny internal HTTP server: /control/snapshot, /reload, /save-reference
│   │   ├── occupancy_worker.py
│   │   └── flow_worker.py
│   ├── core/
│   │   ├── smoothing.py         # SlotSmoother, CountSmoother
│   │   ├── flow_counter.py      # FlowCounter (clamp, corrections, idempotency)
│   │   ├── fusion.py            # StateStore → LotStatus (confidence, stale, trend, level)
│   │   └── clock.py             # injectable clock for tests
│   ├── db/
│   │   ├── engine.py            # SQLModel engine/session (WAL mode)
│   │   ├── models.py            # tables (see data-model.md)
│   │   └── repo.py              # read/write helpers, aggregation, pruning
│   ├── api/
│   │   ├── app.py               # create_app(): routers, CORS, rate limits, lifespan
│   │   ├── deps.py              # settings, db session, state store, auth dependencies
│   │   ├── sse.py               # Broadcaster (fan-out to SSE clients)
│   │   ├── ingest.py            # applies worker payloads to core/, records changes, broadcasts
│   │   └── routes/
│   │       ├── public.py        # /healthz, /api/lot, /api/status, /api/stream, /api/history
│   │       ├── internal.py      # /internal/* (worker token only)
│   │       ├── push.py          # /api/push/*
│   │       └── admin.py         # /api/admin/*
│   └── push/
│       ├── sender.py            # pywebpush wrapper, expiry cleanup
│       ├── rules.py             # when to notify (on-my-way, schedules, quiet hours)
│       └── scheduler.py         # APScheduler jobs
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/                # synthetic images and JSON only (public repo!)
```

## 5. Frontend module map (`frontend/src/`)

```
frontend/
├── index.html
├── vite.config.ts               # base: '/parking-app/', PWA plugin (injectManifest)
├── public/icons/                # PWA icons
└── src/
    ├── main.tsx                 # root, HashRouter, i18n init, SW registration
    ├── App.tsx                  # layout: header, bottom nav, routes
    ├── sw.ts                    # service worker: precache + push + notificationclick
    ├── api/
    │   ├── types.ts             # LotStatus, Zone, etc. (mirror of api.md)
    │   ├── client.ts            # fetch wrappers, base URL, admin token
    │   ├── live.ts              # SSE connection manager (reconnect, polling fallback)
    │   └── mock.ts              # fake live data for development without backend
    ├── hooks/
    │   ├── useLiveStatus.ts
    │   ├── useNow.ts            # ticking clock for "updated X s ago"
    │   └── useProximity.ts      # Tier 1 geolocation watcher
    ├── lib/
    │   ├── status.ts            # level thresholds, formatting
    │   ├── geo.ts               # haversine distance
    │   └── push.ts              # subscribe/unsubscribe helpers
    ├── screens/
    │   ├── LiveScreen.tsx
    │   ├── NotificationsScreen.tsx
    │   ├── StatsScreen.tsx      # Phase 7
    │   ├── PrivacyScreen.tsx    # Phase 8
    │   └── admin/               # Phase 7, lazy-loaded
    ├── components/              # BigCount, ZoneCard, StatusBanner, UpdatedAgo, InstallHint…
    ├── i18n/
    │   ├── index.ts
    │   └── locales/en.json
    └── styles/index.css         # Tailwind + design tokens
```

## 6. CLI commands (`parking …`)

| Command | Phase | Purpose |
|---------|-------|---------|
| `parking --version` | 0 | Smoke test |
| `parking models export --model yolo11n-seg --imgsz 640 --runtime ncnn` | 1 | Download + export weights to `models/` |
| `parking analyze --image PATH --camera ID` | 1 | Analyse one image → JSON + annotated PNG |
| `parking evaluate --images DIR --labels FILE --camera ID [--sweep 0.1:0.6:0.05]` | 1 | Accuracy metrics, threshold sweep |
| `parking bootstrap-slots --image PATH --camera ID --out FILE` | 1 | Suggest slot polygons |
| `parking benchmark --image PATH [--runs 20]` | 1 | Speed per model/format/size |
| `parking worker occupancy --camera ID` | 2 | Run the occupancy worker |
| `parking worker flow --camera ID` | 5 | Run the flow worker |
| `parking api` | 2 | Run the API (uvicorn) |
| `parking db upgrade` | 2 | Apply Alembic migrations |
| `parking record --camera ID --minutes 60` | 5 | Record a test clip |
| `parking evaluate-flow --video PATH --truth CSV --camera ID` | 5 | Flow accuracy on a clip |
| `parking push vapid-keys` | 6 | Generate VAPID keys |
| `parking admin hash-password` | 7 | Argon2 hash for `.env` |
| `parking db prune` / `parking db aggregate` | 7 | Retention + rollups (also scheduled) |
| `parking backup --out DIR` | 8 | Consistent SQLite + config backup |

## 7. Runtime view (Docker Compose project `parking`)

```mermaid
flowchart TB
  subgraph net_internal["network: parking-internal (private, no published ports)"]
    W1[vision-occupancy]
    W2["vision-flow (profile: flow)"]
    API[api]
  end
  W1 -- "POST /internal/*" --> API
  W2 -- "POST /internal/*" --> API
  API -- "GET /control/*" --> W1
  T["public entry (tunnel or Caddy container)"] -- "/api/*, /healthz only" --> API
```

Only these containers exist; nothing outside the `parking` project is used. On one machine, all of this runs together. In topology T2 the workers run on the lot box and reach the API over a private VPN ([deployment.md §9](deployment.md#9-workers-and-api-on-different-machines-t2)). Details in [deployment.md](deployment.md).
