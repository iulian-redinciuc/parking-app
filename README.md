# Parking App

Shows how many parking spaces are free, in real time, from fixed camera feeds, in a mobile-friendly web app.

Camera workers detect cars (YOLO), a FastAPI backend fuses their results and pushes live updates (SSE) to a React PWA. Web app only: no native or app-store app.

Development and testing happen on a Raspberry Pi; production will be deployed elsewhere, in the cloud or on another Raspberry Pi (see [PLAN.md §2](PLAN.md#2-environments-development-vs-production)).

## Links

- **Plan:** [PLAN.md](PLAN.md): goal, architecture, stack, roadmap
- **Progress:** [PROGRESS.md](PROGRESS.md): task checklist, decisions, open questions
- **Docs:** [docs/README.md](docs/README.md): design specs, phase guides, conventions and definition of done
- **Runbook:** [docs/runbook.md](docs/runbook.md): what to do when something is wrong (a stale camera, wrong counts, the server unreachable), and how to deploy, roll back, restore and rotate secrets
- **Agent loop** (works through the tasks automatically): [tools/agent-loop](tools/agent-loop/README.md)
- **Preview site (development):** https://iulian-redinciuc.github.io/parking-app/

## Repo layout

| Path | What |
|------|------|
| `backend/` | Python package `parking`: CLI, vision workers, API (uv, Python 3.12) |
| `frontend/` | Vite + React + TypeScript + Tailwind PWA |
| `config/` | `lot.example.yaml` (template) and `lot.yaml` ([config.md](docs/design/config.md)) |
| `deploy/` | `.env.example` (copy to the git-ignored `deploy/.env`), the Compose files (`docker-compose.yml` + `docker-compose.dev.yml` for local builds, `docker-compose.server.yml` / `docker-compose.site.yml` for production), `Caddyfile` (public entry), `backup.sh`, and `scripts/` (provisioning, production `.env`, boot / security checks, dev phone testing): [deployment.md](docs/design/deployment.md) |
| `data/`, `models/` | Camera images, labels and model weights. **Git-ignored**, never committed |
| `scripts/` | Tests run from outside against a running system: `load/sse.py` (load test), `resilience/drill.py` (power and network drills): [testing.md](docs/design/testing.md) |
| `tools/` | `slot-editor/` (draw parking spaces, lines and labels on a picture), `flow-tally/` (count cars in a clip by hand), `agent-loop/` |
| `docs/` | Design specs, phase guides and the runbook |

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** (Python package manager). It installs Python 3.12 for you.
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **Node.js 22** with npm (e.g. via [nvm](https://github.com/nvm-sh/nvm) or [NodeSource](https://github.com/nodesource/distributions)).
- **Docker** with the Compose plugin, for the full stack (not needed for the tests or the frontend in mock mode).
- Optional: **[pre-commit](https://pre-commit.com/)** for the local gitleaks + ruff hooks (see [Public repo rules](#public-repo-rules)).

```bash
git clone https://github.com/iulian-redinciuc/parking-app.git
cd parking-app
```

## Run the backend tests

```bash
cd backend
uv sync                       # creates .venv with Python 3.12 and the dev tools
uv run pytest -m "not slow"   # "slow" tests need the real model
uv run ruff check .
uv run ruff format --check .
uv run parking --version      # the CLI
```

The heavy vision dependencies (Ultralytics, NCNN, ONNX Runtime) are an optional extra, needed to run the detector or a worker outside Docker: `uv sync --extra vision`.

## Run the frontend in mock mode

```bash
cd frontend
npm ci
npm run dev                   # http://localhost:5173/parking-app/
```

`frontend/.env.development` sets `VITE_API_BASE=mock`, so the app runs without a backend and uses simulated data ([frontend.md](docs/design/frontend.md)). To use a local API instead, run `VITE_API_BASE=http://localhost:8000 npm run dev`.

Checks (the same ones CI runs):

```bash
npm run lint
npm run format:check          # npm run format to fix
npm test -- --run
npm run build
```

## Run the full stack in Docker

The API and the occupancy worker replay a **simulated feed**: 200 frames made from the one sample photo, with cars arriving and leaving ([vision.md §10](docs/design/vision.md#simulated-feed-parkingvisionsimulatepy-p212)). The frames are git-ignored; make them (again) with:

```bash
cd backend && uv run parking simulate-feed --camera cam-ground --base data/samples/ground-01.jpg --frames 200 --seed 1 --day
```

Then start the stack:

```bash
cd deploy
cp .env.example .env && sed -i "s/^WORKER_TOKEN=.*/WORKER_TOKEN=$(openssl rand -hex 32)/" .env
sed -i "s/^DOCKER_GID=.*/DOCKER_GID=$(getent group docker | cut -d: -f3)/" .env   # for parking-autoheal
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build   # builds parking-api / parking-vision-occupancy
docker compose ps                                       # all "healthy"
curl localhost:8000/api/status
docker compose down                                     # stop; add -v --rmi local to remove everything
```

Only `127.0.0.1:8000` is published. Details and isolation rules: [deployment.md](docs/design/deployment.md). The sample photo and its labels are private and not in the repository; setting a development machine up from nothing, phone testing included: [runbook → Rebuild the dev environment](docs/runbook.md#rebuild-the-dev-environment).

## Production

Production is not the development Pi: it runs from released, version-pinned images on its own machines (working choice: a small cloud VM for the API and the app, a Raspberry Pi at the lot for the cameras, joined by a private VPN; [deployment.md §3](docs/design/deployment.md#3-production-topologies-to-be-chosen)). It isn't deployed yet: see [PROGRESS.md](PROGRESS.md) for what is waiting on hardware.

| To | Read |
|----|------|
| Make a release (`git tag v0.x.y` → multi-arch images in GHCR) | [deployment.md §8](docs/design/deployment.md#8-releases-and-updating) |
| Set the machines up | [phase guide P8.2–P8.3](docs/phases/phase-8-hardening.md#p82-provision-the-production-machines), [deployment.md §10](docs/design/deployment.md#10-provisioning-the-production-machines-t2) |
| Update, roll back, restore, rotate a secret, add a camera | [runbook](docs/runbook.md) |
| Fix something that is broken | [runbook](docs/runbook.md) |

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the backend checks, the frontend checks and a gitleaks secret scan on every push and pull request; [`release.yml`](.github/workflows/release.yml) builds and publishes the images for a `v*` tag. Details in [testing.md](docs/design/testing.md).

## Public repo rules

This repository is **public**. Never commit camera images or video, labels tied to real images, model weights, camera URLs or credentials, tokens, keys or `deploy/.env`. Those belong in `data/`, `models/` or `deploy/.env`, which are git-ignored. Full rules: [security-privacy.md §3](docs/design/security-privacy.md#3-public-repo-rules).

Install the local hooks so gitleaks and ruff run before every commit:

```bash
uv tool install pre-commit
pre-commit install
```

If a secret slips through anyway: rotate it first, then clean the history.

## Contributing

Pick the next unticked task in [PROGRESS.md](PROGRESS.md), follow its phase guide, and start the commit message with the task ID (`P1.5: …`). See the conventions in [docs/README.md](docs/README.md#conventions).
