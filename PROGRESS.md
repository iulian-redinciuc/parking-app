# Parking App — Progress

> Overview: [PLAN.md](PLAN.md) · Docs index: [docs/README.md](docs/README.md)
> Task IDs (e.g. **P1.5**) link to the step-by-step guides. Tick a box when the task's "Done when" check passes.
>
> Status: ⬜ not started · 🟡 in progress · ✅ done · ⏸️ blocked · ⏭️ skipped
>
> The [agent loop](tools/agent-loop/README.md) works through the unticked tasks in order. Nothing is blocked: tasks that need the real cameras or production are closed with a PoC check on images, and their real-world checks are in [docs/go-live.md](docs/go-live.md).

**Agent loop:** finished: every task is done (2026-10-16 16:54:07)
**Status:** the **PoC is complete**: every task is done and checked on images in the dev environment. Going live (real camera, production, real users) is a separate track: [docs/go-live.md](docs/go-live.md).
**Last updated:** 2026-10-17

**Try it (on your phone):** open https://iulian-redinciuc.github.io/parking-app/ (mobile data is fine). It shows the dev Pi's replay feed through a Cloudflare quick tunnel (the API address changes whenever the tunnel restarts; `deploy/scripts/dev-public.sh url` prints the current one), refreshed about every 10 s (header *Updating*: quick tunnels don't carry the live stream). The address changes whenever the tunnel restarts: **after a reboot of the dev Pi re-run `deploy/scripts/dev-public.sh up`** (about a minute; it rebuilds the preview with the new address), then reload the app. `deploy/scripts/dev-public.sh down` closes the tunnel and puts the preview back on mock data.

## Waiting on Iulian

- **Nothing blocks the PoC.** Images (the real sample photo, the simulated feed, the generated images in `samples/generated/`) are the cameras for now.
- **Helps, any time:** more images from the same view (upload to `samples/generated/` on GitHub, or real photos in the chat / `data/samples/` on the dev Pi).
- **When you want to go live** (real camera, production, real users): the checklist and what it needs from you (permission, hardware, a domain, accounts) is [docs/go-live.md](docs/go-live.md).

## Overview

| Phase | Name | Tasks | Status | Started | Finished |
|-------|------|-------|--------|---------|----------|
| 0 | [Foundations](docs/phases/phase-0-foundations.md) (MVP) | 8 / 8 | ✅ | 2026-10-07 | 2026-10-08 |
| 1 | [Still-image PoC](docs/phases/phase-1-still-image.md) (MVP) | 11 / 11 | ✅ | 2026-10-08 | 2026-10-08 |
| 2 | [Backend + simulated feed](docs/phases/phase-2-backend.md) (MVP) | 13 / 13 | ✅ | 2026-10-08 | 2026-10-11 |
| 3 | [Mobile web app](docs/phases/phase-3-frontend.md) (MVP) | 10 / 10 | ✅ | 2026-10-09 | 2026-10-10 |
| 4 | [Live occupancy camera](docs/phases/phase-4-occupancy-camera.md) | 12 / 12 | ✅ | 2026-10-09 | 2026-10-15 |
| 5 | [Entry/exit camera](docs/phases/phase-5-flow-camera.md) | 11 / 11 | ✅ | 2026-10-09 | 2026-10-16 |
| 6 | [Notifications](docs/phases/phase-6-notifications.md) | 8 / 8 | ✅ | 2026-10-09 | 2026-10-15 |
| 7 | [Admin + stats](docs/phases/phase-7-admin-stats.md) | 8 / 8 | ✅ | 2026-10-09 | 2026-10-09 |
| 8 | [Production deployment + hardening](docs/phases/phase-8-hardening.md) | 13 / 13 | ✅ | 2026-10-10 | 2026-10-16 |
| 9 | [Extras](docs/phases/phase-9-extras.md) | 6 / 6 | ✅ | 2026-10-10 | 2026-10-16 |

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

## Phase 1: Still-image proof of concept ✅
- [x] **P1.1** Sample data layout
- [x] **P1.2** Config loader
- [x] **P1.3** Slot editor (slots / lines / label modes)
- [x] **P1.4** Detector module + model export, plus top-down appearance scoring for the MVP (detector part done; the sample is shot straight down, see the guide)
- [x] **P1.5** Geometry + occupancy scoring
- [x] **P1.6** Annotated output image
- [x] **P1.7** `parking analyze`
- [x] **P1.8** Ground-truth labels + `parking evaluate`
- [x] **P1.9** `parking bootstrap-slots`
- [x] **P1.10** Benchmark on the dev Pi
- [x] **P1.11** Tune and decide

## Phase 2: Backend + simulated live feed ✅
- [x] **P2.1** Message models + worker API client (with flow-event outbox)
- [x] **P2.2** Frame sources + health checks
- [x] **P2.3** Occupancy worker
- [x] **P2.4** Smoothing
- [x] **P2.5** State store + fusion
- [x] **P2.6** Database (SQLModel + Alembic)
- [x] **P2.7** Internal ingest endpoints
- [x] **P2.8** SSE broadcaster + `/api/stream`
- [x] **P2.9** REST endpoints, CORS, errors
- [x] **P2.10** Docker images + Compose
- [x] **P2.11** End-to-end test
- [x] **P2.12** Simulated camera feed from one photo (`parking simulate-feed`: cars arriving and leaving)
- [x] **P2.13** Generated images (`samples/generated/`) as the ground camera: check, label, evaluate, add to the replay feed (all 12 usable and in the feed; `parking build-replay`)

## Phase 3: Mobile web app + public access ✅
- [x] **P3.1** App shell, routing, theme
- [x] **P3.2** API types, client, mock
- [x] **P3.3** Live connection manager + `useLiveStatus`
- [x] **P3.4** Live screen components
- [x] **P3.5** Banners and edge states
- [x] **P3.6** i18n
- [x] **P3.7** PWA
- [x] **P3.8** Deploy the preview to GitHub Pages with Actions
- [x] **P3.9** Dev API over HTTPS for phone testing (Cloudflare quick tunnel: no account or domain needed)
- [x] **P3.10** Tests + quality gates (real-phone checklist item waits on P3.9)

## Phase 6: Notifications ✅
- [x] **P6.1** VAPID keys + push sender
- [x] **P6.2** Subscription storage + endpoints
- [x] **P6.3** Service worker push handling
- [x] **P6.4** Alerts screen + permission flow
- [x] **P6.5** Tier 1: proximity while open
- [x] **P6.6** Tier 2: "I'm on my way"
- [x] **P6.7** Tier 3: schedules, quiet hours, almost-full
- [x] **P6.8** Device test matrix (PoC check on emulated devices, live pushes; real phones in docs/go-live.md)

## Phase 7: Admin tools, history, stats ✅
- [x] **P7.1** Admin login
- [x] **P7.2** Camera health page + snapshots
- [x] **P7.3** Slot/line editor in admin
- [x] **P7.4** Corrections UI + audit log
- [x] **P7.5** Rollups + retention jobs
- [x] **P7.6** History + forecast API
- [x] **P7.7** Stats screen
- [x] **P7.8** Admin alerts

## Phase 4: Live occupancy camera ✅
- [x] **P4.1** Choose the production layout, the camera and the vision host (PoC check on images; the live check goes to docs/go-live.md) (moved to go-live)
- [x] **P4.2** Install and network (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P4.3** Snapshot + RTSP sources (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P4.4** Health tuning on the real camera (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P4.5** Calibrate slots on the real view (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P4.6** Camera shift detection
- [x] **P4.7** Debug frame capture
- [x] **P4.8** Validation set (~200 labelled frames) (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P4.9** Evaluate and tune (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P4.10** Per-slot classifier (only if needed) (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P4.11** 7-day soak test (on the vision host) (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P4.12** Camera plugged into the vision host (`device:` / `picamera:` source for the standalone box) — built and tested without hardware; the first run on a real camera is still to do (no camera on the dev Pi)

## Phase 5: Entry/exit camera + combining levels ✅
- [x] **P5.1** Mount Camera A, draw lines (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P5.2** Low-latency RTSP + video source + `parking record` (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P5.3** Motion gate (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P5.4** Tracker integration (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P5.5** Two-line counter
- [x] **P5.6** Flow worker (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P5.7** FlowCounter in the API + corrections
- [x] **P5.8** Fusion update + confidence
- [x] **P5.9** Test clips, tally tool, `evaluate-flow` (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P5.10** Performance on the vision host + accelerator decision (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P5.11** Live week drift test (PoC check on images; the live check goes to docs/go-live.md)

## Phase 8: Production deployment and hardening ✅
- [x] **P8.1** Release pipeline (multi-arch images to GHCR)
- [x] **P8.2** Provision production machines + re-measure on production hardware (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.3** Production public entry + frontend hosting (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.4** Compose hardening (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.5** Watchdog for stuck workers
- [x] **P8.6** Backups + restore drill (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.7** Monitoring + external uptime check (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.8** Security review (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.9** Privacy deliverables (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.10** Load test (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.11** Power/network resilience (PoC check on images; the live check goes to docs/go-live.md)
- [x] **P8.12** Runbook + README
- [x] **P8.13** 7-day staging run + go-live (PoC check on images; the live check goes to docs/go-live.md)

## Phase 9: Optional extras ✅
- [x] **P9.1** Ground-level slot map
- [x] **P9.2** Special spaces (accessible, EV)
- [x] **P9.3** Barrier / induction-loop integration (built and tested against replayed contacts and the dev Pi's own GPIO pins; not yet run on a real barrier controller, none exists)
- [x] **P9.4** Fine-tuned detector / licence swap (PoC check on P4.8's PoC validation frames: fine-tuned YOLO11n 98.2% vs appearance 99.1%, appearance stays; training on the real frames is in docs/go-live.md)
- [x] **P9.5** Multiple lots (one API, database and app for several car parks; checked with two simulated lots on the dev Pi, no real second location)
- [x] **P9.6** Smarter forecast (a model on top of the 8-week median, used only while the nightly check says it beats the median; checked on synthetic history, there is no real history yet)

---

## Open questions

**Nothing blocks the PoC.** The six decisions that only Iulian can make before going live (permission, users, layout and budget, underground level, commercial use, domain) are in [docs/go-live.md §3](docs/go-live.md#3-decisions-only-you-can-make), each with the default used until then.

Settled with working assumptions (change any time):
- **Sample images:** one real photo (`data/samples/ground-01.jpg`, plus a full-resolution second shot) and 12 generated images in `samples/generated/`; real frames come with the camera.
- **Lot layout:** ground level with two rows of marked spaces (17 visible in the sample view); underground size and ramp unknown (placeholder capacity 60).
- **Camera:** a standalone box at the window where the sample photo was taken (Pi + its own camera + mobile data, hardware.md §4.6); no access to the existing cameras or the building's network.
- **Production:** T2 (box at the lot + a small cloud server); no domain needed (`sslip.io` address).
- **Languages:** English (another language is one translation file).
- **Lot GPS position, notification radius:** set in `.env` when going live.

## History

The decision log, the measured metrics and the per-task session log are in [docs/history.md](docs/history.md).
