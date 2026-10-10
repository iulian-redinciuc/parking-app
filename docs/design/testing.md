# Testing

## 1. Layers

| Layer | Location | Tools | Runs in CI? |
|-------|----------|-------|-------------|
| Backend unit | `backend/tests/unit/` | pytest | ✅ |
| Backend integration (API + DB, posting to `/internal/*`) | `backend/tests/integration/` | pytest, httpx `AsyncClient`, pytest-asyncio | ✅ |
| Vision evaluation on **real** images/clips | `data/` (local only) | `parking evaluate`, `parking evaluate-flow` | ❌ (private data). Run on the dev Pi, and again on production hardware (P8.2); record results in PROGRESS.md labelled with the machine |
| Vision smoke test on **synthetic** images | `backend/tests/fixtures/` | pytest (marked `slow`, needs model) | ✅ nightly / manual |
| End-to-end (replay feed → worker → API → SSE) | `backend/tests/e2e/` + `deploy/docker-compose.test.yml` | pytest + docker compose | ✅ on PRs touching backend |
| Frontend unit/component | `frontend/src/**/*.test.tsx` | Vitest, Testing Library | ✅ |
| Slot editor (pure helpers: geometry, ids, file formats, labels) | `tools/slot-editor/editor.test.js` | `node --test` (no dependencies) | ✅ (frontend job) |
| Flow tally (event list + undo, labels CSV read/write) | `tools/flow-tally/tally.test.js` | `node --test` (no dependencies) | ✅ (frontend job) |
| Frontend E2E | `frontend/e2e/` | Playwright (`iPhone 13`, `Pixel 7`) with `VITE_API_BASE=mock` | ✅ |
| Quality | `frontend/lighthouserc.cjs` | Lighthouse CI (`@lhci/cli`, mobile preset, 3 runs): performance ≥ 90, a11y ≥ 90. Lighthouse 12 has no PWA category, so installability is asserted by Playwright (`e2e/pwa.spec.ts`, Chromium DevTools) | ✅ every push |
| Load | `scripts/load/sse.py` | asyncio + httpx, 500 clients | manual, Phase 8 |
| Device checks | — | Real Android + iPhone, [notifications.md §6](notifications.md#6-test-matrix-phase-6) | manual |

## 2. What must have unit tests

| Module | Key cases |
|--------|-----------|
| `config.py` | env interpolation; missing var error; each validation rule; scaling of polygons to frame size |
| `geometry.py` | overlap ratio: full, none, partial; self-intersecting polygon repaired; point-in-polygon at edges |
| `vision/occupancy.py` | with **synthetic `Detection` objects** (no model): mask vs box_bottom; `max` not `sum`; threshold boundary; count mode bottom-centre rule |
| `vision/flow.py` | synthetic tracks: a→b = in; b→a = out; touches a then reverses = nothing; jitter on a line = nothing; one event per track; short tracks ignored; `in_direction` flip |
| `vision/health.py` | black, frozen (N frames), blurry thresholds on generated numpy frames |
| `vision/shift.py` | translate a synthetic textured image by 0/5/20 px → detection matches |
| `core/smoothing.py` | first reading sets state; k-1 contrary readings don't flip; k do; alternating noise never flips |
| `core/flow_counter.py` | clamp at 0/capacity; duplicate `event_id` ignored; correction resets confidence; restore from DB |
| `core/drift.py` | drift per day from the first note's error, sign ignored; target edge; too short = incomplete; notes CSV errors; `drift-note` / `drift-report` CLI (API read faked) |
| `core/fusion.py` | level thresholds; total = sum; stale after timeout (fake clock); trend thresholds; confidence per method |
| `push/rules.py` | each send/skip rule in [notifications.md §4–5](notifications.md#4-tier-2-im-on-my-way-server-rules-pushrulespy) with a fake clock; quiet hours across midnight; timezones |
| API routes | status 503 before data; SSE sends current state first then changes; admin auth required; CORS headers; rate limits |

Use `core/clock.py` (`Clock` protocol with `now()` returning aware UTC; `SystemClock` in production, `FakeClock` with `advance()`/`set()` in tests) everywhere time matters, so tests never `sleep`.

## 3. Fixtures (public repo safe)

- **Synthetic lot images**: `tests/fixtures/make_synthetic.py` draws a grey "asphalt" image with white slot lines and pastes **public-domain car images** (or simple rendered car shapes) into chosen slots. This is good enough to smoke-test the pipeline end to end with the real model, though not for accuracy.
- JSON fixtures: slot files, line files, observation/flow payloads. API response examples (`LotStatus` live and stale, `LotInfo`, error bodies) live in `backend/tests/fixtures/api/`: backend tests check them against the Pydantic models and the real responses, and the frontend keeps **identical copies** in `frontend/src/api/__fixtures__/` (`fixtures.test.ts` fails if they drift, and checks them against the TS types). Change both together.
- **Real images never go into `tests/`.**

## 4. Evaluation datasets (local, git-ignored)

| Set | Contents | Built in |
|-----|----------|----------|
| `data/samples/` + `data/labels/cam-ground.json` | Your Phase 1 sample images | P1.8 |
| `data/validation/cam-ground/` + labels | ~200 frames from the live camera across conditions (`parking validation pick` / `check`) | P4.8 |
| `data/recordings/` + `data/labels/<clip>.csv` | 3 × 1-hour ramp clips with hand tallies | P5.9 |

Every evaluation run writes `out/eval/<set>-<YYYYMMDD-HHMM>.json`. Copy the headline numbers into PROGRESS.md → Metrics.

## 5. CI (`.github/workflows/ci.yml`)

Jobs:
1. **backend**: `uv sync --frozen`, `ruff check`, `ruff format --check`, `pytest -m "not slow"`.
2. **frontend**: `npm ci`, `npm run lint`, `npm run format:check`, `npm run test -- --run`, `npm run build`, then the slot editor checks from the repo root (`eslint tools/slot-editor` with its own config, `prettier --check` with the frontend's config, `node --test tools/slot-editor/editor.test.js`) and the same three for `tools/flow-tally`, `npx playwright install --with-deps chromium webkit`, `npm run e2e` (both projects; in CI 1 retry, `forbidOnly`, `test-results/` uploaded on failure).
3. **secrets**: gitleaks (`gitleaks/gitleaks-action@v2`, full history checkout).
4. **e2e** (only if `backend/**` or `deploy/**` changed, via `dorny/paths-filter`): `PARKING_E2E=1 uv run pytest tests/e2e`. The test drives `deploy/docker-compose.test.yml` (project `parking-e2e`) itself: `up -d --build --wait` with a temp replay folder holding `frame-01`, waits on `/api/stream` for its live free count, swaps in `frame-02` (sidecar written first, both via rename) and removes `frame-01`, expects the new count within `3 × interval + 5 s` (`interval 2`, `consistent_readings 3` → 11 s; ~5 s on the dev Pi), then `down -v --rmi all`. Without `PARKING_E2E=1` the test is skipped, so job 1 doesn't need Docker. Locally: `cd backend && PARKING_E2E=1 uv run pytest tests/e2e -v`.
5. **images** (only if `backend/**`, `.dockerignore` or `ci.yml` changed): builds both targets of `backend/Dockerfile` on an x86 runner (`ubuntu-latest`) and on an ARM runner (`ubuntu-24.04-arm`), without pushing, and runs `parking --version` in each image. This catches "works on the Pi, breaks on x86" (and the reverse) early. Pushing images happens only on release tags (`release.yml`, [deployment.md §8](deployment.md#8-releases-and-updating)), which also pulls and starts the pushed images on both CPU types. **web-image** (only if `frontend/**`, `tools/slot-editor/**`, `deploy/Caddyfile`, `.dockerignore` or `ci.yml` changed): builds `frontend/Dockerfile` on both runners, then `caddy validate` and a check that the frontend is in the image.
6. **lighthouse**: `npm ci`, `npm run build -- --mode development` (mock data), `npm run lhci` (`lhci autorun`: serves `npm run preview`, asserts performance and accessibility ≥ 0.9 on `/parking-app/`); reports kept as the `lighthouse-reports` artifact, not uploaded anywhere public. Locally: `CHROME_PATH=/usr/bin/chromium npm run lhci` after the build.

The vision image is large; the e2e job uses a `FakeDetector` (env `PARKING_FAKE_DETECTOR=1`) that reads expected detections from a JSON sidecar next to each replay image, so CI doesn't need PyTorch. Both e2e services therefore run the small **api** image (the worker code and OpenCV are core dependencies). Fixtures: `backend/tests/fixtures/replay/` = `config/lot.yaml` (camera `cam-e2e`, detector method, `box_bottom`, threshold 0.20) + `config/slots/cam-e2e.json` (4 slots, 320 × 240) + `frames/frame-0{1,2}.png` (synthetic grid images that pass the health checks) with sidecars (1 and 3 spaces taken).

## 6. Manual release checklist (from Phase 3 on)
- [ ] Live screen on a real Android and iPhone over mobile data (not Wi-Fi).
- [ ] Kill the API → banner appears within 30 s → restart → recovers by itself.
- [ ] Airplane mode → offline banner → back online → recovers.
- [ ] Lighthouse mobile: PWA installable, a11y ≥ 90, performance ≥ 90.

## 7. Soak test (P4.11, on the vision host)

`backend/scripts/soak.py` (standard library only; runs with the host's `python3`, needs `docker` and `/sys/class/thermal`, which the containers can't see):

- `python3 backend/scripts/soak.py sample [--out out/soak/soak.jsonl] [--interval 60] [--api http://127.0.0.1:8000] [--duration S] [--once]` appends one JSON line per sample: host `temp_c`, `throttled` (`vcgencmd get_throttled`, if present), `load1`, `mem_avail_mb`; per container (`parking-api`, `parking-vision-occupancy`, `parking-tunnel`) `status`, `restarts`, `started_at`, `mem_mb`, `cpu_pct` (`docker inspect` / `docker stats`); the API's `/healthz` (camera states, ingest counters) and each zone's `stale`/`free` from `/api/status`; and per worker how many `camera back after …` lines it logged since the previous sample (`reconnects`).
- `python3 backend/scripts/soak.py report FILE [--days 7] [--interval 60] [--mem-growth-mb 25] [--mem-growth-pct 10] [--json]` prints temperature (median/p95/max, throttle flags), reconnects, per-container memory, every outage (container not running, API unreachable, camera not `ok`, zone stale, or no samples for > max(5 × interval, 5 min)) with start/end/minutes, and the verdict. Only items that were fine at least once count: a camera that never reported (`unknown`, e.g. Camera A not installed), its always-stale zone and a container that was never running (`tunnel` without `--profile public`) are listed as "never ok, not counted" (an API that never answered fails the run). Exit 0 = **passed**: the samples span `--days`, nothing is still down at the last sample (no unrecovered outage), and no container's memory grew (median of the last 24 h vs the median of the first 24 h after a 1 h warm-up; growth allowed up to max(25 MB, 10%)). The least-squares slope (MB/day, runs of ≥ 1 day) is printed for information.

The sample file lives in the git-ignored `out/`; it has no images, URLs or secrets.
