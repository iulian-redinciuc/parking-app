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
| `docker-compose.site.yml` | T2 lot box: vision services only, `API_INTERNAL_URL` = the API's VPN address |
| `docker-compose.server.yml` | T2 server: API + public entry only (`--profile web`: `parking-web`, Caddy + the frontend; or `--profile public`: the Cloudflare tunnel) |

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
    depends_on: { api: { condition: service_healthy } }
    healthcheck:                                               # same as vision-occupancy
      test: ["CMD", "python", "-c", "import socket;socket.create_connection(('localhost',9000),3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
      start_interval: 2s

  tunnel:
    container_name: parking-tunnel
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment: { TUNNEL_TOKEN: "${TUNNEL_TOKEN}" }
    restart: unless-stopped
    networks: [internal, egress]
    profiles: ["public"]

  tunnel-quick:                       # dev only (§5 Option Q)
    container_name: parking-tunnel-quick
    image: cloudflare/cloudflared:2026.10.0
    command: tunnel --no-autoupdate --config /etc/cloudflared/config.yml --url http://api:8000
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

- **Production only.** The dev Pi holds test data.
- Nightly: `docker compose exec api parking backup --out /app/data/backups` (host cron or a small `backup` service in the same compose project).
- Keep 14 daily + 8 weekly copies, and copy them **off the production machine** (object storage or another machine via `rclone`, encrypted).
- **Restore drill** in Phase 8, onto a spare machine (the dev Pi works).

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
- **`prod-env.sh server|site`** (`PARKING_VERSION=v0.x.y` required): writes a new `deploy/.env` (mode 600) from `.env.example`, never overwrites one, never prints a secret. `server` generates a fresh `WORKER_TOKEN`, `ADMIN_TOKEN` and VAPID key pair (from the released API image) and, given `PUBLIC_HOST=<hostname>`, sets `PUBLIC_HOST`, `CORS_ORIGINS=https://<hostname>` and `PUBLIC_APP_URL=https://<hostname>/`; `site` takes the server's `WORKER_TOKEN` from the environment and leaves the API's secrets empty. It lists what is still to fill in by hand (lot location, camera URLs, admin password hash, public origins).
- **Server VM:** any provider's small x86-64 or ARM64 VM that meets [hardware.md §4.3](hardware.md#43-production-api-server-topologies-t2t3), with a public IPv4 address.
