# Deployment

## 1. Environments

| Env | Where | How |
|-----|-------|-----|
| **Dev, backend** | Pi (or any Linux/Mac) | `uv` venv with Python 3.12 in `backend/`; workers and API run with `parking …`; a throwaway Mosquitto via `docker run` |
| **Dev, frontend** | anywhere | `npm run dev` with `VITE_API_BASE=mock` or `http://<pi>:8000` |
| **Production** | Pi, Docker Compose in `deploy/` | `docker compose up -d --build` from a checkout of `main` |
| **Frontend production** | GitHub Pages | GitHub Actions on push to `main` |

The host has Python 3.13. Pin the project to **3.12** (`uv python install 3.12`), because some ML wheels for ARM64 lag behind new Python versions. The Docker images use 3.12 too.

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
  networks: [internal]
  logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }

services:
  mosquitto:
    image: eclipse-mosquitto:2
    restart: unless-stopped
    volumes:
      - ./mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - ./mosquitto/acl:/mosquitto/config/acl:ro
      - ./mosquitto/passwd:/mosquitto/config/passwd:ro      # generated, git-ignored
      - mosquitto-data:/mosquitto/data
    networks: [internal]                                      # no published ports

  api:
    <<: *common
    build: { context: .., dockerfile: backend/Dockerfile, target: api }
    depends_on: [mosquitto]
    ports: ["127.0.0.1:8000:8000"]                            # LAN/tunnel only via host loopback
    networks: [internal, default]                             # default: reach home MQTT for HA bridge
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3

  vision-occupancy:
    <<: *common
    build: { context: .., dockerfile: backend/Dockerfile, target: vision }
    command: ["occupancy", "--camera", "cam-ground"]
    depends_on: [mosquitto]
    networks: [internal, cameras]
    deploy: { resources: { limits: { cpus: "1.0", memory: 1200M } } }
    environment: { OMP_NUM_THREADS: "2" }

  vision-flow:
    <<: *common
    build: { context: .., dockerfile: backend/Dockerfile, target: vision }
    command: ["flow", "--camera", "cam-ramp"]
    profiles: ["flow"]                                        # enabled from Phase 5
    depends_on: [mosquitto]
    networks: [internal, cameras]
    deploy: { resources: { limits: { cpus: "1.5", memory: 1200M } } }
    environment: { OMP_NUM_THREADS: "2" }

  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment: { TUNNEL_TOKEN: "${TUNNEL_TOKEN}" }
    restart: unless-stopped
    networks: [default]
    profiles: ["public"]                                      # enabled from Phase 3

networks:
  internal: { internal: true }
  cameras: {}            # needs a route to the camera VLAN; host networking is a fallback
  default: {}

volumes:
  mosquitto-data: {}
```

Notes:
- `internal: true` → nothing outside the stack can reach Mosquitto or the workers.
- Workers on `cameras` must be able to reach the camera IPs. If Docker routing to the camera VLAN is awkward, use `network_mode: host` for the vision services only (and then they reach Mosquitto via a published loopback port).
- **cloudflared reaches the API by its service name** `http://api:8000`. Configure that as the tunnel's public hostname target in the Cloudflare dashboard.

## 4. MQTT brokers

| Broker | Purpose | Auth |
|--------|---------|------|
| **mosquitto (parking stack)** | workers ↔ API | `allow_anonymous false`, password file, ACL |
| **Home Assistant's broker** (optional, existing) | **only** receives `parking/main/status` + HA discovery from the API | whatever it already uses (not modified by this project) |

Why not reuse a shared home broker for everything: other devices on the network can usually reach it, and any client with write access could publish fake counts. The parking broker is private to the stack.

`deploy/mosquitto/mosquitto.conf`:
```
listener 1883
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl
persistence true
persistence_location /mosquitto/data/
```

`deploy/mosquitto/acl`:
```
user parking-worker
topic write parking/+/camera/+/observation
topic write parking/+/camera/+/flow
topic write parking/+/camera/+/health
topic write parking/+/camera/+/snapshot/#
topic read  parking/+/camera/+/cmd

user parking-api
topic readwrite parking/#
```

Create the password file: `docker run --rm -v $PWD/deploy/mosquitto:/m eclipse-mosquitto:2 mosquitto_passwd -c -b /m/passwd parking-worker '<pw>'`, then the same with `-b` (no `-c`) for `parking-api`.

The API reaches the home broker at the Docker host gateway (`HA_MQTT_URL=mqtt://172.17.0.1:1883` or `host.docker.internal` with `extra_hosts: ["host.docker.internal:host-gateway"]`).

## 5. Public access for the API

### Option A: Cloudflare Tunnel (recommended if you have a domain on Cloudflare)
1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → **Create tunnel** (cloudflared) → copy the **token** to `TUNNEL_TOKEN` in `.env`.
2. Public hostname: `parking-api.<domain>` → service `http://api:8000`.
3. `docker compose --profile public up -d`.
4. Cloudflare settings for SSE: no caching for `/api/*` (a Cache Rule "bypass"). SSE is passed through with the 15 s pings.

### Option B: Tailscale Funnel (no domain needed)
1. Install Tailscale on the Pi, `tailscale up`.
2. `tailscale funnel --bg 8000` → `https://<pi-name>.<tailnet>.ts.net`.
3. Use that as `VITE_API_BASE`, and add it to nothing else (CORS lists the *frontend* origin, not the API).

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

- Nightly at 03:30 (host cron or a small `backup` service): `docker compose exec api parking backup --out /app/data/backups`.
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

- At the lot: Pi 5 + PoE switch + cameras + router (4G/5G if there's no wired internet). Runs **only the vision services** with a compose override.
- Workers connect to the parking broker over the internet with **MQTT over TLS + auth**: expose Mosquitto through the tunnel as TCP, or run a WireGuard/Tailscale link between the two Pis (simpler and recommended).
- Video never leaves the site. Bandwidth is a few KB per minute.
