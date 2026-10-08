# Documentation

Start with [../PLAN.md](../PLAN.md) for the big picture. Track status in [../PROGRESS.md](../PROGRESS.md).

## How the docs are organised

| Folder | What's in it | Use it when |
|--------|--------------|-------------|
| `design/` | **Specs**: the single source of truth for contracts, formats, algorithms and decisions | You need to know *what exactly* to build (payload shapes, file formats, thresholds) |
| `phases/` | **Step-by-step guides**, one per phase, split into numbered tasks | You're implementing. Pick the next unticked task in PROGRESS.md and open its phase file |

Phase guides **link to** the design specs instead of repeating them. If a spec and a phase guide disagree, the spec wins. Fix the guide.

## Design specs

| File | Covers |
|------|--------|
| [design/architecture.md](design/architecture.md) | Components, data flow, backend/frontend module map, ground rules |
| [design/config.md](design/config.md) | `lot.yaml`, slot files, line files, labels, `.env` variables |
| [design/vision.md](design/vision.md) | Detection, occupancy, smoothing, entry/exit counting, health checks, evaluation |
| [design/api.md](design/api.md) | REST endpoints, SSE stream, internal worker endpoints, auth |
| [design/data-model.md](design/data-model.md) | SQLite tables, retention, aggregation, restart behaviour |
| [design/frontend.md](design/frontend.md) | Screens, components, live-update hook, PWA, i18n, styling |
| [design/notifications.md](design/notifications.md) | Push, proximity tiers, iPhone specifics |
| [design/deployment.md](design/deployment.md) | Dev Pi isolation rules, production topologies, multi-arch images, Compose, public entry, frontend hosting, backups, releases |
| [design/hardware.md](design/hardware.md) | Cameras, mounting, network, compute hardware (dev Pi, production options), sample-image guidelines |
| [design/security-privacy.md](design/security-privacy.md) | Threats, controls, secrets, public-repo rules, GDPR checklist |
| [design/testing.md](design/testing.md) | Test layers, fixtures, CI, evaluation datasets, device matrix |

## Phase guides

| Phase | Guide | Needs camera hardware? |
|-------|-------|-----------------------|
| 0 | [phases/phase-0-foundations.md](phases/phase-0-foundations.md) | No |
| 1 | [phases/phase-1-still-image.md](phases/phase-1-still-image.md) | No (one image) |
| 2 | [phases/phase-2-backend.md](phases/phase-2-backend.md) | No |
| 3 | [phases/phase-3-frontend.md](phases/phase-3-frontend.md) | No |
| 4 | [phases/phase-4-occupancy-camera.md](phases/phase-4-occupancy-camera.md) | Yes: Camera B + vision host |
| 5 | [phases/phase-5-flow-camera.md](phases/phase-5-flow-camera.md) | Yes: Camera A |
| 6 | [phases/phase-6-notifications.md](phases/phase-6-notifications.md) | No |
| 7 | [phases/phase-7-admin-stats.md](phases/phase-7-admin-stats.md) | No |
| 8 | [phases/phase-8-hardening.md](phases/phase-8-hardening.md): production deployment + hardening | Production machines |
| 9 | [phases/phase-9-extras.md](phases/phase-9-extras.md) | Depends |

## Conventions

### Task IDs
Tasks are numbered `P<phase>.<n>`, e.g. **P1.5**. The same ID appears in the phase guide, PROGRESS.md, branch names and commit messages, so everything can be traced.

Every task in a phase guide has:
- **Files**: what gets created or changed
- **Steps**: what to do, in order
- **Done when**: a check anyone can run to confirm it's finished

### Definition of done (every task)
1. Code is written and passes `ruff` / `eslint`.
2. Tests exist for the logic and pass locally and in CI.
3. Any contract change (payload, config, endpoint) is reflected in the `design/` spec **in the same commit**.
4. **No secrets or real camera images** in the diff (see [design/security-privacy.md](design/security-privacy.md#3-public-repo-rules)).
5. PROGRESS.md is ticked and the session log updated.

### Git workflow
- `main` is always deployable. Pushing to `main` redeploys the GitHub Pages **preview**. Production is deployed only from release tags (`v*`).
- Commit messages start with the task ID: `P1.5: slot overlap scoring with mask/box modes`.
- **Agent loop** ([tools/agent-loop](../tools/agent-loop/README.md)): one commit per task, pushed straight to `main`. CI runs on every push. A red CI run is fixed by the next session before it starts new work.
- **Manual work:** a branch per task (`p1.5-occupancy`) and a PR to `main` is fine too. Merge when CI is green.

### Glossary
| Term | Meaning |
|------|---------|
| **Space / slot** | One parking space. In code it's `slot` |
| **Zone** | A part of the lot with its own count: `ground`, `underground` |
| **Occupancy camera** | A camera that sees parked cars (Camera B) |
| **Flow camera** | A camera that counts cars crossing a line in or out (Camera A) |
| **Observation** | One analysed frame's result, sent by a worker |
| **Stale** | No fresh data from a camera for longer than the stale timeout |
| **Confidence** | 0–1 estimate of how trustworthy a zone's number is |
| **SSE** | Server-Sent Events: the server keeps an HTTP connection open and pushes updates |
| **PWA** | Progressive Web App: a website that can be installed to the home screen and receive push |
| **VAPID** | The key pair that identifies our server to browser push services |
| **NCNN** | A fast neural-network runtime for ARM CPUs; used on the dev Pi (production may use another runtime) |
| **Dev Pi** | Iulian's Raspberry Pi 5, used **only** for development and testing |
| **Production** | The real deployment, on machines not decided yet ([design/deployment.md §3](design/deployment.md#3-production-topologies-to-be-chosen)) |
| **Vision host** | The machine that runs the camera workers in production (usually at the lot) |
| **Preview** | The GitHub Pages copy of the frontend used for testing |
