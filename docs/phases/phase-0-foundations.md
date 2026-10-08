# Phase 0: Foundations

**Goal:** an empty but working skeleton (backend package, frontend app, CI, secret protection), plus the inputs needed for Phase 1.
**Needs hardware:** no.
**Specs used:** [architecture.md](../design/architecture.md), [config.md](../design/config.md), [testing.md](../design/testing.md), [security-privacy.md](../design/security-privacy.md).

## Deliverables
- Folder layout from [architecture.md §4–5](../design/architecture.md#4-backend-module-map-backendparking)
- `parking --version` works; `pytest` and `npm test` pass; CI green
- Secret scanning active
- Answers to the open questions; first sample image on the dev Pi

---

## P0.1: Repo layout and `.gitignore`
**Files:** `.gitignore`, `backend/`, `frontend/`, `tools/`, `config/`, `deploy/`, `data/.gitkeep`, `models/.gitkeep`

**Steps**
1. Create the folders. Add a `.gitkeep` in empty ones.
2. `.gitignore`:
   ```gitignore
   # private data (public repo!)
   data/*
   !data/.gitkeep
   models/*
   !models/.gitkeep
   out/
   deploy/.env
   *.sqlite*
   # python
   __pycache__/
   .venv/
   .pytest_cache/
   .ruff_cache/
   # node
   node_modules/
   frontend/dist/
   frontend/playwright-report/
   frontend/test-results/
   # misc
   .DS_Store
   *.mp4
   *.jpg
   *.jpeg
   !frontend/public/**/*.jpg
   !backend/tests/fixtures/**/*.jpg
   ```
3. Remove the old root `index.html` only in Phase 3, when the Pages preview switches to Actions. Until then it keeps the preview site up.

**Done when:** `git status` shows the new folders, and `touch data/x.jpg && git status --porcelain data/` prints nothing.

## P0.2: Backend project
**Files:** `backend/pyproject.toml`, `backend/uv.lock`, `backend/parking/__init__.py`, `backend/parking/cli.py`, `backend/tests/unit/test_cli.py`

**Steps**
1. Install uv and Python 3.12: `curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12`.
2. `cd backend && uv init --package --name parking --python 3.12`, move `src/parking/` to `parking/` (flat layout, see [architecture.md](../design/architecture.md)), then edit `pyproject.toml` (keep uv's `[build-system]` and add `[tool.uv.build-backend] module-root = ""`):
   ```toml
   [project]
   name = "parking"
   version = "0.1.0"
   requires-python = ">=3.12,<3.13"
   dependencies = [
     "typer>=0.12", "pydantic>=2.7", "pydantic-settings>=2.3", "pyyaml>=6",
     "numpy>=1.26", "shapely>=2.0", "opencv-python-headless>=4.10",
     "fastapi>=0.115", "uvicorn[standard]>=0.30", "sse-starlette>=2.1",
     "sqlmodel>=0.0.21", "alembic>=1.13",
     "pywebpush>=2.0", "apscheduler>=3.10,<4", "argon2-cffi>=23.1", "slowapi>=0.1.9", "httpx>=0.27",
   ]
   [project.optional-dependencies]
   vision = ["ultralytics>=8.3", "lap>=0.5", "ncnn>=1.0.20240410", "onnxruntime>=1.18"]
   openvino = ["openvino>=2024.3"]          # only if an Intel machine is chosen for production
   [project.scripts]
   parking = "parking.cli:app"
   [dependency-groups]
   dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.6"]
   [tool.ruff]
   line-length = 100
   [tool.ruff.lint]
   select = ["E", "F", "I", "B", "UP", "SIM"]
   [tool.pytest.ini_options]
   testpaths = ["tests"]
   markers = ["slow: needs the real model"]
   asyncio_mode = "auto"
   ```
   (Pins are minimums. `uv lock` resolves the current versions.)
3. `parking/cli.py`: a Typer app with a `--version` callback, plus sub-apps `models`, `worker`, `db`, `push`, `admin` (empty for now).
4. A test that runs `CliRunner().invoke(app, ["--version"])` and checks the output.
5. `uv sync && uv run parking --version && uv run pytest && uv run ruff check`.

**Done when:** all three commands succeed on the dev Pi.

## P0.3: Frontend scaffold
**Files:** `frontend/**`

**Steps**
1. `npm create vite@latest frontend -- --template react-ts`, then `cd frontend && npm i`.
2. Add Tailwind v4 (`npm i -D tailwindcss @tailwindcss/vite`), react-router (`npm i react-router`), i18next (`npm i i18next react-i18next`), vite-plugin-pwa (`npm i -D vite-plugin-pwa`), Vitest + Testing Library (`npm i -D vitest @testing-library/react @testing-library/jest-dom jsdom`), Playwright (`npm i -D @playwright/test`).
3. `vite.config.ts`: `base: '/parking-app/'`, the Tailwind plugin, Vitest `environment: 'jsdom'`. (The PWA plugin gets configured in P3.7.)
4. `App.tsx`: a `HashRouter` with one route showing "Parking: coming soon", using the colour tokens from [frontend.md §4](../design/frontend.md#4-styling-and-accessibility).
5. `.env.development`: `VITE_API_BASE=mock`.
6. A smoke test: renders the app and finds the heading.
7. Scripts: `dev`, `build`, `preview`, `lint`, `test`, `e2e`.

**Done when:** `npm run build && npm test -- --run && npm run lint` pass.

## P0.4: Config templates
**Files:** `config/lot.example.yaml`, `config/lot.yaml`, `deploy/.env.example`

**Steps**
1. Copy the example from [config.md §1](../design/config.md#1-configlotyaml) into `config/lot.example.yaml`.
2. `config/lot.yaml`: the same, with the real zone names and capacities (or placeholders until you answer the open questions).
3. `deploy/.env.example`: every variable from [config.md §5](../design/config.md#5-environment-variables-deployenv) with empty or dummy values and a comment.

**Done when:** the files exist and contain no real secrets.

## P0.5: CI workflow
**Files:** `.github/workflows/ci.yml`

**Steps:** implement jobs 1–3 from [testing.md §5](../design/testing.md#5-ci-githubworkflowsciyml) (`backend`, `frontend` without Playwright for now, `secrets` with `gitleaks/gitleaks-action@v2`). Trigger on `push` and `pull_request`.

**Done when:** a PR shows three green checks.

## P0.6: Secret protection
**Steps**
1. Turn on GitHub secret scanning and push protection: `gh api -X PATCH repos/iulian-redinciuc/parking-app -f "security_and_analysis[secret_scanning][status]=enabled" -f "security_and_analysis[secret_scanning_push_protection][status]=enabled"`.
2. Local pre-commit hook: `pipx install pre-commit` (or `uv tool install pre-commit`), `.pre-commit-config.yaml` with the `gitleaks` and `ruff` hooks, `pre-commit install`.
3. `.github/dependabot.yml` for `pip` (`/backend`), `npm` (`/frontend`), `github-actions` (`/`), weekly.

**Done when:** committing a file containing `AKIAIOSFODNN7EXAMPLE` is blocked locally (then delete the test file).

## P0.7: README for developers
**Files:** `README.md`

Sections: what it is, links to PLAN/PROGRESS/docs, prerequisites (uv, Node 22, Docker), "run backend tests", "run frontend in mock mode", "public repo rules" (link to [security-privacy.md §3](../design/security-privacy.md#3-public-repo-rules)).

**Done when:** following the README on a fresh clone gets tests running.

## P0.8: Inputs from you (owner: Iulian)
- [ ] Sample images as described in [hardware.md §1](../design/hardware.md#1-sample-images-for-phase-1-what-to-send), copied to `~/workspace/parking-app/data/samples/` (e.g. `scp` from your laptop, or `rsync`).
- [ ] Answers to the open questions in PROGRESS.md (at minimum #2 location, #3 layout, #4 camera option).

**Done when:** at least `data/samples/ground-01.jpg` exists and questions 2–4 are answered.

---

## Exit criteria
- [ ] CI green on `main`
- [ ] `parking --version`, `pytest`, `npm test`, `npm run build` all pass on the dev Pi
- [ ] Secret scanning + pre-commit active
- [ ] Sample image available; layout questions answered
