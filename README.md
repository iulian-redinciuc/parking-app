# Parking App

Shows how many parking spaces are free, in real time, from fixed camera feeds, in a mobile-friendly web app.

Camera workers detect cars (YOLO), a FastAPI backend fuses their results and pushes live updates (SSE) to a React PWA. Web app only: no native or app-store app.

Development and testing happen on a Raspberry Pi; production will be deployed elsewhere, in the cloud or on another Raspberry Pi (see [PLAN.md §2](PLAN.md#2-environments-development-vs-production)).

## Links

- **Plan:** [PLAN.md](PLAN.md): goal, architecture, stack, roadmap
- **Progress:** [PROGRESS.md](PROGRESS.md): task checklist, decisions, open questions
- **Docs:** [docs/README.md](docs/README.md): design specs, phase guides, conventions and definition of done
- **Agent loop** (works through the tasks automatically): [tools/agent-loop](tools/agent-loop/README.md)
- **Preview site (development):** https://iulian-redinciuc.github.io/parking-app/

## Repo layout

| Path | What |
|------|------|
| `backend/` | Python package `parking`: CLI, vision workers, API (uv, Python 3.12) |
| `frontend/` | Vite + React + TypeScript + Tailwind PWA |
| `config/` | `lot.example.yaml` (template) and `lot.yaml` ([config.md](docs/design/config.md)) |
| `deploy/` | `.env.example` (copy to the git-ignored `deploy/.env`), Compose files from Phase 2 |
| `data/`, `models/` | Camera images, labels and model weights. **Git-ignored**, never committed |
| `docs/` | Design specs and phase guides |

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** (Python package manager). It installs Python 3.12 for you.
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **Node.js 22** with npm (e.g. via [nvm](https://github.com/nvm-sh/nvm) or [NodeSource](https://github.com/nodesource/distributions)).
- **Docker** with the Compose plugin (used from Phase 2 for the full stack; not needed for the steps below).
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

The heavy vision dependencies (Ultralytics, NCNN, ONNX Runtime) are an optional extra and are only needed from Phase 1: `uv sync --extra vision`.

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

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the backend checks, the frontend checks and a gitleaks secret scan on every push and pull request. Details in [testing.md](docs/design/testing.md).

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
