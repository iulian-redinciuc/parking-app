# Parking App — Progress

> Overview: [PLAN.md](PLAN.md) · Docs index: [docs/README.md](docs/README.md)
> Task IDs (e.g. **P1.5**) link to the step-by-step guides. Tick a box when the task's "Done when" check passes.
>
> Status: ⬜ not started · 🟡 in progress · ✅ done · ⏸️ blocked · ⏭️ skipped
>
> The [agent loop](tools/agent-loop/README.md) works through the unticked tasks in order. A task marked `⏸️` is skipped until its need is met: **delete the `⏸️ ` from its line to unblock it** (editing on GitHub works too).

**Current focus:** the **MVP** (Phases 0–3, see [PLAN §6](PLAN.md#mvp)). Now Phase 1: P1.4 top-down scoring for the sample photo, then P1.9–P1.11.
**Build order:** 0 → 1 → 2 → 3 (MVP) → 6 → 7 (no hardware needed) → 4 → 5 → 8 (need the real cameras / production machines) → 9.
**Last updated:** 2026-10-08

## Waiting on Iulian

- **Nothing for the MVP.** Undecided open questions use the working assumptions below; technical choices are made by the agent and recorded in the decision log.
- Later, when available (not needed now): photos or video from the **real camera position** (Phase 4) and **where production runs** (Phase 8). More sample photos (busy, nearly empty, night) would help tune the vision.

## Overview

| Phase | Name | Tasks | Status | Started | Finished |
|-------|------|-------|--------|---------|----------|
| 0 | [Foundations](docs/phases/phase-0-foundations.md) (MVP) | 8 / 8 | ✅ | 2026-10-07 | 2026-10-08 |
| 1 | [Still-image PoC](docs/phases/phase-1-still-image.md) (MVP) | 7 / 11 | 🟡 | 2026-10-08 | |
| 2 | [Backend + simulated feed](docs/phases/phase-2-backend.md) (MVP) | 0 / 11 | ⬜ | | |
| 3 | [Mobile web app](docs/phases/phase-3-frontend.md) (MVP) | 0 / 10 | ⬜ | | |
| 4 | [Live occupancy camera](docs/phases/phase-4-occupancy-camera.md) | 0 / 11 | ⬜ | | |
| 5 | [Entry/exit camera](docs/phases/phase-5-flow-camera.md) | 0 / 11 | ⬜ | | |
| 6 | [Notifications](docs/phases/phase-6-notifications.md) | 0 / 8 | ⬜ | | |
| 7 | [Admin + stats](docs/phases/phase-7-admin-stats.md) | 0 / 8 | ⬜ | | |
| 8 | [Production deployment + hardening](docs/phases/phase-8-hardening.md) | 0 / 13 | ⬜ | | |
| 9 | [Extras](docs/phases/phase-9-extras.md) | 0 / 6 | ⬜ | | |

---

## Phase 0: Foundations ✅
Planning done: repo created, GitHub Pages live, PLAN.md + docs written.
- [x] **P0.1** Repo layout and `.gitignore`
- [x] **P0.2** Backend project (uv, Python 3.12, Typer CLI, ruff, pytest)
- [x] **P0.3** Frontend scaffold (Vite, React, TS, Tailwind, Vitest)
- [x] **P0.4** Config templates (`lot.example.yaml`, `lot.yaml`, `.env.example`)
- [x] **P0.5** CI workflow
- [x] **P0.6** Secret protection (GitHub scanning, gitleaks pre-commit, Dependabot)
- [x] **P0.7** README for developers
- [x] **P0.8** Inputs from Iulian: sample image(s) + open questions 2–4 (one photo; questions 2–4 not decided, working assumptions recorded)

## Phase 1: Still-image proof of concept 🟡
- [x] **P1.1** Sample data layout
- [x] **P1.2** Config loader
- [x] **P1.3** Slot editor (slots / lines / label modes)
- [ ] **P1.4** Detector module + model export, plus top-down appearance scoring for the MVP (detector part done; the sample is shot straight down, see the guide)
- [x] **P1.5** Geometry + occupancy scoring
- [x] **P1.6** Annotated output image
- [x] **P1.7** `parking analyze`
- [x] **P1.8** Ground-truth labels + `parking evaluate`
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
| 1 | Sample image(s): normal, full, empty, night ([what to send](docs/design/hardware.md#1-sample-images-for-phase-1-what-to-send)) | One photo: `data/samples/ground-01.jpg` (1932×2576, portrait, daytime, ground level shot from high up, almost straight down). Two rows of painted perpendicular spaces: left row ~8 spaces fully visible (2 taken), right row cut off at the image edge (3 cars visible). Later a second, full-resolution shot from the same window (`ground-02.jpg`, framing slightly shifted). No full/empty/night/rain shots yet; not needed for the MVP | ✅ enough for the MVP |
| 2 | **Where will production run?** Topology T1 / T2 / T3 ([deployment.md §3](docs/design/deployment.md#3-production-topologies-to-be-chosen)); power and internet at the lot. Needed before Phase 4 | Not decided. Keep the design portable | 🟡 assumed |
| 3 | Spaces per level; marked spaces? One ramp? Separate in/out lanes? | Not decided. Working assumption: ground level has marked spaces in two rows (as in the photo); total count, underground level and ramp layout unknown. Phase 1 uses only the ground-level photo | 🟡 assumed |
| 4 | Camera layout: Option A / B / C ([PLAN §5](PLAN.md#5-key-design-decisions)); existing cameras? | Not decided. Working assumption: Option A, with Camera B over the ground level at roughly the sample photo's position (high, looking down); no existing cameras | 🟡 assumed |
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
| 2026-10-08 | P1.2: config models forbid unknown keys; `reset` only on flow zones; `levels.filling < plenty`; `Settings` reads `deploy/.env` and treats empty values as unset; added `load_lines` and `LotConfig.zone_capacity` (recorded in config.md loader rules) | The guide left these open; strict keys catch YAML typos, and `.env.example` has empty secrets |
| 2026-10-08 | P1.3: the slot editor is served with `python3 -m http.server` (not opened by double-click); slot corners snap to existing corners and a click on a corner starts a new space; added `Shift+D` duplicate down and *Mark labelled*. Its own `eslint.config.js` reuses the frontend's packages; CI's frontend job runs its lint, Prettier check and `node --test` (testing.md §5). `config/slots/cam-ground.json` has 17 ground spaces: 8 in the left row and 9 in the right row, which is cut off at the image edge, so those polygons stop at x = 1931 | Browsers block ES modules on `file://`; without snapping, the first click of the next space selected its neighbour; the sample photo's rows run vertically; ESLint 10 won't lint files outside the config's folder |
| 2026-10-08 | P1.4: the `vision` extra pins **CPU-only** `torch`/`torchvision` from the PyTorch CPU index and adds `pnnx` (needed for NCNN export). `FakeDetector` reads a `{"detections": [...]}` sidecar (format in vision.md §1). The slow test uses Ultralytics' bundled `bus.jpg` (not in the repo) and expects ≥ 1 vehicle, not ≥ 1 car | Default PyPI torch wheels pull ~3 GB of CUDA libraries; NCNN export fails without pnnx; flat drawn cars aren't detected by YOLO, and no public-domain car photo is in the repo |
| 2026-10-08 | P1.4 found that **COCO-pretrained YOLO11 doesn't detect cars seen straight down** (0–1 of 5 on `ground-01.jpg`; yolo11-obb aerial models ≤ 2 of 5). **Superseded the same day by the MVP decision below** (appearance scoring; no input needed) | The detector code works (NCNN output matches PyTorch; detects the bus in `bus.jpg`), so the problem is the viewpoint, not the code |
| 2026-10-08 | P1.5: `score_slots`/`count_in_zones` take an optional `image_size` (the slot file's) to rescale polygons to `frame_size`; in `mask` mode a detection without a usable mask falls back to `box_bottom`; `count_in_zones` returns every zone (0 if empty) and counts a vehicle once per zone. Recorded in vision.md §2 | The spec's signature had no way to know the slot file's size; the detector may return a box without a mask |
| 2026-10-08 | P1.6: `annotate_occupancy` takes `totals` as `{zone: (free, capacity)}` plus optional `inference_ms` and `image_size` (polygon rescaling); the banner reads `Ground: 12 free / 40 \| 143 ms` with `\|` instead of `·`; labels of slots cut off at the image edge are moved inside the frame. Recorded in vision.md §2 | The guide didn't define `totals` or where the time comes from; OpenCV's Hershey fonts draw non-ASCII characters as `?` |
| 2026-10-08 | P1.8: the agent made the ground-truth labels itself from the two sample photos (same scene; 5 cars are clearly visible, so nothing was unsure) instead of waiting; checked against the photo afterwards (correct: G05 G06 G09 G12 G14 taken). `parking evaluate` treats **free** as the positive class, excludes unsure slots from the free counts too, rejects labels naming unknown slots, keys the detection cache on image size/mtime + `conf`/`classes`/`use_masks`, breaks best-threshold ties by free-precision, then count error, then closeness to the configured threshold, and also has `--threshold`, `--imgsz`, `--no-cache`, `--fake-detector`. Labels load through `LabelFile`/`load_labels` in `config.py`. Recorded in vision.md §10 and config.md §4 | The guide left these open; labelling 17 clearly visible spaces needs no input from Iulian |
| 2026-10-08 | P1.7: `analyze_frame` takes an optional `capacities` (zone → capacity) for the totals; the JSON adds `totals` and `timings` to the observation; `parking analyze` also has `--fake-detector` (sidecar `<image>.json`); paths in lot.yaml resolve against the repo root, and CLI tools read `${VAR}` from `deploy/.env.example` < `deploy/.env` < environment (`cli_env`). Recorded in vision.md §2 and config.md loader rules | `lot.yaml` needs `LOT_LAT`/`TZ`/`CAM_RAMP_RTSP_URL` even for offline tools, and the dev checkout has no `deploy/.env`; the guide didn't say where count-zone capacities come from |
| 2026-10-08 | **MVP defined: Phases 0–3** (live free-space count on the phone from the sample photo replayed as a simulated camera). The sample's straight-down view is **not** the final camera. Open questions without answers use the recorded working assumptions; nothing waits on Iulian for the MVP | Owner: "work with what you have", no more questions |
| 2026-10-08 | **Top-down MVP scoring:** `occupancy.method: appearance` (each space scored by how much it differs from empty pavement, vision.md §2.1) for `cam-ground`; the YOLO detector stays the default for angled cameras; the trained per-slot classifier (vision.md §9) replaces the heuristic once the real camera gives enough photos. P1.4 unblocked with this scope | COCO detectors don't see cars from straight above (0–1 of 5); one photo is too little to train a model; marked spaces on uniform pavers make a no-training heuristic workable for the MVP |
| 2026-10-08 | **Build order:** MVP (0–3) → 6 → 7 → 4 → 5 → 8 → 9; PROGRESS.md sections reordered (the loop works top to bottom) | Phases 6–7 need no hardware; 4, 5, 8 need the real cameras / production machines |
| 2026-10-08 | Agent loop: technical and design choices are made by the agent and recorded here, never sent to Iulian as a question; tasks are only blocked for physical things (hardware, installation, accounts, payments) | Owner's requirement |
| 2026-10-08 | `data/IMG_8093.jpeg` (a lot photo uploaded on GitHub, so public) stays; the owner is fine with it. Agents still never commit images | Owner's decision |

## Metrics

| Date | Phase | Metric | Value | Target | Notes |
|------|-------|--------|-------|--------|-------|
| 2026-10-08 | 1 | Slot accuracy (samples) | 70.6% (24/34; free-precision 70.6%, count error 5) | ≥ 97% | dev Pi, `yolo11n-seg` NCNN @ 1280, mask, 0.30 (best of the 0.1–0.6 sweep, all tie); 2 photos of the same scene. 0 cars detected, so every taken space is said free (P1.4 blocker) |
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
- P0.7: developer README (what it is, links, repo layout, prerequisites uv/Node 22/Docker, backend tests, frontend in mock mode, CI, public-repo rules + pre-commit). Followed it on a fresh clone in `/tmp`: pytest (6 passed), ruff, `parking --version`, `npm ci`, lint, format check, vitest (1 passed), build and `npm run dev` at `/parking-app/` all work.
- P0.8: one sample photo provided (`data/samples/ground-01.jpg`, not committed); open questions 2–4 not decided by Iulian, so working assumptions were recorded and Phase 1 proceeds with them.
- P1.1: created git-ignored `data/labels/` and `data/reference/`; copied `ground-01.jpg` to `data/reference/cam-ground.jpg` (the only image, so it's the reference); recorded its conditions (`day`, `dry`; partly full) in git-ignored `data/samples/CONDITIONS.md` for the P1.8 labels file. `ls data/samples` lists the image and `git status` stays clean.
- P1.2: `backend/parking/config.py` (pydantic models for lot.yaml, slot and line files, `${VAR}` interpolation, loader rules, `SlotFile.scaled`, `Settings` for every `.env` variable) + `tests/unit/test_config.py` (38 tests); `config/lot.yaml` loads with the `.env.example` values; pytest (44 passed) and ruff pass.
- P1.3: slot editor `tools/slot-editor/` (`index.html`, ES module `editor.js` with `createEditor`, README; slots / lines / label modes, zoom/pan/pinch, snapping, duplicate right/down, import/export in the config.md §2–4 formats) + `editor.test.js` (10 node tests) in CI. Drew the 17 visible ground spaces on `data/reference/cam-ground.jpg` by clicking in headless Chromium → `config/slots/cam-ground.json`; re-import + export is byte-identical, `load_slots()` loads it (ground capacity 17), new pytest checks committed slot files (45 passed); label and lines modes also exported correctly.
- P1.4 (⏸️ blocked): `parking/vision/detector.py` (`Detection`, `Detector`, `YoloDetector`, `FakeDetector`, class mapping/filter, `export_model`) + `parking models export`; exported `yolo11n-seg` @ 1280 and `yolo11n` @ 640 to NCNN; `test_detector_filter.py` (8 unit + 1 slow, all pass; pytest 54 passed, ruff clean). Done-when check fails: 0 vehicles from `yolo11n-seg` @ 1280 (1 from `yolo11n` @ 640) on `ground-01.jpg`, which shows 5 cars from straight above. Needs Iulian's choice of camera view or model.
- P1.5: `parking/geometry.py` (polygon repair, box bottom, overlap ratio, bottom-centre, point-in) + `parking/vision/occupancy.py` (`score_slots` with an STRtree over footprints, max overlap, mask/box_bottom modes; `count_in_zones`), tested with hand-made detections (`test_occupancy.py` 13, `test_geometry.py` 5). Branch coverage of `occupancy.py` is 100% (pytest-cov added); pytest (72 passed) and ruff pass. P1.4's real-photo detection problem is still open; this task doesn't need the model.
- P1.6: `parking/vision/annotate.py` (`annotate_occupancy`: green free / red-filled taken slots with id + score, yellow detections, top banner with free/capacity and ms; text scales with frame width) + `test_annotate.py` (11 tests). Rendered on `ground-01.jpg` with the 17 slots and 5 hand-made detections (the real detector still misses top-down cars, P1.4) at full size and at 720p; both read clearly when shrunk to phone width (540–590 px). pytest (83 passed) and ruff pass.
- P1.7: `parking/vision/pipeline.py` (`analyze_frame` → `AnalysisResult` with detections, slot results, zone counts, totals, timings; `to_observation()`) + `parking analyze` (config overrides, `--fake-detector`, JSON + annotated PNG). `uv run parking analyze --image data/samples/ground-01.jpg --camera cam-ground` prints `ground: 17 free / 17 (0 taken) in 1.9 s` and writes both files; 0 taken because the COCO model still sees no top-down cars (P1.4). New `test_pipeline.py` (5, 100% branch coverage), CLI tests with the fake detector (6), `cli_env` tests (3); pytest (97 passed incl. slow) and ruff pass.
- P1.8: `parking/vision/evaluate.py` (per-image comparison with unsure excluded, overall + per-condition summary, free-precision/recall, count error, sweep, best threshold, detection cache `out/cache/<image>.<model>.<imgsz>.json`), `LabelFile`/`load_labels`, `highlight_slots` and `parking evaluate`. Labelled `ground-01.jpg` and `ground-02.jpg` by eye (taken G05 G06 G09 G12 G14). `parking evaluate --images data/samples --labels data/labels/cam-ground.json --camera cam-ground --sweep 0.1:0.6:0.05` prints slot accuracy 70.6%, free-precision 70.6%, count error 5.00, best threshold 0.30 (second run: 2 from cache, no model load), and writes the report + mistake PNGs to `out/eval/`. The 5 misses are the top-down cars COCO YOLO doesn't see (P1.4). New `test_evaluate.py` (16) + 10 CLI tests, 100% branch coverage of `evaluate.py`; pytest (123 passed) and ruff pass.
- Defined the MVP (Phases 0–3) and the build order (0–3, 6, 7, 4, 5, 8, 9); P1.4 unblocked with top-down appearance scoring; labels checked; nothing waits on Iulian for the MVP; agents now decide technical choices themselves.
