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
| Frontend E2E | `frontend/e2e/` | Playwright (`iPhone 13`, `Pixel 7`) with `VITE_API_BASE=mock` | ✅ |
| Quality | — | Lighthouse CI (PWA, a11y ≥ 90) | ✅ on frontend PRs |
| Load | `scripts/load/sse.py` | asyncio + httpx, 500 clients | manual, Phase 8 |
| Device checks | — | Real Android + iPhone, [notifications.md §8](notifications.md#7-test-matrix-phase-6) | manual |

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

Use `core/clock.py` (`Clock` protocol with `now()`) everywhere time matters, so tests never `sleep`.

## 3. Fixtures (public repo safe)

- **Synthetic lot images**: `tests/fixtures/make_synthetic.py` draws a grey "asphalt" image with white slot lines and pastes **public-domain car images** (or simple rendered car shapes) into chosen slots. This is good enough to smoke-test the pipeline end to end with the real model, though not for accuracy.
- JSON fixtures: slot files, line files, observation/flow payloads, LotStatus samples (also used by the frontend: `frontend/src/api/__fixtures__/`).
- **Real images never go into `tests/`.**

## 4. Evaluation datasets (local, git-ignored)

| Set | Contents | Built in |
|-----|----------|----------|
| `data/samples/` + `data/labels/cam-ground.json` | Your Phase 1 sample images | P1.8 |
| `data/validation/cam-ground/` + labels | ~200 frames from the live camera across conditions | P4.8 |
| `data/recordings/` + `data/labels/<clip>.csv` | 3 × 1-hour ramp clips with hand tallies | P5.9 |

Every evaluation run writes `out/eval/<set>-<YYYYMMDD-HHMM>.json`. Copy the headline numbers into PROGRESS.md → Metrics.

## 5. CI (`.github/workflows/ci.yml`)

Jobs:
1. **backend**: `uv sync --frozen`, `ruff check`, `ruff format --check`, `pytest -m "not slow"`.
2. **frontend**: `npm ci`, `npm run lint`, `npm run test -- --run`, `npm run build`, `npx playwright install --with-deps chromium webkit`, `npm run e2e`.
3. **secrets**: gitleaks.
4. **e2e** (only if `backend/**` or `deploy/**` changed): build images, `docker compose -f deploy/docker-compose.test.yml up --abort-on-container-exit`.
5. **images** (only if `backend/**` changed): `docker buildx build --platform linux/amd64,linux/arm64` for both targets, without pushing. This catches "works on the Pi, breaks on x86" (and the reverse) early. Pushing images happens only on release tags ([deployment.md §8](deployment.md#8-releases-and-updating)).

The vision image is large; the e2e job uses a `FakeDetector` (env `PARKING_FAKE_DETECTOR=1`) that reads expected detections from a JSON sidecar next to each replay image, so CI doesn't need PyTorch.

## 6. Manual release checklist (from Phase 3 on)
- [ ] Live screen on a real Android and iPhone over mobile data (not Wi-Fi).
- [ ] Kill the API → banner appears within 30 s → restart → recovers by itself.
- [ ] Airplane mode → offline banner → back online → recovers.
- [ ] Lighthouse mobile: PWA installable, a11y ≥ 90, performance ≥ 90.
