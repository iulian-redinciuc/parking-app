# Deployment

Two kinds of environment:
- **Development / testing:** Iulian's **Raspberry Pi 5**. Used only to build and test. It also runs other software, which the app must never touch (§1).
- **Production:** **the cloud or another Raspberry Pi.** The exact layout is chosen before real cameras go in (P4.1) from §3, and deployed in Phase 8. Nothing in the code may assume the dev Pi.

## 1. Development on the Raspberry Pi

### 1.1 Isolation rules (dev Pi)
The app **does not use, connect to, or modify anything already installed on the Pi**:

| Rule | How |
|------|-----|
| Own Compose project | `name: parking`; every container is prefixed `parking-` |
| Own networks | `parking_internal` (private, `internal: true`) and `parking_egress` (outbound only) |
| Own data | Everything lives under `~/workspace/parking-app/` (`config/`, `data/`, `models/`). No volumes shared with other projects |
| No shared services | No existing brokers, databases, reverse proxies, home-automation software or host tunnels are used. Anything needed runs in its own `parking-*` container |
| Host ports | Only `127.0.0.1:8000` (API, loopback only). Change it with `API_HOST_PORT` if needed |
| Host Python | Untouched. `uv` installs Python 3.12 in the user's own uv folder, and containers carry their own Python |
| Removal | `cd deploy && docker compose down -v --rmi local`, then delete the folder |

### 1.2 How development runs

| What | How |
|------|-----|
| Backend | `uv` venv with Python 3.12 in `backend/`; API and workers run with `parking …`, or `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` (builds ARM64 images locally) |
| Frontend | `npm run dev` with `VITE_API_BASE=mock` or `http://<pi>:8000` |
| Camera input | Still images (`file:`), image folders (`folder:`), **recordings from the real cameras** (`video:`). A live camera only when one is reachable for testing |
| Phone testing | Frontend **preview** on GitHub Pages (§6). The dev API is reachable over HTTPS through the `parking-tunnel-quick` container (a Cloudflare quick tunnel, §5 Option Q: no account or domain) **only while testing**: `deploy/scripts/dev-public.sh up`, then `down` afterwards |
| Data | Test data only. `data/` on the Pi is disposable and not backed up |

## 2. Docker images (`backend/Dockerfile`, multi-stage)

```dockerfile
FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN pip install uv

FROM base AS api
RUN cd backend && uv sync --frozen --no-dev --no-install-project   # core deps only (no torch), cached layer
COPY backend/ ./backend/
RUN cd backend && uv sync --frozen --no-dev                         # installs the project itself
USER 1000:1000
CMD ["/app/backend/.venv/bin/parking", "api", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS vision
ARG VISION_EXTRAS=vision                               # e.g. "vision,openvino" for Intel hosts
RUN cd backend && uv sync --frozen --no-dev --no-install-project \
    $(echo ",${VISION_EXTRAS}" | sed 's/,/ --extra /g')               # one --extra per name
COPY backend/ ./backend/
RUN cd backend && uv sync --frozen --no-dev $(echo ",${VISION_EXTRAS}" | sed 's/,/ --extra /g')
ENV HOME=/tmp YOLO_CONFIG_DIR=/tmp/Ultralytics         # uid 1000 has no home in the image
USER 1000:1000
ENTRYPOINT ["/app/backend/.venv/bin/parking", "worker"]
```

- The build context is the repo root; `.dockerignore` lets in only `backend/` (minus `.venv`, caches and `tests/`) and, for the web image, `frontend/` (minus `node_modules`, `dist`, `.env*`), `tools/slot-editor/` and `deploy/Caddyfile`, so `data/`, `models/` and `deploy/.env` never reach an image.
- **web** image (`frontend/Dockerfile`, P8.3): a `node:22-alpine` stage builds the frontend with `VITE_BASE=/` and an empty `VITE_API_BASE` (same origin as the API), then `caddy:2.11-alpine` gets the built files in `/srv` and `deploy/Caddyfile` ([§5 Option B](#option-b-reverse-proxy-on-a-machine-with-a-public-ip-t2t3-server)). Nothing about a hostname is built in: `PUBLIC_HOST` is read when the container starts. Released as `ghcr.io/iulian-redinciuc/parking-web:<version>` from the first tag after `v0.1.0`; CI builds it on both CPU types when the frontend or the Caddyfile changes (job `web-image`).
- The working directory is `/app`, so the default `config/lot.yaml` and the relative paths in it resolve against the mounted `/app/config`, `/app/data` and `/app/models`. Containers run as uid 1000 (the owner of the repo folders on the dev Pi).
- The **api** image stays small (no PyTorch). Only **vision** carries the ML stack (expect ~2 GB).
- **Multi-arch:** CI builds every image for `linux/amd64` **and** `linux/arm64` with `docker buildx`, so the same version runs on the dev Pi and on any production machine. On pushes and PRs that touch `backend/`, CI only builds them (the `images` job in `ci.yml`: each CPU type on its own native runner, then `parking --version` in the image) to catch "works on ARM, breaks on x86" early. On a release tag (`v*`), `release.yml` builds both platforms in one go (QEMU) and pushes them to **GitHub Container Registry**: `ghcr.io/iulian-redinciuc/parking-api:<version>` and `parking-vision:<version>` (`<version>` is the tag, e.g. `v0.1.0`; `:latest` moves with every full release). Details in [§8](#8-releases-and-updating).
- **NVIDIA hosts** (if chosen) need a separate CUDA-based `vision` variant (`parking-vision:<version>-cuda`), built only if that hardware is picked. Not built today: the chosen vision host is a Raspberry Pi 5 (P4.1).
- Model weights are **not** baked in: they're mounted from `./models`, created on each machine with `parking models export --runtime <runtime>` (the export is tuned to that machine's runtime; see [vision.md §11](vision.md#11-runtimes-and-performance)).
- `opencv-python-headless` (not `opencv-python`), since there's no GUI in containers.

## 3. Production topologies (to be chosen)

Pick one before Phase 4 hardware goes in (open question #2). The code supports all three; only configuration differs.

**Chosen in P4.1 (2026-10-09): T2.** The lot hardware is listed in [hardware.md §4.5](hardware.md#45-chosen-in-p41-to-order); the cloud VM is picked and provisioned in P8.2.

| | **T1: one Raspberry Pi at the lot** | **T2: Pi at the lot + cloud** (recommended) | **T3: all in the cloud** |
|---|---|---|---|
| Vision workers | Raspberry Pi at the lot | Raspberry Pi at the lot | Cloud VM |
| API + database | Same Raspberry Pi | Cloud VM | Same cloud VM |
| Does video leave the lot? | No | No | **Yes** (camera streams over a VPN) |
| Upload bandwidth from the lot | Tiny (only the public app traffic) | Tiny (results are a few KB/minute) | **~1–4 Mbit/s per camera, continuously** |
| Public HTTPS entry | Tunnel from the lot box (works behind 4G/CGNAT) | VM public IP + reverse proxy, or a tunnel | Same as T2 |
| If the lot's internet drops | The app is unreachable | Phones see "stale"; counting continues on site and flow events are delivered when the link returns (outbox) | No data at all |
| Machines to run | 1 | 2 | 1 (with enough CPU or a GPU) |
| Typical extra cost | Lowest | + a small VM | A bigger VM (inference in the cloud) |

**Why T2 is recommended:** video stays on site (privacy, bandwidth), while the public part (API, push, database, backups) runs on reliable infrastructure with a fixed address. Choose T1 if one machine and minimum cost matter more. T3 only if nothing can be installed at the lot.

On-site vision box options are in [hardware.md §4](hardware.md#4-compute-hardware).

## 4. Compose (`deploy/`)

Files:
| File | Used for |
|------|----------|
| `docker-compose.yml` | Base definition (all services, using released images from GHCR) |
| `docker-compose.dev.yml` | Dev Pi: builds images locally, mounts source for quick iteration |
| `docker-compose.site.yml` | T2 lot box: vision services and `autoheal` only, `API_INTERNAL_URL` = the API's VPN address |
| `docker-compose.server.yml` | T2 server: API + public entry only (`--profile web`: `parking-web`, Caddy + the frontend; or `--profile public`: the Cloudflare tunnel) |

Base file (abridged):
```yaml
name: parking

x-hardening: &hardening               # §4.1
  read_only: true
  tmpfs: ["/tmp:size=64m"]
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  user: "1000:1000"

x-common: &common
  <<: *hardening
  env_file: .env
  restart: unless-stopped
  volumes:
    - ../config:/app/config
    - ../data:/app/data
    - ../models:/app/models:ro
  logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }

services:
  api:
    <<: *common
    container_name: parking-api
    image: ghcr.io/iulian-redinciuc/parking-api:${PARKING_VERSION:-latest}
    ports: ["127.0.0.1:${API_HOST_PORT:-8000}:8000"]          # loopback only
    networks: [internal, egress]                               # egress: Web Push
    deploy: { resources: { limits: { cpus: "${API_CPUS:-1.0}", memory: "${API_MEMORY:-512M}" } } }
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
      start_interval: 2s

  vision-occupancy:
    <<: *common
    container_name: parking-vision-occupancy
    image: ghcr.io/iulian-redinciuc/parking-vision:${PARKING_VERSION:-latest}
    command: ["occupancy", "--camera", "cam-ground"]
    networks: [internal, egress]                               # egress: reach the camera
    deploy: { resources: { limits: { cpus: "${VISION_CPUS:-1.0}", memory: "${VISION_MEMORY:-1200M}" } } }
    environment: { OMP_NUM_THREADS: "2", API_INTERNAL_URL: "${API_INTERNAL_URL:-http://api:8000}" }
    depends_on: { api: { condition: service_healthy } }
    healthcheck:                                               # §4.2: the loop's heartbeat file is fresh
      test: ["CMD", "/app/backend/.venv/bin/python", "-m", "parking.workers.heartbeat"]
      interval: 10s
      timeout: 5s
      retries: 2
      start_period: 60s
      start_interval: 2s
    labels: { parking.autoheal: "true" }                       # restarted when unhealthy

  vision-flow:
    <<: *common
    container_name: parking-vision-flow
    image: ghcr.io/iulian-redinciuc/parking-vision:${PARKING_VERSION:-latest}
    command: ["flow", "--camera", "cam-ramp"]
    profiles: ["flow"]                                         # enabled from Phase 5
    networks: [internal, egress]
    deploy: { resources: { limits: { cpus: "${FLOW_CPUS:-1.5}", memory: "${FLOW_MEMORY:-1200M}" } } }
    environment: { OMP_NUM_THREADS: "2", API_INTERNAL_URL: "${API_INTERNAL_URL:-http://api:8000}" }
    depends_on: { api: { condition: service_healthy } }
    healthcheck: …                                             # same as vision-occupancy
    labels: { parking.autoheal: "true" }

  autoheal:                           # §4.2: restarts unhealthy containers labelled parking.autoheal=true
    <<: *hardening
    container_name: parking-autoheal
    image: willfarrell/autoheal:1.2.0
    group_add: ["${DOCKER_GID:?…}"]   # the Docker socket's group on this machine
    environment: { AUTOHEAL_CONTAINER_LABEL: parking.autoheal, AUTOHEAL_INTERVAL: "5", AUTOHEAL_DEFAULT_STOP_TIMEOUT: "10" }
    volumes: ["/var/run/docker.sock:/var/run/docker.sock"]
    network_mode: none
    restart: unless-stopped

  tunnel:
    <<: *hardening-tunnel             # §4.1: read-only, no capabilities, uid 65532, limits, `tunnel ready` healthcheck
    container_name: parking-tunnel
    image: cloudflare/cloudflared:2026.10.0
    command: tunnel --no-autoupdate --metrics localhost:20241 run
    environment: { TUNNEL_TOKEN: "${TUNNEL_TOKEN:-}" }
    restart: unless-stopped
    networks: [internal, egress]
    profiles: ["public"]
    depends_on:
      api: { condition: service_healthy }

  tunnel-quick:                       # dev only (§5 Option Q)
    <<: *hardening-tunnel
    container_name: parking-tunnel-quick
    image: cloudflare/cloudflared:2026.10.0
    command: tunnel --no-autoupdate --metrics localhost:20241 --config /etc/cloudflared/config.yml --url http://api:8000
    volumes:
      - ./cloudflared-quick.yml:/etc/cloudflared/config.yml:ro
    restart: unless-stopped
    networks: [internal, egress]
    profiles: ["quick"]
    depends_on:
      api: { condition: service_healthy }

networks:
  internal: { internal: true }
  egress: {}
```

Dev override (`docker-compose.dev.yml`): each service drops the GHCR `image` (`image: !reset null`, Compose ≥ 2.24) and gets `build: { context: .., dockerfile: backend/Dockerfile, target: api | vision }`, so the local images are named `parking-api` / `parking-vision-occupancy` and `down --rmi local` removes them. `../backend/parking` is mounted read-only over the installed code (the project is installed editable), so a code change only needs `docker compose … restart`.

Notes:
- On one machine, workers reach the API at `http://api:8000/internal/*`, and the API reaches workers at `http://vision-occupancy:9000/control/*`. Both use `WORKER_TOKEN`.
- Across machines (T2), see §9. `docker-compose.server.yml` and `docker-compose.site.yml` are **standalone** files (not overrides of the base file): the server has `api` + `tunnel`, the lot box has the two workers without the `depends_on: api`. Both refuse to start without a pinned `PARKING_VERSION` and `VPN_BIND_IP`.
- Workers need a route to the camera IPs. If Docker bridge routing can't reach the camera network, set `network_mode: host` on the vision services only.
- CPU and memory limits come from `.env`, because they differ per machine (§4.1).

### 4.1 Hardening (P8.4)

Every service in every Compose file (base, server, site; the e2e test stack too, so CI notices a write outside the mounts) has:

| Setting | Value | Notes |
|---------|-------|-------|
| `restart` | `unless-stopped` | Comes back after a crash, a Docker restart and a reboot; stays down only after `docker compose stop` / `down` |
| `healthcheck` | API: `/healthz`; workers: the loop's heartbeat file (§4.2); `parking-autoheal`: the Docker socket answers `/_ping`; `parking-web`: port 443; tunnels: `cloudflared tunnel --metrics localhost:20241 ready` (connected to Cloudflare's edge) | `depends_on: service_healthy` uses them when the stack is started with `up` |
| `read_only: true` | root filesystem read-only | The only writable places are the mounts (`config/`, `data/`, for Caddy its two volumes) and `/tmp` |
| `tmpfs` | `/tmp:size=64m` | In memory: counts towards the service's memory limit, empty after a restart. Holds the Ultralytics settings (`HOME=/tmp`) and SQLite's temporary files. The tunnels write nothing and have none |
| `cap_drop` | `[ALL]`, nothing added back | No service needs a capability |
| `security_opt` | `no-new-privileges:true` | setuid binaries and file capabilities can't raise privileges |
| `user` | `1000:1000` (`parking-*` images, the owner of `config/`, `data/`, `models/`); `65532:65532` (cloudflared's own `nonroot`) | Never root |
| `deploy.resources.limits` | from `.env`, see below | |
| `logging` | `json-file`, 3 × 10 MB | The disk can't fill with logs |

**Caddy without root or capabilities** (`parking-web`): the image runs as uid 1000, owns `/data` and `/config` (a new named volume copies that owner) and has the file capability removed from the `caddy` binary (with `cap_drop: [ALL]` it could not be granted). Ports 80/443 are opened through the container's own `net.ipv4.ip_unprivileged_port_start=0` sysctl (Docker's default since 20.10, set explicitly in `docker-compose.server.yml`), which applies only inside that container's network namespace.

**Resource limits** (`.env`, per machine; defaults in `.env.example`):

| Variable | Default | Container | Basis |
|----------|---------|-----------|-------|
| `API_CPUS` / `API_MEMORY` | `1.0` / `512M` | `parking-api` | dev Pi: ~115 MB, < 1% CPU with one camera and no clients; re-check under P8.10's 500 SSE clients |
| `VISION_CPUS` / `VISION_MEMORY` | `1.0` / `1200M` | `parking-vision-occupancy` | dev Pi: ~130 MB with the appearance scorer; a YOLO model in memory needs several hundred MB |
| `FLOW_CPUS` / `FLOW_MEMORY` | `1.5` / `1200M` | `parking-vision-flow` | P5.10's dev-Pi reference; raise `FLOW_CPUS` to 2 first if fps is short |
| `WEB_CPUS` / `WEB_MEMORY` | `1.0` / `256M` | `parking-web` | dev Pi: ~15 MB idle |
| (fixed) | `0.5` / `128M` | tunnels | dev Pi: ~20 MB |
| (fixed) | `0.2` / `64M` | `parking-autoheal` | a shell loop with `curl` + `jq` every 5 s |

These are the dev-Pi values; they are tuned on the production machines after P8.2's measurements and P8.10's load test, in each machine's `.env` only.

**Pinned images:** production never runs `latest`. `docker-compose.server.yml` / `docker-compose.site.yml` refuse to start without `PARKING_VERSION`, and `prod-env.sh` accepts only a release tag (`v0.x.y`). Third-party images carry an exact version in the Compose files (`cloudflare/cloudflared:2026.10.0`, `willfarrell/autoheal:1.2.0`) and in the Dockerfiles (`caddy:2.11-alpine`, `node:22-alpine`, `python:3.12-slim-bookworm`). Dependabot proposes updates weekly: `docker-compose` in `/deploy`, `docker` in `/backend` and `/frontend`, next to `pip`, `npm` and `github-actions`. The base file keeps `${PARKING_VERSION:-latest}` only as the default for a machine without an `.env`; the dev Pi builds its own images.

**Reboot:** `provision.sh` enables the Docker service (and, on T2, orders it after `wg-quick@wg0`, so the VPN address the ports are published on exists first). At boot Docker starts every container that was running, in no particular order and without `depends_on`: the workers retry until the API answers, Caddy answers 502 for `/api/*` until then. Target: live data again within 3 minutes of power-on with no manual step. `deploy/scripts/boot-check.sh server|site [zone …]`, run right after logging in again, waits until every container of the project is running and healthy and (server) `/api/status` has no stale zone, and prints the seconds since boot (fails above `BOOT_LIMIT_S`, default 180). It only reads; it never starts a container.

Measured on the dev Pi (P8.4, not a reboot: the API and the worker processes ended at the same moment and Docker's restart policy brought them back): see PROGRESS.md → Metrics.

### 4.2 Watchdog for stuck workers (P8.5)

A worker whose loop hangs (a camera read or a model call that never returns) keeps its process, its `/control/*` port and even its health messages (they come from their own thread), so neither Docker's restart policy nor the old port check notices. Three parts close that gap:

1. **Heartbeat file** (`parking/workers/heartbeat.py`). Every time the loop comes round (occupancy: after each frame, healthy or not; flow: every pass, at most one write a second) the worker rewrites `HEARTBEAT_FILE` with the longest silence allowed, `max(3 × interval, 20 s)`, where `interval` is the occupancy worker's own period (the `folder:` source's `interval` or `sample_every_s`) and 0 for the flow worker. The file's modification time is the beat. `HEARTBEAT_FILE=/tmp/heartbeat` is set by the `parking-vision` image (the e2e stack sets it itself, because it runs the worker from the API image); without it, e.g. `parking worker … --print` on a laptop, nothing is written. It says the loop is alive, not that the camera works: a camera that is down is reported through the health messages and a restart wouldn't fix it.
2. **Healthcheck**: `python -m parking.workers.heartbeat` exits 1 when the file is missing or older than the limit written in it. With `interval: 10s`, `retries: 2` a stuck loop is `unhealthy` 25–40 s after it stopped (at `sample_every_s` ≤ 6; later for slower cameras, by the 3 × rule).
3. **`parking-autoheal`** (`willfarrell/autoheal`, in the base and the site file; the T2 server has no workers). Docker itself never restarts an unhealthy container. Every 5 s autoheal asks the Docker socket for unhealthy containers labelled **`parking.autoheal=true`** and restarts them (10 s for a clean stop, then killed). Only the two workers carry the label: the label is namespaced so that nothing else on a shared machine (the dev Pi) can ever match, and the API and the public entry are left out on purpose (their healthchecks fail for reasons a restart doesn't fix, like load or the internet being away; the external uptime check of P8.7 covers them).

The Docker socket is root on the host for whoever can talk to it, so this is the only container that gets it, and it gets nothing else: the §4.1 hardening (read-only, no capabilities, uid 1000 plus the socket's group `DOCKER_GID`), **no network** (`network_mode: none`), an exact image version. `DOCKER_GID` is per machine (`getent group docker | cut -d: -f3`); both Compose files refuse to start without it, and `prod-env.sh site` fills it in.

**Restart count:** an autoheal restart doesn't raise Docker's own `RestartCount`. Every health message carries the worker process's `started_at`; the API counts a restart whenever a camera's value changes and shows it in `/healthz` as `restarts` ([api.md §2](api.md#2-public-rest-endpoints)), with a warning in its log.

**Testing it:** `docker compose kill -s USR1 vision-occupancy` (or `vision-flow`). `SIGUSR1` makes the worker's main thread sleep forever inside the signal handler, so the loop stops wherever it was while the other threads go on, which is what a real hang looks like. `docker compose pause` doesn't count: a paused container can't be health-checked. Measured on the dev Pi: PROGRESS.md → Metrics.

## 5. Public access for the API

Only `/api/*` and `/healthz` may be reachable from the internet. `/internal/*` and the workers' `/control/*` must never be.

### Option A: Cloudflare Tunnel (its own `parking-tunnel` container)
Works on any machine, needs no inbound ports, and works behind 4G/CGNAT (good for T1). Requires a domain on Cloudflare.
1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → **Create tunnel** → copy the **token** to `TUNNEL_TOKEN`.
2. Public hostnames (order matters):
   - `<api-host>`, path `^/(api/|healthz$)` → `http://api:8000`
   - `<api-host>` (no path) → **HTTP 404**
3. `docker compose --profile public up -d`.
4. Cache Rule: bypass the cache for `<api-host>/*` (SSE and live data must never be cached).

Use **separate hostnames** for dev testing (e.g. `parking-api-dev.<domain>`) and production.

### Option Q: Cloudflare quick tunnel (dev phone testing only, `parking-tunnel-quick`)
No account, no domain, no token: cloudflared asks Cloudflare for a random `https://<words>.trycloudflare.com` address. Used on the dev Pi until there is a domain (P3.9); **never for production** (no uptime guarantee, and the address changes every time the tunnel starts).
- Service `tunnel-quick` (profile `quick`, image pinned). The path rule of Option A is kept with an ingress file, `deploy/cloudflared-quick.yml` (`^/(api/|healthz$)` → `http://api:8000`, everything else → 404), mounted as the container's config; `--url` is still needed to ask for a quick tunnel. Checked through the tunnel: `/healthz` and `/api/status` 200, `/internal/*` and `/docs` 404.
- `deploy/scripts/dev-public.sh up | down | url`. `up`: dev stack + tunnel (`docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile quick up -d`), reads the address from the container's log (the last one since its current start), waits for `<url>/healthz`, sets the repository variable `API_BASE` to it and runs the Pages workflow (§6), waiting for the run unless `DEV_PUBLIC_WAIT=0`. `down`: removes only the tunnel container, `API_BASE=mock`, Pages again. `url`: prints the current address. Needs `gh` signed in. Run `up` again after a reboot or a tunnel restart: the container comes back by itself but with a **new address**, and the preview keeps calling the old one until it is rebuilt.
- **Quick tunnels don't carry SSE**: `/api/stream` answers `200 text/event-stream` but no event ever arrives (0 bytes in 150 s, measured in P3.9). The app notices (frontend.md §3: a stream counts as live only after its first event) and polls `/api/status` every 10 s, so the numbers on the phone are up to ~10 s behind and the header says *Updating*. A named tunnel (Option A) or Caddy (Option B) streams normally.
- The address isn't a secret (it is built into the public preview's JavaScript); what protects the API is the same as in production: the path rule, the tokens and the rate limits.

### Option B: reverse proxy on a machine with a public IP (T2/T3 server)
**Chosen for production in P8.3** (the T2 server is a cloud VM with a public IPv4 address, so no Cloudflare account or tunnel is needed). A `parking-web` container (image `parking-web`, [§2](#2-docker-images-backenddockerfile-multi-stage); Caddy obtains and renews HTTPS certificates automatically) is the one public listener, and it also serves the frontend ([§6](#6-frontend-hosting)). `deploy/Caddyfile`, abridged:
```
{$PUBLIC_HOST:localhost} {
  @api path /api/* /healthz
  handle @api { reverse_proxy api:8000 { flush_interval -1 } }   # -1: stream SSE immediately
  handle { root * /srv; file_server }                             # the frontend; unknown paths: 404
}
```
- Only `/api/*` and `/healthz` have a route to the API. `/internal/*`, `/control/*`, `/docs` and everything else end in the file server and get a 404.
- **`PUBLIC_HOST`** (`.env`) is the public hostname. Either a domain or subdomain whose DNS **A record** points at the server, or, **without a domain**, `<server IPv4 with dashes>.sslip.io` (e.g. `203-0-113-7.sslip.io`; sslip.io is a free public DNS service that answers with the address in the name, and Let's Encrypt issues certificates for such names). Moving to a real domain later = change `PUBLIC_HOST`, `CORS_ORIGINS`, `PUBLIC_APP_URL` and restart; push subscriptions and home-screen installs belong to the origin, so users install and enable notifications again once.
- `docker compose -f docker-compose.server.yml --profile web up -d`. Ports 80, 443/tcp and 443/udp (HTTP/3) are published; open them with `provision.sh server --public-proxy` ([§10](#10-provisioning-the-production-machines-t2)). Certificates live in the `parking_caddy-data` volume: keep it across updates (Let's Encrypt limits how often a name can get a new certificate).
- Headers: HSTS, `X-Content-Type-Options: nosniff`; `Cache-Control: no-cache` for `/`, `index.html`, `sw.js`, `registerSW.js` and the manifest (a new release reaches installed apps on the next load), one year `immutable` for the hashed `/assets/*`.
- **Client IP:** the API's rate limits are per client IP, so uvicorn must take it from the proxy's `X-Forwarded-For`. `docker-compose.server.yml` gives the `internal` network a fixed subnet (`INTERNAL_SUBNET`, default `172.31.77.0/24`) and sets uvicorn's `FORWARDED_ALLOW_IPS` to it for the API: the header is trusted only from containers on that network (Caddy or cloudflared), not from the VPN or loopback.
- Checked on the dev Pi (P8.3, the image on a loopback port in front of the dev API, `PUBLIC_HOST=localhost` = Caddy's own test certificate): `/`, `/sw.js`, the manifest, `/healthz`, `/api/status` 200; `/internal/*`, `/control/*`, `/docs`, `/openapi.json` 404; `/api/stream` delivers its first event at once. The real certificate and a phone over mobile data need the production server.

## 6. Frontend hosting

| Stage | Where | Build settings |
|-------|-------|----------------|
| **Preview** (development, now) | GitHub Pages: `https://iulian-redinciuc.github.io/parking-app/` | `VITE_BASE=/parking-app/`, `VITE_API_BASE` = dev API URL or `mock` |
| **Production** (chosen in P8.3) | **The production reverse proxy**: `parking-web` serves the built files at `https://<PUBLIC_HOST>/`, the same origin as the API (no CORS preflights, one certificate, the frontend and the API are always the same release). The other options stay possible: GitHub Pages with a custom domain, or any static host (Cloudflare Pages, Netlify) | `VITE_BASE=/`, `VITE_API_BASE=` (empty = same origin), set in `frontend/Dockerfile`; another host builds with its own values |

The GitHub Pages preview is unchanged by production: it keeps building from `main` with the repository variable `API_BASE` (the dev API or `mock`). `CORS_ORIGINS` on the production server is only `https://<PUBLIC_HOST>` (the preview is not allowed to call the production API).

The build is host-agnostic: `HashRouter` needs no server rewrite rules, and the base path and API URL are build variables.

Preview workflow `.github/workflows/pages.yml`:
```yaml
name: Deploy preview frontend
on:
  push: { branches: [main], paths: ["frontend/**", ".github/workflows/pages.yml"] }
  workflow_dispatch:
permissions: { contents: read, pages: write, id-token: write }
concurrency: { group: pages, cancel-in-progress: true }
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: npm, cache-dependency-path: frontend/package-lock.json }
      - run: npm ci
        working-directory: frontend
      - run: npm run build
        working-directory: frontend
        env: { VITE_BASE: "/parking-app/", VITE_API_BASE: "${{ vars.API_BASE || 'mock' }}" }
      - uses: actions/upload-pages-artifact@v3
        with: { path: frontend/dist }
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment: { name: github-pages, url: "${{ steps.d.outputs.page_url }}" }
    steps:
      - id: d
        uses: actions/deploy-pages@v4
```
One-time switch from "deploy from branch" to Actions: `gh api -X PUT repos/iulian-redinciuc/parking-app/pages -f build_type=workflow`.
Set the preview API URL: `gh variable set API_BASE --body "https://parking-api-dev.<domain>"` (or `mock`), then run the workflow (`gh workflow run pages.yml`); with the quick tunnel, `deploy/scripts/dev-public.sh` does both (§5 Option Q). An unset variable builds in mock mode (an empty `VITE_API_BASE` would otherwise mean a same-origin API, which Pages doesn't have). As built (P3.8): Pages uses `build_type=workflow`, `API_BASE=mock` until P3.9, the old root placeholder `index.html` is gone.

## 7. Backups

**Production only** (the dev Pi holds test data), on the **API machine** (the server in T2): it has the database, the `config/` the admin editor writes, and the `.env` with the VAPID keys. The lot box holds nothing that can't be recreated (a copy of `lot.yaml`, exported models, `prod-env.sh site` + the camera URLs). How to restore: [runbook.md](../runbook.md#restore-from-backup).

**The archive** (`parking backup --out DIR`, `parking/db/backup.py`): `parking-YYYYMMDD-HHMM.tar.gz`, time in **UTC**, written atomically (a second run in the same minute replaces the first).

| Entry | Content |
|-------|---------|
| `manifest.json` | `format` (1), `created`, `app_version`, `db_revision` (Alembic), `files`: `{path: {size, sha256}}` |
| `db/parking.sqlite` | The database, copied with SQLite's online backup API (safe while the API runs) and checked with `PRAGMA integrity_check` before it's packed |
| `config/…` | `lot.yaml`, slot and line files (not the editor's `*.bak`) |
| `reference/…` | `data/reference/` (the slot reference images), when present. Real camera images: one more reason the remote is encrypted |

Not in the archive: `.env` (the containers never see the file; it goes to the remote separately), `models/` (reproducible), debug captures, recordings, validation sets and labels.

**Rotation** (same command, after writing; `--keep-daily 14 --keep-weekly 8`, `--no-rotate`): the newest archive of each of the last 14 days that have one stays, plus the newest of each of the last 8 ISO weeks. Counted in days/weeks *with* a backup, so a stopped cron never rotates the last copies away. Other files in the folder are never touched.

**Nightly job** (`deploy/backup.sh`, host cron `/etc/cron.d/parking-backup` at **03:30** on the machine's clock, installed by `provision.sh server` with `cron` and `rclone`; output in `journalctl -t parking-backup`):
1. `docker exec parking-api /app/backend/.venv/bin/parking backup --out /app/data/backups` → `data/backups/` on the host.
2. Off-machine copy with `rclone` to the remote **`parking-backup`** in `deploy/rclone.conf` (git-ignored, mode 600): archives to `backups/`, the `.env` to `env/<hostname>.env`; a `.env` that changed leaves its previous version under `env-old/<time>/`.
3. The script **refuses to upload unless the remote's type is `crypt`** (file names and contents encrypted before they leave the machine).
4. Archives older than 60 days are deleted on the remote (`BACKUP_REMOTE_DAYS`). Uploads are `rclone copy`, never `sync`: a wiped local folder can't wipe the remote.

Exit codes: `0` done, `1` failed, `3` the local archive exists but was **not** copied off the machine (no rclone, no `rclone.conf`, no such remote, or not `crypt`). The "backup failed" alert is P8.7.

**The remote** is two entries in `rclone.conf`: the storage itself (any rclone backend: S3-compatible object storage, SFTP to another machine, …) and `parking-backup`, a `crypt` remote on top of it. Commands: [phase guide P8.6](../phases/phase-8-hardening.md#p86-backups-and-restore). **Keep a copy of `rclone.conf` outside the machine** (password manager): without its two crypt passwords the backups can't be read.

**Other commands:** `backup.sh list` (archives here and on the remote, backed-up `.env` names), `backup.sh env [NAME]` (fetch a `.env`; never overwrites one), `backup.sh restore [NAME]` (default: the newest; fetches it if it isn't in `data/backups/`, refuses while `parking-api` runs, then runs `parking restore` in a one-off container of the API image without network).

**`parking restore ARCHIVE`** checks the archive first (only the expected paths, every checksum, database integrity) and changes nothing if a check fails. Then nothing is lost: an existing database is renamed to `parking.sqlite.before-restore-<time>` (its `-wal`/`-shm` are removed so they can't be replayed into the restored file), a config file that differs is kept as `<file>.bak`. At the next start the API migrates the database if the backup is from an older release, and shows the restored numbers as stale until the workers deliver ([data-model.md §2](data-model.md#2-in-memory-state-api-process)).

Variables for `backup.sh` (environment, all optional): `RCLONE`, `BACKUP_RCLONE_CONFIG`, `BACKUP_REMOTE`, `BACKUP_REMOTE_DAYS`, `BACKUP_ENV_NAME`, `BACKUP_ENV_FILE`, `BACKUP_ROOT` (the folder holding `config/` and `data/`), `BACKUP_API_CONTAINER`, `BACKUP_IMAGE`.

**Restore drill** (P8.6): done on the dev Pi from a backup of the dev stack through a `crypt` remote (results in [PROGRESS.md → Metrics](../../PROGRESS.md#metrics)); repeated with a production backup once production exists.

## 8. Releases and updating

- **Release:** set `version` in `backend/pyproject.toml` (and `uv lock`) if it changed, then `git tag v0.x.y && git push origin v0.x.y` → `.github/workflows/release.yml`:
  1. **images**: checks that the tag matches the backend version (`v0.1.0` or `v0.1.0-rc1` ↔ `0.1.0`), then builds `api`, `vision` and (after `v0.1.0`) `web` for `linux/amd64,linux/arm64` (QEMU + buildx) and pushes `ghcr.io/iulian-redinciuc/parking-api:<tag>`, `parking-vision:<tag>` and `parking-web:<tag>`. `:latest` is moved too, except for pre-release tags (a `-` in the tag).
  2. **verify**: on an x86 runner and on an ARM runner, `docker pull` both images, check the architecture, run `parking --version`, and import the ML stack in the vision image; for `web`, `caddy validate` and the frontend's `index.html`.
  3. **release**: publishes the GitHub release with notes from the commit messages since the previous `v*` tag, grouped by the phase in the task ID (`deploy/scripts/release-notes.sh <tag>`; the first release lists the whole history). Pre-release tags are marked as pre-releases.
  *Run workflow* on the Actions page (`workflow_dispatch`) is a dry run: it builds both platforms and pushes nothing.
- **Pulling:** both packages are **public** (linked to this public repo), so `docker pull` / `docker compose pull` need no login on any machine. `docker buildx imagetools inspect ghcr.io/iulian-redinciuc/parking-api:<tag>` lists the platforms (the extra `unknown/unknown` entries are the build attestations).
- A failed release is fixed with a new tag (`v0.x.y+1`); tags are never moved.
- **Deploy to production:**
  ```bash
  cd /opt/parking/deploy                       # a checkout of the repo's deploy/ + config/ on that machine
  PARKING_VERSION=v0.x.y docker compose pull
  PARKING_VERSION=v0.x.y docker compose --profile public --profile flow up -d
  # T2: -f docker-compose.server.yml --profile web  on the server, -f docker-compose.site.yml --profile flow  on the lot box
  docker compose logs -f --tail=100 api
  ```
- Migrations run automatically at API start-up. **Roll back** by deploying the previous version (and restore the DB if a migration wasn't backwards compatible).
- The dev Pi uses `docker-compose.dev.yml` (local builds) and never needs releases.

## 9. Workers and API on different machines (T2)

- Connect the lot box and the API server with a **private VPN** (WireGuard or Tailscale, set up just for this app). Workers use `API_INTERNAL_URL=http://<api-vpn-address>:8000`, and the API reaches each worker's `/control/*` at its VPN address (config `cameras[].control_url`).
- `/internal/*` therefore never crosses the public internet, and the public entry (§5) still forwards only `/api/*` and `/healthz`.
- Workers keep their flow-event **outbox**, so a flaky lot connection loses nothing.
- Video never leaves the site. Bandwidth is a few KB per minute.

Chosen in P8.2: **WireGuard**, one tunnel just for this app (`wg0`, UDP 51820 on the server). The lot box dials out with a 25 s keepalive, so it works behind NAT/CGNAT (a 4G router) and needs no open port at the lot.

| | Server (cloud VM) | Lot box (vision host) |
|---|---|---|
| VPN address (`VPN_BIND_IP`) | `10.77.0.1` | `10.77.0.2` |
| Compose file | `docker-compose.server.yml` | `docker-compose.site.yml` |
| Published on the VPN address | API `:8000` (also on `127.0.0.1` for the public entry) | occupancy worker `/control/*` `:9000`, flow worker `:9001` |
| `.env` | `VPN_BIND_IP=10.77.0.1` | `VPN_BIND_IP=10.77.0.2`, `API_INTERNAL_URL=http://10.77.0.1:8000`, the server's `WORKER_TOKEN` |
| `lot.yaml` | `control_url: http://10.77.0.2:9000` (`cam-ground`), `http://10.77.0.2:9001` (`cam-ramp`) | same file |

Ports published by Docker bypass `ufw`, so the protection is the **bind address**: nothing is published on a public or LAN interface. Docker is ordered after `wg-quick@wg0` (a systemd drop-in) so the VPN address exists when the containers start at boot.

## 10. Provisioning the production machines (T2)

Two scripts in `deploy/scripts/`, run on the production machine itself (never on the dev Pi). The step-by-step is in the [phase guide, P8.2](../phases/phase-8-hardening.md#p82-provision-the-production-machines).

- **`provision.sh server|site`** (with `sudo`; Debian 12+, 64-bit Raspberry Pi OS, Ubuntu 24.04+; safe to re-run; `DRY_RUN=1` only prints):
  - `unattended-upgrades` on (security updates daily, Raspberry Pi archive included, reboot at 04:00 when a kernel update needs it);
  - Docker Engine + the compose plugin from Docker's apt repository, started on boot;
  - SSH with keys only (`/etc/ssh/sshd_config.d/00-parking.conf`; it stops before changing anything if the login user has no authorized key);
  - `ufw`: deny inbound; allow SSH; server: also UDP 51820 (VPN) and, with `--public-proxy`, 80/443 for a reverse proxy (§5 Option B; a tunnel needs none); lot box: nothing else;
  - WireGuard: a key pair per machine (`/etc/wireguard/parking.key`, never leaves it); the first run prints the public key, the second run with `--peer-key <other machine's key>` (lot box: also `--endpoint <server address>`) writes `wg0.conf` and enables it;
  - `/opt/parking/{deploy,config,models,data}` (`config`, `data`, `models` owned by uid 1000, the containers' user); `--version v0.x.y` copies that release's `deploy/` and `config/` there without overwriting existing config files.
  - server only: `cron` and `rclone`, and `/etc/cron.d/parking-backup` (the nightly backup at 03:30, [§7](#7-backups)).
- **`boot-check.sh server|site`**: after a reboot, the time until the stack is back by itself ([§4.1](#41-hardening-p84)).
- **`prod-env.sh server|site`** (`PARKING_VERSION=v0.x.y` required): writes a new `deploy/.env` (mode 600) from `.env.example`, never overwrites one, never prints a secret. `server` generates a fresh `WORKER_TOKEN`, `ADMIN_TOKEN` and VAPID key pair (from the released API image) and, given `PUBLIC_HOST=<hostname>`, sets `PUBLIC_HOST`, `CORS_ORIGINS=https://<hostname>` and `PUBLIC_APP_URL=https://<hostname>/`; `site` takes the server's `WORKER_TOKEN` from the environment and leaves the API's secrets empty. It lists what is still to fill in by hand (lot location, camera URLs, admin password hash, public origins).
- **Server VM:** any provider's small x86-64 or ARM64 VM that meets [hardware.md §4.3](hardware.md#43-production-api-server-topologies-t2t3), with a public IPv4 address.
