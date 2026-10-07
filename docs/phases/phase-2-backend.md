# Phase 2: Backend and simulated live feed

**Goal:** the whole server pipeline running end to end (worker → MQTT → API → SSE), with a folder of images standing in for a camera.
**Needs hardware:** no.
**Specs used:** [architecture.md](../design/architecture.md), [api.md §1–3, §5](../design/api.md), [data-model.md](../design/data-model.md), [vision.md §3, §5, §8](../design/vision.md), [deployment.md §2–4](../design/deployment.md).

## Deliverables
- `parking worker occupancy` publishing observations
- `parking api` serving `/healthz`, `/api/lot`, `/api/status`, `/api/stream`
- Docker Compose with mosquitto + api + vision-occupancy
- An end-to-end test on a replay folder

## Order
```
P2.1 messaging ─> P2.2 sources ─> P2.3 worker ─────────────────────────────┐
P2.4 smoothing ─> P2.5 fusion ─> P2.6 db ─> P2.7 consumer ─> P2.8 SSE ─> P2.9 routes ─> P2.10 docker ─> P2.11 e2e
```

---

## P2.1: Messaging models and MQTT helpers
**Files:** `parking/messaging.py`, `parking/mqtt.py`, `tests/unit/test_messaging.py`

**Steps**
1. Pydantic models: `Observation`, `SlotScore`, `FlowEventMsg`, `CameraHealthMsg`, `Command`, `LotStatus`, `ZoneStatus`, `Totals`, exactly as in [api.md](../design/api.md#5-mqtt-internal-broker). `model_config = ConfigDict(extra="ignore")` for forward compatibility.
2. Topic builders: `topic_observation(lot, cam)`, `topic_flow`, `topic_health`, `topic_cmd`, `topic_snapshot(lot, cam, req)`, `topic_status(lot)`. Plus a `parse_topic(topic) -> (kind, cam)`.
3. `mqtt.py`:
   - `SyncPublisher` (paho, for workers): connect with credentials, `loop_start()`, automatic reconnect, Last Will on the health topic, `publish_model(topic, model, qos, retain)`, and subscribe to `cmd` with a callback.
   - `async_client(settings)` context manager (aiomqtt, for the API) with a reconnect loop (backoff 1 → 30 s).
4. Tests: round-trip each model through JSON; topic parse/build symmetry.

**Done when:** tests pass.

## P2.2: Frame sources and health checks
**Files:** `parking/vision/sources.py`, `parking/vision/health.py`, tests

**Steps**
1. `FrameSource` protocol: `read() -> Frame | None` (`Frame = (np.ndarray, ts: datetime)`), `close()`.
2. `make_source(uri)` parses the schemes in [config.md "Source URI formats"](../design/config.md#source-uri-formats). Implement `file:` and `folder:` now. `snapshot:`/`rtsp:`/`video:` come in Phases 4–5 (raise `NotImplementedError` with a clear message).
3. `FolderReplaySource`: sorted file list, re-scanned on every pass (new files dropped in get picked up); `interval` handled by the **worker loop**, not the source.
4. `health.py`: `FrameHealth` with `check(frame) -> Issue | None` per [vision.md §5](../design/vision.md#5-frame-health-parkingvisionhealthpy), and rolling `unhealthy_ratio`.
5. Tests with generated numpy frames (all-black, identical repeated frames, a Gaussian-blurred texture).

**Done when:** tests pass.

## P2.3: Occupancy worker
**Files:** `parking/workers/base.py`, `parking/workers/occupancy_worker.py`, CLI `worker occupancy`

**Steps**
1. `base.Worker`: loads config + settings, creates the publisher, handles SIGTERM/SIGINT (finish the current frame, publish `down`, disconnect), publishes health every 10 s from a timer thread, handles `cmd` messages (`reload` → reload slot file + config section; `snapshot` → publish the current or annotated frame as JPEG on `snapshot/<request_id>`).
2. `OccupancyWorker.loop()`:
   ```
   every sample_every_s:
       frame = source.read()
       issue = health.check(frame)
       if issue: record, continue
       result = pipeline.analyze_frame(frame, cam, slots, detector)   # from P1.7
       publish Observation(qos=1)
   ```
   Use a monotonic clock for scheduling, and don't drift if analysis is slow (next run = previous start + interval; skip if it's late by more than one interval).
3. `--fake-detector` flag (or env `PARKING_FAKE_DETECTOR=1`) uses `FakeDetector` (sidecar JSON per image), for CI.
4. Log one line per observation at DEBUG and a summary every minute at INFO.

**Done when:** with a local broker (`docker run --rm -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf`) and `mosquitto_sub -t 'parking/#' -v`, observations appear every 5 s.

## P2.4: Smoothing
**Files:** `parking/core/smoothing.py`, `parking/core/clock.py`, tests

Implement `SlotSmoother` and `CountSmoother` per [vision.md §3](../design/vision.md#3-temporal-smoothing-parkingcoresmoothingpy-runs-in-the-api). Include `restore(states: dict[str, bool])` for start-up.

**Done when:** all the smoothing cases in [testing.md §2](../design/testing.md#2-what-must-have-unit-tests) pass.

## P2.5: State store and fusion
**Files:** `parking/core/fusion.py`, `parking/core/flow_counter.py` (skeleton; filled in Phase 5), tests

**Steps**
1. `StateStore(config, clock)`:
   - `apply_observation(obs) -> list[Change]`: feeds the smoother, recomputes the zones that camera covers, and returns what changed (zone counts, slot flips).
   - `apply_health(msg)`: updates camera state and the confidence inputs.
   - `tick()`: called every second; marks zones `stale` after `stale_after_s` and returns a change if staleness flipped.
   - `status() -> LotStatus`: the full current status with level, trend, confidence and totals ([vision.md §8](../design/vision.md#8-fusion-confidence-trend-parkingcorefusionpy), [api.md Levels](../design/api.md#levels)).
2. Trend ring buffer per zone (`deque` of (ts, free), 20 min).
3. Tests with a fake clock: level boundaries, totals, stale on/off, trend, confidence when degraded.

**Done when:** tests pass.

## P2.6: Database
**Files:** `parking/db/engine.py`, `parking/db/models.py`, `parking/db/repo.py`, `alembic.ini`, `migrations/`, CLI `db upgrade`

**Steps**
1. Engine with WAL pragmas on connect.
2. Tables for this phase: `zone_state`, `slot_state`, `camera_health`, `flow_event`, `correction` ([data-model.md §1](../design/data-model.md#1-tables)). The push and admin tables come in Phases 6–7, each with its own migration.
3. `alembic init migrations` and an autogenerate first revision. Check it by hand.
4. `repo.py`: `record_changes(changes)`, `latest_zone_states()`, `latest_slot_states()`, `upsert_camera_health()`.
5. Integration test: a temp DB, upgrade, write, read back.

**Done when:** `parking db upgrade` creates `data/db/parking.sqlite` with the tables.

## P2.7: MQTT consumer in the API
**Files:** `parking/api/mqtt_consumer.py`

**Steps**
1. On API start-up (FastAPI `lifespan`): `db upgrade`, restore `StateStore` from the DB (marked stale), start the consumer task and a `tick()` task (1 s).
2. Consumer: subscribe to `parking/<lot>/camera/+/observation`, `+/flow`, `+/health`. Dispatch to the `StateStore`. For each change → `repo.record_changes` (in a thread via `anyio.to_thread`) → `broadcaster.publish(status)` → publish the retained `status` topic.
3. Never crash the task on a bad message: log it and continue. Count bad messages in a metric shown in `/healthz`.

**Done when:** with the worker running, the API logs zone changes and `mosquitto_sub -t parking/main/status` shows the retained status.

## P2.8: SSE broadcaster and `/api/stream`
**Files:** `parking/api/sse.py`, `parking/api/routes/public.py`

**Steps**
1. `Broadcaster`: `subscribe() -> asyncio.Queue` (maxsize 10), `unsubscribe(q)`, `publish(status)` puts to every queue (if full: drop the oldest, then put). Track the client count.
2. `/api/stream` with `sse-starlette`: first yield `retry` + the current status, then loop on the queue. Ping every `sse_ping_s` (`EventSourceResponse(ping=15)`). Cap the clients (e.g. 1000 → 503).
3. Disconnect cleanup in `finally`.

**Done when:** `curl -N localhost:8000/api/stream` prints the current status at once, then new events as images change, with `: ping` lines in between.

## P2.9: REST endpoints, CORS, errors
**Files:** `parking/api/app.py`, `parking/api/deps.py`, `parking/api/routes/public.py`, `parking/cli.py` (`api` command)

**Steps**
1. `create_app(settings)`: routers, CORS from `CORS_ORIGINS`, the [error format](../design/api.md#error-format) via exception handlers, slowapi limits, gzip middleware (excluding SSE).
2. `/healthz`, `/api/lot` (location from settings), `/api/status` (503 `unavailable` until the first observation, unless restored from the DB).
3. `?lang=` / `Accept-Language` resolution for zone names.
4. `parking api --host --port --reload`.
5. Integration tests with httpx `AsyncClient` + an in-process fake MQTT (call the consumer's dispatch directly).

**Done when:** tests pass, and `/docs` shows the endpoints in DEBUG mode.

## P2.10: Docker images and Compose
**Files:** `backend/Dockerfile`, `deploy/docker-compose.yml`, `deploy/mosquitto/{mosquitto.conf,acl}`, `deploy/.env.example`

**Steps**
1. Dockerfile per [deployment.md §2](../design/deployment.md#2-docker-images-backenddockerfile-multi-stage).
2. Compose per [deployment.md §3](../design/deployment.md#3-compose-deploydocker-composeyml), without `cloudflared` and `vision-flow` for now.
3. Mosquitto config, ACL and password file ([deployment.md §4](../design/deployment.md#4-mqtt-brokers)).
4. Set `config/lot.yaml` → `cam-ground.source: "folder:data/replay/ground?interval=5&loop=true"` and copy a few sample images into `data/replay/ground/`.
5. `cd deploy && docker compose up -d --build` → `docker compose ps` shows everything healthy.

**Done when:** `curl localhost:8000/api/status` on the Pi returns a real `LotStatus` computed from the replay images.

## P2.11: End-to-end test
**Files:** `deploy/docker-compose.test.yml`, `backend/tests/e2e/test_pipeline.py`, `backend/tests/fixtures/replay/*` (synthetic images + detection sidecars)

**Steps**
1. Test compose: mosquitto + api + vision-occupancy with `PARKING_FAKE_DETECTOR=1`, using a fixture folder.
2. The test opens `/api/stream`, waits for the first status, copies a new fixture image (with different sidecar detections) into the replay folder, and asserts the expected free count arrives within `3 × interval + 5 s`.
3. Wire it into CI ([testing.md §5](../design/testing.md#5-ci-githubworkflowsciyml), job 4).

**Done when:** green locally and in CI.

---

## Exit criteria
- [ ] Dropping a new image into `data/replay/ground/` changes the numbers on `curl -N …/api/stream` within ~20 s
- [ ] Restarting the API shows the last known numbers immediately (marked stale), then live again
- [ ] Killing the worker → health `down` → zone `stale` after 60 s
- [ ] Unit + integration + e2e tests green in CI
