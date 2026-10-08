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
| Phone testing | Frontend **preview** on GitHub Pages (§6). The dev API is reachable over HTTPS through the `parking-tunnel` container **only while testing** (`docker compose --profile public up -d`, then `down` afterwards) |
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

- The build context is the repo root; `.dockerignore` lets in only `backend/` (minus `.venv`, caches and `tests/`), so `data/`, `models/` and `deploy/.env` never reach an image.
- The working directory is `/app`, so the default `config/lot.yaml` and the relative paths in it resolve against the mounted `/app/config`, `/app/data` and `/app/models`. Containers run as uid 1000 (the owner of the repo folders on the dev Pi).
- The **api** image stays small (no PyTorch). Only **vision** carries the ML stack (expect ~2 GB).
- **Multi-arch:** CI builds every image for `linux/amd64` **and** `linux/arm64` with `docker buildx`, so the same version runs on the dev Pi and on any production machine. On PRs, CI only builds them (both CPU types) to catch "works on ARM, breaks on x86" early. On a release tag (`v*`), CI pushes them to **GitHub Container Registry**: `ghcr.io/iulian-redinciuc/parking-api:<version>` and `parking-vision:<version>`.
- **NVIDIA hosts** (if chosen) need a separate CUDA-based `vision` variant (`parking-vision:<version>-cuda`), built only if that hardware is picked.
- Model weights are **not** baked in: they're mounted from `./models`, created on each machine with `parking models export --runtime <runtime>` (the export is tuned to that machine's runtime; see [vision.md §11](vision.md#11-runtimes-and-performance)).
- `opencv-python-headless` (not `opencv-python`), since there's no GUI in containers.

## 3. Production topologies (to be chosen)

Pick one before Phase 4 hardware goes in (open question #2). The code supports all three; only configuration differs.

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
| `docker-compose.site.yml` | T2 lot box: vision services only, `API_INTERNAL_URL` = the API's VPN address |
| `docker-compose.server.yml` | T2 server: API + public entry only |

Base file (abridged):
```yaml
name: parking

x-common: &common
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
    deploy: { resources: { limits: { cpus: "${VISION_CPUS:-1.0}", memory: 1200M } } }
    environment: { OMP_NUM_THREADS: "2", API_INTERNAL_URL: "${API_INTERNAL_URL:-http://api:8000}" }
    depends_on: { api: { condition: service_healthy } }
    healthcheck:                                               # the /control/* server is up
      test: ["CMD", "python", "-c", "import socket;socket.create_connection(('localhost',9000),3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
      start_interval: 2s

  vision-flow:
    <<: *common
    container_name: parking-vision-flow
    image: ghcr.io/iulian-redinciuc/parking-vision:${PARKING_VERSION:-latest}
    command: ["flow", "--camera", "cam-ramp"]
    profiles: ["flow"]                                         # enabled from Phase 5
    networks: [internal, egress]
    deploy: { resources: { limits: { cpus: "${FLOW_CPUS:-1.5}", memory: 1200M } } }
    environment: { OMP_NUM_THREADS: "2", API_INTERNAL_URL: "${API_INTERNAL_URL:-http://api:8000}" }

  tunnel:
    container_name: parking-tunnel
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment: { TUNNEL_TOKEN: "${TUNNEL_TOKEN}" }
    restart: unless-stopped
    networks: [internal, egress]
    profiles: ["public"]

networks:
  internal: { internal: true }
  egress: {}
```

Dev override (`docker-compose.dev.yml`): each service drops the GHCR `image` (`image: !reset null`, Compose ≥ 2.24) and gets `build: { context: .., dockerfile: backend/Dockerfile, target: api | vision }`, so the local images are named `parking-api` / `parking-vision-occupancy` and `down --rmi local` removes them. `../backend/parking` is mounted read-only over the installed code (the project is installed editable), so a code change only needs `docker compose … restart`.

Notes:
- On one machine, workers reach the API at `http://api:8000/internal/*`, and the API reaches workers at `http://vision-occupancy:9000/control/*`. Both use `WORKER_TOKEN`.
- Across machines (T2), see §9.
- Workers need a route to the camera IPs. If Docker bridge routing can't reach the camera network, set `network_mode: host` on the vision services only.
- CPU and memory limits come from `.env`, because they differ per machine.

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

### Option B: reverse proxy on a machine with a public IP (T2/T3 server)
A `parking-caddy` container (Caddy obtains HTTPS certificates automatically):
```
<api-host> {
  @public path /api/* /healthz
  handle @public { reverse_proxy api:8000 { flush_interval -1 } }   # -1: stream SSE immediately
  handle { respond 404 }
}
```
Open ports 80/443 on that server's firewall only.

## 6. Frontend hosting

| Stage | Where | Build settings |
|-------|-------|----------------|
| **Preview** (development, now) | GitHub Pages: `https://iulian-redinciuc.github.io/parking-app/` | `VITE_BASE=/parking-app/`, `VITE_API_BASE` = dev API URL or `mock` |
| **Production** (decided in Phase 8) | Options: keep GitHub Pages (custom domain possible); any static host (Cloudflare Pages, Netlify); or the production reverse proxy serving the built files (same origin as the API, so no CORS needed) | `VITE_BASE` and `VITE_API_BASE` for that host |

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
        env: { VITE_BASE: "/parking-app/", VITE_API_BASE: "${{ vars.API_BASE }}" }
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
Set the preview API URL: `gh variable set API_BASE --body "https://parking-api-dev.<domain>"` (or `mock`).

## 7. Backups

- **Production only.** The dev Pi holds test data.
- Nightly: `docker compose exec api parking backup --out /app/data/backups` (host cron or a small `backup` service in the same compose project).
- Keep 14 daily + 8 weekly copies, and copy them **off the production machine** (object storage or another machine via `rclone`, encrypted).
- **Restore drill** in Phase 8, onto a spare machine (the dev Pi works).

## 8. Releases and updating

- **Release:** `git tag v0.x.y && git push --tags` → CI builds and pushes multi-arch images to GHCR.
- **Deploy to production:**
  ```bash
  cd /opt/parking/deploy                       # a checkout of the repo's deploy/ + config/ on that machine
  PARKING_VERSION=v0.x.y docker compose pull
  PARKING_VERSION=v0.x.y docker compose --profile public --profile flow up -d
  docker compose logs -f --tail=100 api
  ```
- Migrations run automatically at API start-up. **Roll back** by deploying the previous version (and restore the DB if a migration wasn't backwards compatible).
- The dev Pi uses `docker-compose.dev.yml` (local builds) and never needs releases.

## 9. Workers and API on different machines (T2)

- Connect the lot box and the API server with a **private VPN** (WireGuard or Tailscale, set up just for this app). Workers use `API_INTERNAL_URL=http://<api-vpn-address>:8000`, and the API reaches each worker's `/control/*` at its VPN address (config `cameras[].control_url`).
- `/internal/*` therefore never crosses the public internet, and the public entry (§5) still forwards only `/api/*` and `/healthz`.
- Workers keep their flow-event **outbox**, so a flaky lot connection loses nothing.
- Video never leaves the site. Bandwidth is a few KB per minute.
