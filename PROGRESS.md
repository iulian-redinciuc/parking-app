# Parking App — Progress

> Overview: [PLAN.md](PLAN.md) · Docs index: [docs/README.md](docs/README.md)
> Task IDs (e.g. **P1.5**) link to the step-by-step guides. Tick a box when the task's "Done when" check passes.
>
> Status: ⬜ not started · 🟡 in progress · ✅ done · ⏸️ blocked · ⏭️ skipped
>
> The [agent loop](tools/agent-loop/README.md) works through the unticked tasks in order. A task marked `⏸️` is skipped until its need is met: **delete the `⏸️ ` from its line to unblock it** (editing on GitHub works too).

**Current focus:** Phase 0 → P0.1–P0.7 (skeleton), waiting on **P0.8** (sample image + answers).
**Last updated:** 2026-10-08

## Waiting on Iulian

- **P0.8**: sample photo(s) of the lot in `data/samples/` on the dev Pi ([what to send](docs/design/hardware.md#1-sample-images-for-phase-1-what-to-send)), and answers to open questions 2–4 below.

## Overview

| Phase | Name | Tasks | Status | Started | Finished |
|-------|------|-------|--------|---------|----------|
| 0 | [Foundations](docs/phases/phase-0-foundations.md) | 6 / 8 | 🟡 | 2026-10-07 | |
| 1 | [Still-image PoC](docs/phases/phase-1-still-image.md) | 0 / 11 | ⬜ | | |
| 2 | [Backend + simulated feed](docs/phases/phase-2-backend.md) | 0 / 11 | ⬜ | | |
| 3 | [Mobile web app](docs/phases/phase-3-frontend.md) | 0 / 10 | ⬜ | | |
| 4 | [Live occupancy camera](docs/phases/phase-4-occupancy-camera.md) | 0 / 11 | ⬜ | | |
| 5 | [Entry/exit camera](docs/phases/phase-5-flow-camera.md) | 0 / 11 | ⬜ | | |
| 6 | [Notifications](docs/phases/phase-6-notifications.md) | 0 / 8 | ⬜ | | |
| 7 | [Admin + stats](docs/phases/phase-7-admin-stats.md) | 0 / 8 | ⬜ | | |
| 8 | [Production deployment + hardening](docs/phases/phase-8-hardening.md) | 0 / 13 | ⬜ | | |
| 9 | [Extras](docs/phases/phase-9-extras.md) | 0 / 6 | ⬜ | | |

---

## Phase 0: Foundations 🟡
Planning done: repo created, GitHub Pages live, PLAN.md + docs written.
- [x] **P0.1** Repo layout and `.gitignore`
- [x] **P0.2** Backend project (uv, Python 3.12, Typer CLI, ruff, pytest)
- [x] **P0.3** Frontend scaffold (Vite, React, TS, Tailwind, Vitest)
- [x] **P0.4** Config templates (`lot.example.yaml`, `lot.yaml`, `.env.example`)
- [x] **P0.5** CI workflow
- [x] **P0.6** Secret protection (GitHub scanning, gitleaks pre-commit, Dependabot)
- [ ] **P0.7** README for developers
- [ ] ⏸️ **P0.8** Inputs from Iulian: sample image(s) + open questions 2–4 (needs: Iulian to provide the photos and answers)

## Phase 1: Still-image proof of concept ⬜
- [ ] **P1.1** Sample data layout
- [ ] **P1.2** Config loader
- [ ] **P1.3** Slot editor (slots / lines / label modes)
- [ ] **P1.4** Detector module + model export
- [ ] **P1.5** Geometry + occupancy scoring
- [ ] **P1.6** Annotated output image
- [ ] **P1.7** `parking analyze`
- [ ] **P1.8** Ground-truth labels + `parking evaluate`
- [ ] **P1.9** `parking bootstrap-slots`
- [ ] **P1.10** Benchmark on the dev Pi
- [ ] **P1.11** Tune and decide

## Phase 2: Backend + simulated live feed ⬜
- [ ] **P2.1** Message models + worker API client (with flow-event outbox)
- [ ] **P2.2** Frame sources + health checks
- [ ] **P2.3** Occupancy worker
- [ ] **P2.4** Smoothing
- [ ] **P2.5** State store + fusion
- [ ] **P2.6** Database (SQLModel + Alembic)
- [ ] **P2.7** Internal ingest endpoints
- [ ] **P2.8** SSE broadcaster + `/api/stream`
- [ ] **P2.9** REST endpoints, CORS, errors
- [ ] **P2.10** Docker images + Compose
- [ ] **P2.11** End-to-end test

## Phase 3: Mobile web app + public access ⬜
- [ ] **P3.1** App shell, routing, theme
- [ ] **P3.2** API types, client, mock
- [ ] **P3.3** Live connection manager + `useLiveStatus`
- [ ] **P3.4** Live screen components
- [ ] **P3.5** Banners and edge states
- [ ] **P3.6** i18n
- [ ] **P3.7** PWA
- [ ] **P3.8** Deploy the preview to GitHub Pages with Actions
- [ ] **P3.9** Dev API over HTTPS for phone testing
- [ ] **P3.10** Tests + quality gates

## Phase 4: Live occupancy camera ⬜
- [ ] **P4.1** Choose the production layout, the camera and the vision host
- [ ] **P4.2** Install and network
- [ ] **P4.3** Snapshot + RTSP sources
- [ ] **P4.4** Health tuning on the real camera
- [ ] **P4.5** Calibrate slots on the real view
- [ ] **P4.6** Camera shift detection
- [ ] **P4.7** Debug frame capture
- [ ] **P4.8** Validation set (~200 labelled frames)
- [ ] **P4.9** Evaluate and tune
- [ ] **P4.10** Per-slot classifier (only if needed)
- [ ] **P4.11** 7-day soak test (on the vision host)

## Phase 5: Entry/exit camera + combining levels ⬜
- [ ] **P5.1** Mount Camera A, draw lines
- [ ] **P5.2** Low-latency RTSP + video source + `parking record`
- [ ] **P5.3** Motion gate
- [ ] **P5.4** Tracker integration
- [ ] **P5.5** Two-line counter
- [ ] **P5.6** Flow worker
- [ ] **P5.7** FlowCounter in the API + corrections
- [ ] **P5.8** Fusion update + confidence
- [ ] **P5.9** Test clips, tally tool, `evaluate-flow`
- [ ] **P5.10** Performance on the vision host + accelerator decision
- [ ] **P5.11** Live week drift test

## Phase 6: Notifications ⬜
- [ ] **P6.1** VAPID keys + push sender
- [ ] **P6.2** Subscription storage + endpoints
- [ ] **P6.3** Service worker push handling
- [ ] **P6.4** Alerts screen + permission flow
- [ ] **P6.5** Tier 1: proximity while open
- [ ] **P6.6** Tier 2: "I'm on my way"
- [ ] **P6.7** Tier 3: schedules, quiet hours, almost-full
- [ ] **P6.8** Device test matrix

## Phase 7: Admin tools, history, stats ⬜
- [ ] **P7.1** Admin login
- [ ] **P7.2** Camera health page + snapshots
- [ ] **P7.3** Slot/line editor in admin
- [ ] **P7.4** Corrections UI + audit log
- [ ] **P7.5** Rollups + retention jobs
- [ ] **P7.6** History + forecast API
- [ ] **P7.7** Stats screen
- [ ] **P7.8** Admin alerts

## Phase 8: Production deployment and hardening ⬜
- [ ] **P8.1** Release pipeline (multi-arch images to GHCR)
- [ ] **P8.2** Provision production machines + re-measure on production hardware
- [ ] **P8.3** Production public entry + frontend hosting
- [ ] **P8.4** Compose hardening
- [ ] **P8.5** Watchdog for stuck workers
- [ ] **P8.6** Backups + restore drill
- [ ] **P8.7** Monitoring + external uptime check
- [ ] **P8.8** Security review
- [ ] **P8.9** Privacy deliverables
- [ ] **P8.10** Load test
- [ ] **P8.11** Power/network resilience
- [ ] **P8.12** Runbook + README
- [ ] **P8.13** 7-day staging run + go-live

## Phase 9: Optional extras ⬜
- [ ] **P9.1** Ground-level slot map
- [ ] **P9.2** Special spaces (accessible, EV)
- [ ] **P9.3** Barrier / induction-loop integration
- [ ] **P9.4** Fine-tuned detector / licence swap
- [ ] **P9.5** Multiple lots
- [ ] **P9.6** Smarter forecast

---

## Open questions

| # | Question | Answer | Status |
|---|----------|--------|--------|
| 1 | Sample image(s): normal, full, empty, night ([what to send](docs/design/hardware.md#1-sample-images-for-phase-1-what-to-send)) | | ⬜ waiting |
| 2 | **Where will production run?** Topology T1 / T2 / T3 ([deployment.md §3](docs/design/deployment.md#3-production-topologies-to-be-chosen)); power and internet at the lot. Needed before Phase 4 | | ⬜ |
| 3 | Spaces per level; marked spaces? One ramp? Separate in/out lanes? | | ⬜ |
| 4 | Camera layout: Option A / B / C ([PLAN §5](PLAN.md#5-key-design-decisions)); existing cameras? | | ⬜ |
| 5 | Who are the users (household / staff / public)? | | ⬜ |
| 6 | Domain on Cloudflare, or Tailscale Funnel? | | ⬜ |
| 7 | iPhone users needing notifications? | | ⬜ |
| 8 | Lot GPS location + notification radius (kept in `.env`) | | ⬜ |
| 9 | UI language(s) | | ⬜ |
| 10 | Commercial use? (Ultralytics AGPL) | | ⬜ |
| 11 | Hardware budget | | ⬜ |

## Decision log

| Date | Decision | Why |
|------|----------|-----|
| 2026-10-07 | Repo made public so GitHub Pages works on the free plan | Pages on private repos needs GitHub Pro |
| 2026-10-07 | Stack: Python/FastAPI + YOLO11 + SQLite; Vite/React/TS PWA; SSE; Web Push | [PLAN.md §4](PLAN.md#4-stack-summary) |
| 2026-10-07 | Still image → simulated feed → real cameras | De-risks vision first; Phases 1–3 need no hardware |
| 2026-10-07 | Notification tiers instead of a promised background geofence | Browsers can't track location in the background |
| 2026-10-08 | **Fully isolated:** no Home Assistant, no message broker, nothing shared with existing software on the dev Pi. Workers send results to the API over HTTP on the app's own private Docker network | Owner's requirement; also fewer moving parts |
| 2026-10-07 | Admin auth with bearer tokens, not cookies | Frontend and API are different sites; third-party cookies are blocked (Safari) and would need CSRF protection |
| 2026-10-07 | HashRouter in the frontend | GitHub Pages has no SPA fallback |
| 2026-10-07 | Python pinned to 3.12 (uv) instead of the dev Pi's 3.13 | ML wheels lag behind new Python versions |
| 2026-10-08 | **The Raspberry Pi is for development and testing only.** Production runs elsewhere (not decided; choose a topology before Phase 4). The design is portable: multi-arch images (amd64 + arm64), AI runtime per machine, machine-specific settings in config | Owner's requirement |
| 2026-10-08 | **Web app only.** No native or app-store app; the native-app extra was removed | Owner's requirement |
| 2026-10-08 | Production target: **the cloud or another Raspberry Pi** (exact topology chosen before Phase 4) | Owner's requirement |
| 2026-10-08 | GitHub Pages is the **preview** frontend; production frontend hosting decided in P8.3 | Keep options open |
| 2026-10-08 | Phase 8 became "Production deployment + hardening" (13 tasks) | Deployment to the new environment needs its own steps |
| 2026-10-08 | Backend uses a flat layout (`backend/parking/`, not `src/`) with `uv_build` and `module-root = ""`; uv is installed per user in `~/.local/bin` | Matches the module map in architecture.md; `uv init` defaults to `src/` |
| 2026-10-08 | Frontend lints with **ESLint** (typescript-eslint, react-hooks, react-refresh) + **Prettier** (`format`, `format:check` scripts), replacing the oxlint that `create-vite` now ships | frontend.md §1 and the definition of done name ESLint/Prettier |
| 2026-10-08 | `config/lot.yaml` is a copy of the example with placeholder zones/capacities until open questions 3–4 are answered. `deploy/.env.example` also lists the compose variables `PARKING_VERSION`, `VISION_CPUS`, `FLOW_CPUS` (added to config.md §5) | deployment.md §4 reads them from `.env` but config.md §5 didn't list them |
| 2026-10-08 | CI (`.github/workflows/ci.yml`) runs on every `push` and `pull_request`; the frontend job also runs `npm run format:check` (added to testing.md §5). P0.5 was verified on the push run to `main` (three green jobs) instead of opening a PR | The agent loop pushes straight to `main` and doesn't open PRs; the same three jobs run on PRs |
| 2026-10-08 | `vite.config.ts` reads `base` from `VITE_BASE` (default `/parking-app/`) already in P0.3; `frontend/.env.development` (`VITE_API_BASE=mock`) is committed because it holds no secret | Matches frontend.md §1; the guide asks for the file |
| 2026-10-08 | P0.6: added `.gitleaks.toml` (default rules + strict `aws-access-key-id-strict` rule, path allowlist for the phase-0 guide that quotes the example key). Dependabot covers pip/npm/github-actions now; the `docker` ecosystem is added in P2.10 when Dockerfiles exist. pre-commit installed with `uv tool install` | gitleaks' default AWS rule allowlists `…EXAMPLE` keys, so the "Done when" check passed through; Dependabot errors on a directory without a Dockerfile |

## Metrics

| Date | Phase | Metric | Value | Target | Notes |
|------|-------|--------|-------|--------|-------|
| | 1 | Slot accuracy (samples) | | ≥ 97% | |
| | 1 | Inference time per image (dev Pi, chosen settings) | | < 2 s | worst-case reference |
| | 4 | Slot accuracy (validation set) | | ≥ 97% | |
| | 4 | Count error ≤ 1 (% of frames) | | ≥ 95% | |
| | 5 | Flow event accuracy (3 clips) | | ≥ 98% | |
| | 5 | Flow drift per day | | ≤ 2 cars | |
| | 5 | Flow fps while active (vision host) | | ≥ 8 | |
| | 8 | Slot accuracy on production hardware | | ≥ 97% | |
| | 8 | Flow fps on production hardware | | ≥ 8 | |
| | 8 | SSE p95 delivery (500 clients, production) | | < 2 s | |

## Session log

### 2026-10-07
- Created the repo, enabled GitHub Pages, made the repo public (free-plan requirement).
- Checked the dev machine: Raspberry Pi 5 8 GB, Docker, Node 22, Python 3.13.
- Wrote PLAN.md, then split the details into `docs/design/` (11 specs) and `docs/phases/` (10 guides, 94 tasks).
- **Next:** P0.1–P0.7 skeleton; get P0.8 inputs (sample image + answers) to start Phase 1.

### 2026-10-08
- Removed Home Assistant and the message broker; the app is fully isolated on the dev Pi.
- Clarified that the Pi is **dev/test only**: added production topologies (T1/T2/T3), multi-arch images, AI runtime per machine, preview vs production frontend, and turned Phase 8 into production deployment + hardening.
- Web app only: removed the native-app extra (97 tasks in total). Production will be the cloud or another Raspberry Pi.
- P0.1: repo layout (`backend/`, `frontend/`, `config/`, `deploy/`, `data/`, `models/`) and `.gitignore`; checked that `data/` and `models/` contents, `out/`, `deploy/.env` and stray `.jpg` files are ignored while test-fixture `.jpg` files are not.
- P0.2: backend project with uv + Python 3.12.15 (`backend/pyproject.toml`, `uv.lock`), Typer CLI with `--version` and empty `models`/`worker`/`db`/`push`/`admin` sub-apps; `uv sync`, `uv run parking --version`, `pytest` (2 passed) and `ruff check` all pass on the dev Pi.
- P0.3: frontend scaffold (Vite 8, React 19, TS 6 strict, Tailwind v4 with the frontend.md §4 tokens, HashRouter "coming soon" page, Vitest smoke test, ESLint + Prettier, Playwright config for iPhone 13 / Pixel 7); `npm run build`, `npm test -- --run` (1 passed) and `npm run lint` pass.
- P0.4: `config/lot.example.yaml` (from config.md §1), `config/lot.yaml` (placeholders until open questions 3–4), `deploy/.env.example` (every config.md §5 variable, secrets empty); new `test_config_templates.py` checks spec coverage, empty secrets and that both YAML files parse; pytest (6 passed) and ruff pass.
- P0.5: CI workflow with `backend` (uv, ruff check + format, pytest), `frontend` (npm ci, lint, format check, vitest, build; Playwright later) and `secrets` (gitleaks) jobs on push and PR.
- P0.6: GitHub secret scanning + push protection enabled; `.pre-commit-config.yaml` (gitleaks v8.30.1, ruff v0.16.10) installed; `.gitleaks.toml`; `.github/dependabot.yml` (pip, npm, actions weekly). A commit containing the AWS example key ID is blocked locally; full-history gitleaks scan clean; ruff + pytest (6 passed) pass.
