# Deployment

## 0. Isolation from everything else on the Pi

The parking app **does not use, connect to, or modify anything already installed on the Pi**. In practice:

| Rule | How |
|------|-----|
| Own Compose project | `name: parking` in `deploy/docker-compose.yml`; every container is prefixed `parking-` |
| Own networks | `parking_internal` (private, `internal: true`) and `parking_egress` (only for containers that must reach cameras or the internet) |
| Own data | Everything lives under `~/workspace/parking-app/` (`config/`, `data/`, `models/`). No named volumes shared with other projects |
| No shared services | No existing brokers, databases, reverse proxies, home-automation software or host tunnels are used. The tunnel runs in **its own** container |
| Host ports | Only `127.0.0.1:8000` (API, loopback only). If 8000 is ever taken, change it in `.env` (`API_HOST_PORT`) |
| Host Python | Untouched. `uv` installs Python 3.12 in the user's own uv folder, and the containers carry their own Python |
| Removal | `cd deploy && docker compose down -v --rmi local`, then delete the folder |

## 1. Environments

| Env | Where | How |
|-----|-------|-----|
| **Dev, backend** | Pi (or any Linux/Mac) | `uv` venv with Python 3.12 in `backend/`; API and workers run with `parking …` |
| **Dev, frontend** | anywhere | `npm run dev` with `VITE_API_BASE=mock` or `http://<pi>:8000` |
| **Production** | Pi, Docker Compose in `deploy/` | `docker compose up -d --build` from a checkout of `main` |
| **Frontend production** | GitHub Pages | GitHub Actions on push to `main` |

Pin the project to **Python 3.12** (`uv python install 3.12`), because some ML wheels for ARM64 lag behind new Python versions. The Docker images use 3.12 too.

## 2. Docker images (`backend/Dockerfile`, multi-stage)

```dockerfile
FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN pip install uv

FROM base AS api
RUN cd backend && uv sync --frozen --no-dev            # core deps only (no torch)
COPY backend/ ./backend/
USER 1000:1000
CMD ["/app/backend/.venv/bin/parking", "api", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS vision
RUN cd backend && uv sync --frozen --no-dev --extra vision   # ultralytics, ncnn, torch (cpu)
COPY backend/ ./backend/
USER 1000:1000
ENTRYPOINT ["/app/backend/.venv/bin/parking", "worker"]
```

- The **api** image stays small (no PyTorch). Only **vision** carries the ML stack (expect ~2 GB).
- Model weights are **not** baked in: they're mounted from `./models` (created by `parking models export`).
- `opencv-python-headless` (not `opencv-python`) in `pyproject.toml`, since there's no GUI in containers.

## 3. Compose (`deploy/docker-compose.yml`)

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
    build: { context: .., dockerfile: backend/Dockerfile, target: api }
    ports: ["127.0.0.1:${API_HOST_PORT:-8000}:8000"]          # loopback only
    networks: [internal, egress]                               # egress: Web Push to browser push services
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3

  vision-occupancy:
    <<: *common
    container_name: parking-vision-occupancy
    build: { context: .., dockerfile: backend/Dockerfile, target: vision }
    command: ["occupancy", "--camera", "cam-ground"]
    depends_on: [api]
    networks: [internal, egress]                               # egress: reach the camera
    deploy: { resources: { limits: { cpus: "1.0", memory: 1200M } } }
    environment: { OMP_NUM_THREADS: "2", API_INTERNAL_URL: "http://api:8000" }

  vision-flow:
    <<: *common
    container_name: parking-vision-flow
    build: { context: .., dockerfile: backend/Dockerfile, target: vision }
    command: ["flow", "--camera", "cam-ramp"]
    profiles: ["flow"]                                         # enabled from Phase 5
    depends_on: [api]
    networks: [internal, egress]
    deploy: { resources: { limits: { cpus: "1.5", memory: 1200M } } }
    environment: { OMP_NUM_THREADS: "2", API_INTERNAL_URL: "http://api:8000" }

  tunnel:
    container_name: parking-tunnel
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment: { TUNNEL_TOKEN: "${TUNNEL_TOKEN}" }
    restart: unless-stopped
    networks: [internal, egress]
    profiles: ["public"]                                       # enabled from Phase 3

networks:
  internal: { internal: true }     # containers talk to each other here; no outside access
  egress: {}                       # outbound only (cameras, push services, Cloudflare)
```

Notes:
- Workers reach the API at `http://api:8000/internal/*`; the API reaches workers at `http://vision-occupancy:9000/control/*`. Both use `WORKER_TOKEN`.
- Workers need a route to the camera IPs. Docker's default bridge routing usually covers the LAN. If the cameras sit on a separate VLAN that Docker can't reach, set `network_mode: host` on the vision services only (they then reach the API at `http://127.0.0.1:${API_HOST_PORT}`).

## 4. Worker ↔ API communication

Plain HTTP on the private `internal` network. There's **no message broker**: see [api.md §5](api.md#5-internal-endpoints-workers--api). That means one less service to run and nothing that could be confused with other software on the Pi.

## 5. Public access for the API

Only `/api/*` and `/healthz` may be reachable from the internet. `/internal/*` must not be.

### Option A: Cloudflare Tunnel in its own container (recommended if you have a domain on Cloudflare)
1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → **Create tunnel** (cloudflared) → copy the **token** to `TUNNEL_TOKEN` in `deploy/.env`.
2. Public hostnames for the tunnel (order matters):
   - `parking-api.<domain>`, path `^/(api/|healthz$)` → service `http://api:8000`
   - `parking-api.<domain>` (no path) → **HTTP 404** (the "catch-all" rule)
3. `docker compose --profile public up -d`.
4. Cloudflare Cache Rule: bypass the cache for `parking-api.<domain>/*`, so SSE and live data are never cached.

This uses only the `parking-tunnel` container; any `cloudflared` installed on the host is neither used nor changed.

### Option B: Tailscale Funnel in its own container (no domain needed)
1. Add a `tailscale/tailscale` service to the compose file (its own state folder in `data/tailscale/`, an auth key in `.env`), with a serve config that publishes only `/api/` and `/healthz` to `http://api:8000`.
2. Turn on Funnel for that node → `https://parking.<tailnet>.ts.net`.
3. Use that as `VITE_API_BASE`.

## 6. Frontend on GitHub Pages

`.github/workflows/pages.yml`:
```yaml
name: Deploy frontend
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
        env: { VITE_API_BASE: "${{ vars.API_BASE }}" }
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

One-time switch from "deploy from branch" to Actions:
`gh api -X PUT repos/iulian-redinciuc/parking-app/pages -f build_type=workflow`

Set the API URL: `gh variable set API_BASE --body "https://parking-api.<domain>"` (use `mock` until the tunnel exists).

## 7. Backups

- Nightly at 03:30, from a host crontab entry that only runs `docker compose -p parking exec api parking backup --out /app/data/backups` (or a small `backup` service in the same compose project).
- Keep 14 daily + 8 weekly copies. Copy them off the Pi (another disk or a cloud drive with `rclone`), because a backup on the same NVMe doesn't protect against the disk dying.
- **Restore test** once per phase from Phase 8: stop the API, copy the backup over `data/db/parking.sqlite`, start, verify `/api/status`.

## 8. Updating

```bash
cd ~/workspace/parking-app && git pull
cd deploy && docker compose --profile public --profile flow up -d --build
docker compose logs -f --tail=100 api
```
Migrations run automatically at API start-up. Roll back with `git checkout <previous-tag>` and the same command (and restore the DB if a migration was not backwards compatible).

## 9. Edge box variant (lot is elsewhere)

- At the lot: Pi 5 + PoE switch + cameras + router (4G/5G if there's no wired internet). Runs **only the vision services** with a compose override that sets `API_INTERNAL_URL` to the main Pi.
- Link the two Pis with a private VPN (WireGuard or Tailscale, set up just for this), so `/internal/*` still never touches the public internet. Workers keep their flow-event outbox, so a flaky link loses nothing.
- Video never leaves the site. Bandwidth is a few KB per minute.
