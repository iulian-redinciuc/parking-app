# Parking App — Progress

> Overview: [PLAN.md](PLAN.md) · Docs index: [docs/README.md](docs/README.md)
> Task IDs (e.g. **P1.5**) link to the step-by-step guides. Tick a box when the task's "Done when" check passes.
>
> Status: ⬜ not started · 🟡 in progress · ✅ done · ⏸️ blocked · ⏭️ skipped

**Current focus:** Phase 0 → P0.1–P0.7 (skeleton), waiting on **P0.8** (sample image + answers).
**Last updated:** 2026-10-07

## Overview

| Phase | Name | Tasks | Status | Started | Finished |
|-------|------|-------|--------|---------|----------|
| 0 | [Foundations](docs/phases/phase-0-foundations.md) | 0 / 8 | 🟡 | 2026-10-07 | |
| 1 | [Still-image PoC](docs/phases/phase-1-still-image.md) | 0 / 11 | ⬜ | | |
| 2 | [Backend + simulated feed](docs/phases/phase-2-backend.md) | 0 / 11 | ⬜ | | |
| 3 | [Mobile web app](docs/phases/phase-3-frontend.md) | 0 / 10 | ⬜ | | |
| 4 | [Live occupancy camera](docs/phases/phase-4-occupancy-camera.md) | 0 / 11 | ⬜ | | |
| 5 | [Entry/exit camera](docs/phases/phase-5-flow-camera.md) | 0 / 11 | ⬜ | | |
| 6 | [Notifications](docs/phases/phase-6-notifications.md) | 0 / 9 | ⬜ | | |
| 7 | [Admin + stats](docs/phases/phase-7-admin-stats.md) | 0 / 8 | ⬜ | | |
| 8 | [Hardening](docs/phases/phase-8-hardening.md) | 0 / 9 | ⬜ | | |
| 9 | [Extras](docs/phases/phase-9-extras.md) | 0 / 7 | ⬜ | | |

---

## Phase 0: Foundations 🟡
Planning done: repo created, GitHub Pages live, PLAN.md + docs written.
- [ ] **P0.1** Repo layout and `.gitignore`
- [ ] **P0.2** Backend project (uv, Python 3.12, Typer CLI, ruff, pytest)
- [ ] **P0.3** Frontend scaffold (Vite, React, TS, Tailwind, Vitest)
- [ ] **P0.4** Config templates (`lot.example.yaml`, `lot.yaml`, `.env.example`)
- [ ] **P0.5** CI workflow
- [ ] **P0.6** Secret protection (GitHub scanning, gitleaks pre-commit, Dependabot)
- [ ] **P0.7** README for developers
- [ ] **P0.8** Inputs from Iulian: sample image(s) + open questions 2–4

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
- [ ] **P1.10** Benchmark on the Pi 5
- [ ] **P1.11** Tune and decide

## Phase 2: Backend + simulated live feed ⬜
- [ ] **P2.1** Messaging models + MQTT helpers
- [ ] **P2.2** Frame sources + health checks
- [ ] **P2.3** Occupancy worker
- [ ] **P2.4** Smoothing
- [ ] **P2.5** State store + fusion
- [ ] **P2.6** Database (SQLModel + Alembic)
- [ ] **P2.7** MQTT consumer in the API
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
- [ ] **P3.8** Deploy to Pages with Actions
- [ ] **P3.9** API over HTTPS (tunnel)
- [ ] **P3.10** Tests + quality gates

## Phase 4: Live occupancy camera ⬜
- [ ] **P4.1** Choose and buy the camera
- [ ] **P4.2** Install and network
- [ ] **P4.3** Snapshot + RTSP sources
- [ ] **P4.4** Health tuning on the real camera
- [ ] **P4.5** Calibrate slots on the real view
- [ ] **P4.6** Camera shift detection
- [ ] **P4.7** Debug frame capture
- [ ] **P4.8** Validation set (~200 labelled frames)
- [ ] **P4.9** Evaluate and tune
- [ ] **P4.10** Per-slot classifier (only if needed)
- [ ] **P4.11** 7-day soak test

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
- [ ] **P5.10** Performance + AI HAT+ decision
- [ ] **P5.11** Live week drift test

## Phase 6: Notifications ⬜
- [ ] **P6.1** VAPID keys + push sender
- [ ] **P6.2** Subscription storage + endpoints
- [ ] **P6.3** Service worker push handling
- [ ] **P6.4** Alerts screen + permission flow
- [ ] **P6.5** Tier 1: proximity while open
- [ ] **P6.6** Tier 2: "I'm on my way"
- [ ] **P6.7** Tier 3: schedules, quiet hours, almost-full
- [ ] **P6.8** Tier 4: Home Assistant
- [ ] **P6.9** Device test matrix

## Phase 7: Admin tools, history, stats ⬜
- [ ] **P7.1** Admin login
- [ ] **P7.2** Camera health page + snapshots
- [ ] **P7.3** Slot/line editor in admin
- [ ] **P7.4** Corrections UI + audit log
- [ ] **P7.5** Rollups + retention jobs
- [ ] **P7.6** History + forecast API
- [ ] **P7.7** Stats screen
- [ ] **P7.8** Admin alerts

## Phase 8: Hardening for production ⬜
- [ ] **P8.1** Compose hardening
- [ ] **P8.2** Watchdog for stuck workers
- [ ] **P8.3** Backups + restore drill
- [ ] **P8.4** Monitoring + external uptime check
- [ ] **P8.5** Security review
- [ ] **P8.6** Privacy deliverables
- [ ] **P8.7** Load test
- [ ] **P8.8** Power/network resilience
- [ ] **P8.9** Runbook + README

## Phase 9: Optional extras ⬜
- [ ] **P9.1** Native app with background geofencing
- [ ] **P9.2** Ground-level slot map
- [ ] **P9.3** Special spaces (accessible, EV)
- [ ] **P9.4** Barrier / induction-loop integration
- [ ] **P9.5** Fine-tuned detector / licence swap
- [ ] **P9.6** Multiple lots
- [ ] **P9.7** Smarter forecast

---

## Open questions

| # | Question | Answer | Status |
|---|----------|--------|--------|
| 1 | Sample image(s): normal, full, empty, night ([what to send](docs/design/hardware.md#1-sample-images-for-phase-1-what-to-send)) | | ⬜ waiting |
| 2 | Where is the lot relative to the Pi? Power/internet there? | | ⬜ |
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
| 2026-10-07 | Stack: Python/FastAPI + YOLO11 (NCNN) + MQTT + SQLite on the Pi 5; Vite/React/TS PWA on Pages; SSE; Web Push | [PLAN.md §4](PLAN.md#4-stack-summary) |
| 2026-10-07 | Still image → simulated feed → real cameras | De-risks vision first; Phases 1–3 need no hardware |
| 2026-10-07 | Notification tiers instead of a promised background geofence | Browsers can't track location in the background |
| 2026-10-07 | A **dedicated** Mosquitto for the parking stack; an optional Home Assistant broker only receives status | Only authenticated workers may write counts |
| 2026-10-07 | Admin auth with bearer tokens, not cookies | Frontend and API are different sites; third-party cookies are blocked (Safari) and would need CSRF protection |
| 2026-10-07 | HashRouter in the frontend | GitHub Pages has no SPA fallback |
| 2026-10-07 | Python pinned to 3.12 (uv) instead of the host's 3.13 | ARM64 ML wheels lag behind new Python versions |

## Metrics

| Date | Phase | Metric | Value | Target | Notes |
|------|-------|--------|-------|--------|-------|
| | 1 | Slot accuracy (samples) | | ≥ 97% | |
| | 1 | Inference time per image (Pi 5, chosen settings) | | < 2 s | |
| | 4 | Slot accuracy (validation set) | | ≥ 97% | |
| | 4 | Count error ≤ 1 (% of frames) | | ≥ 95% | |
| | 5 | Flow event accuracy (3 clips) | | ≥ 98% | |
| | 5 | Flow drift per day | | ≤ 2 cars | |
| | 5 | Flow fps while active | | ≥ 8 | |
| | 8 | SSE p95 delivery (500 clients) | | < 2 s | |

## Session log

### 2026-10-07
- Created the repo, enabled GitHub Pages, made the repo public (free-plan requirement).
- Checked the hardware: Raspberry Pi 5 8 GB, Docker, Node 22, Python 3.13.
- Wrote PLAN.md, then split the details into `docs/design/` (11 specs) and `docs/phases/` (10 guides, 95 tasks).
- **Next:** P0.1–P0.7 skeleton; get P0.8 inputs (sample image + answers) to start Phase 1.
