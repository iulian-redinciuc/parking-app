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
2. **frontend**: `npm ci`, `npm run lint`, `npm run format:check`, `npm run test -- --run`, `npm run build`, then the slot editor checks from the repo root (`eslint tools/slot-editor` with its own config, `prettier --check` with the frontend's config, `node --test tools/slot-editor/editor.test.js`), `npx playwright install --with-deps chromium webkit`, `npm run e2e` (both projects; in CI 1 retry, `forbidOnly`, `test-results/` uploaded on failure).
3. **secrets**: gitleaks (`gitleaks/gitleaks-action@v2`, full history checkout).
4. **e2e** (only if `backend/**` or `deploy/**` changed, via `dorny/paths-filter`): `PARKING_E2E=1 uv run pytest tests/e2e`. The test drives `deploy/docker-compose.test.yml` (project `parking-e2e`) itself: `up -d --build --wait` with a temp replay folder holding `frame-01`, waits on `/api/stream` for its live free count, swaps in `frame-02` (sidecar written first, both via rename) and removes `frame-01`, expects the new count within `3 × interval + 5 s` (`interval 2`, `consistent_readings 3` → 11 s; ~5 s on the dev Pi), then `down -v --rmi all`. Without `PARKING_E2E=1` the test is skipped, so job 1 doesn't need Docker. Locally: `cd backend && PARKING_E2E=1 uv run pytest tests/e2e -v`.
5. **images** (later task; only if `backend/**` changed): `docker buildx build --platform linux/amd64,linux/arm64` for both targets, without pushing. This catches "works on the Pi, breaks on x86" (and the reverse) early. Pushing images happens only on release tags ([deployment.md §8](deployment.md#8-releases-and-updating)).
6. **lighthouse**: `npm ci`, `npm run build -- --mode development` (mock data), `npm run lhci` (`lhci autorun`: serves `npm run preview`, asserts performance and accessibility ≥ 0.9 on `/parking-app/`); reports kept as the `lighthouse-reports` artifact, not uploaded anywhere public. Locally: `CHROME_PATH=/usr/bin/chromium npm run lhci` after the build.

The vision image is large; the e2e job uses a `FakeDetector` (env `PARKING_FAKE_DETECTOR=1`) that reads expected detections from a JSON sidecar next to each replay image, so CI doesn't need PyTorch. Both e2e services therefore run the small **api** image (the worker code and OpenCV are core dependencies). Fixtures: `backend/tests/fixtures/replay/` = `config/lot.yaml` (camera `cam-e2e`, detector method, `box_bottom`, threshold 0.20) + `config/slots/cam-e2e.json` (4 slots, 320 × 240) + `frames/frame-0{1,2}.png` (synthetic grid images that pass the health checks) with sidecars (1 and 3 spaces taken).

## 6. Manual release checklist (from Phase 3 on)
- [ ] Live screen on a real Android and iPhone over mobile data (not Wi-Fi).
- [ ] Kill the API → banner appears within 30 s → restart → recovers by itself.
- [ ] Airplane mode → offline banner → back online → recovers.
- [ ] Lighthouse mobile: PWA installable, a11y ≥ 90, performance ≥ 90.
