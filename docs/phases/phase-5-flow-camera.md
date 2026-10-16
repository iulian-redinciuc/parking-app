# Phase 5: Entry/exit camera and combining levels

**Goal:** count cars entering and leaving with Camera A, keep a correct running count for the level it covers, and combine all levels into one status.
**Needs hardware:** Camera A, and the vision host from P4.1 (plus maybe an accelerator, decided in P5.10).
**Where the work happens:** development and clip evaluation on the **dev Pi**; the speed check and the live drift week on the **vision host at the lot**.
**Specs used:** [vision.md §7–8, §10](../design/vision.md), [api.md §4–5](../design/api.md), [data-model.md](../design/data-model.md), [config.md §3](../design/config.md#3-line-file-configlinescamerajson).

## Deliverables
- `parking worker flow` publishing in/out events
- `FlowCounter` in the API with corrections and an optional scheduled reset
- Recorded test clips with hand tallies, and measured accuracy
- One live week with measured drift

---

## P5.1: Mount Camera A and check the view
**Steps**
1. Position per the layout option chosen (A: underground ramp; B: main entrance), side-on to the lane ([hardware.md §2](../design/hardware.md#flow-camera-camera-a)).
2. Network and harden it like Camera B (P4.2). Sub-stream 640×360, ~10–15 fps, H.264, keyframe interval ≤ 2 s.
3. Save a sub-stream frame → `data/reference/cam-ramp.jpg`.
4. Slot editor → **lines mode**: ROI, `line_a`, `line_b` (about a car-length apart, perpendicular to travel), `in_direction` → `config/lines/cam-ramp.json`.
5. `parking lines-check --camera cam-ramp`: checks the line file (each line across the lane, not crossing, roughly parallel, ≥ 3% of the frame diagonal apart, inside the frame and the ROI, drawn on a frame of the reference's shape) and writes `out/lines/cam-ramp.jpg` with the ROI, A/B and the IN arrow on the reference frame. Exit 1 lists what to fix.

**Done when:** the lines file is committed, `parking lines-check --camera cam-ramp` prints `ok`, and its overlay shows the whole lane with ~3 m before and after the lines.

**PoC check (2026-10-15):** no ramp camera exists, so the reference frame is a rendered stand-in: `backend/scripts/synth_ramp.py data/reference/cam-ramp.jpg` (640×360 like the sub-stream, the ramp lane seen side-on from the opposite wall, ~42 px per metre, wall panels 2.5 m wide; deterministic, git-ignored like every reference). The lines were drawn on it in the slot editor's lines mode (headless Chromium, real clicks: `line_a` on the street side, `line_b` ~4.8 m further in, the ROI around the lane plus car height, `in_direction: a_to_b` = down the ramp) and exported to `config/lines/cam-ramp.json` (committed). `parking lines-check --camera cam-ramp`: `line_a 162 px, line_b 160 px, 200 px apart, 0° between them, IN = a_to_b`, no warnings, **`ok`**, exit 0; the overlay `out/lines/cam-ramp.jpg` shows the whole lane with ~5 m before line A and after line B. New test: the committed file passes the check on the rendered frame (gap a car length, ≥ 3 m each side). Not checked: a real camera's position, network, stream settings, or lines on its view (go-live-checks.md).

## P5.2: Low-latency RTSP + video file source
**Files:** `parking/vision/sources.py`

**Steps**
1. Reuse `RtspSource` (P4.3). Make sure the reader thread always returns the **newest** frame and drops the backlog.
2. `VideoFileSource` (`video:` scheme) plays at native fps, or as fast as possible with `realtime=false`, for evaluation.
3. `parking record --camera cam-ramp --minutes 60 --out data/recordings/`: save the sub-stream to MP4 with **ffmpeg stream copy** (`ffmpeg -rtsp_transport tcp -i URL -c copy -t 3600 out.mp4`), with no re-encoding.

As built (details in [config.md "Low latency and recordings"](../design/config.md#source-uri-formats) and [architecture.md §6](../design/architecture.md)): `rtsp:` counts overwritten frames in `dropped` and adds FFmpeg's `nobuffer`/`low_delay`; frames carry a `seq`; sources report `fps`. `parking record` writes a fragmented MP4 (only the video stream) and checks it with `ffprobe`; `parking stream-check` measures a source's frame rate. Both need FFmpeg, so on a host without it run them in the vision container, e.g. on the vision host:

```bash
cd deploy
docker compose run --rm --entrypoint parking vision-occupancy record --camera cam-ramp --minutes 60
docker compose run --rm --entrypoint parking vision-occupancy stream-check --camera cam-ramp --seconds 300
docker compose run --rm --entrypoint parking vision-occupancy stream-check \
  --source video:data/recordings/cam-ramp-<date-time>.mp4 --seconds 3600 --window 60
```

Without Camera A, `backend/scripts/fake_rtsp.py` (run where FFmpeg is, e.g. the vision container) serves a 640×360 10 fps test pattern at `rtsp://127.0.0.1:8554/sub`; pass it with `--source rtsp:rtsp://127.0.0.1:8554/sub`.

**Done when:** a 1-hour recording plays back, and the source reports a steady fps (on the real Camera A: `record --minutes 60` exits 0, and `stream-check` exits 0 both on the live camera and on the recording with `video:`).

**PoC check (2026-10-15):** on the vision image (`parking-p52-poc`, this commit's code) against `backend/scripts/fake_rtsp.py` serving the rendered ramp frame (`data/reference/cam-ramp.jpg`, 640×360, 10 fps, keyframe every 2 s) as `cam-ramp`'s `CAM_RAMP_RTSP_URL`, shortened to 15 min: `record --camera cam-ramp --minutes 15` → 900.7 s, h264 640×360, 9 007 frames, 10.00 fps, 27.8 MB, exit 0; at the same time `stream-check --camera cam-ramp --seconds 840 --window 60` on the live `rtsp:` source → 8 400 frames, every window 10.00 fps, longest gap 0.11 s, 1 dropped, 0 reconnects, steady, exit 0. A recording from the same setup played back with `video:` (realtime, 5 min, 60-s windows) → 3 002 frames, 10.00 fps, longest gap 0.10 s, steady, exit 0, and decoded in full with `realtime=false` (9 007 frames in 5.3 s). A first run with `pytest` loading the CPU at the same time had one 1.28-s gap in the live check (not steady), so it was run again with nothing else on the CPU. This check found a bug in the fake camera: a slideshow went out in 5-s bursts (3.6–4.2 fps); it is now paced on the output. The 1-hour length was run on the test pattern on 2026-10-10 (session log); the real Camera A check is in [go-live-checks.md](../go-live-checks.md#p52-low-latency-rtsp-video-source-and-parking-record).

## P5.3: Motion gate
**Files:** `parking/vision/motion.py`, tests

Implement [vision.md §7.2](../design/vision.md#72-motion-gate-parkingvisionmotionpy). Tests: a static synthetic scene → inactive; a moving rectangle → active; the hold-over keeps it active for 2 s after the motion stops.

As built: `motion_min_area_px` is in pixels of the line file's `image_size`; details in vision.md §7.2. To check an off-peak hour of a P5.2 recording:

```bash
parking motion-check --camera cam-ramp --source "video:data/recordings/cam-ramp-<date-time>.mp4?realtime=false"
```

**Done when:** on a recorded clip, the gate is active for < 20% of an off-peak hour (log the ratio): `motion-check` prints the share and exits 0.

**PoC check (2026-10-15):** no Camera A recording, so a synthetic off-peak clip of the rendered ramp view: `uv run python scripts/synth_ramp.py --clip 20 --cars 6 ../data/recordings/cam-ramp-synthetic-offpeak.mp4` (640×360, 10 fps, mp4v, 20 min = the loop's cap instead of an hour; fresh sensor noise every frame, the light drifting ±3%, 6 cars at 8–15 km/h, i.e. 18 an hour, a car in view in 2.8% of frames). `parking motion-check --camera cam-ramp --source "video:data/recordings/cam-ramp-synthetic-offpeak.mp4?realtime=false" --seconds 1200` with the committed `cam-ramp` lines and `motion_min_area_px: 1500`: 12 000 frames over 1 200 s, **gate active 3.4%** (413 frames) in 6 bursts, one per car, ok, exit 0. The first run found a bug: on this textured scene MOG2 marked ~7 000 px of the first frame foreground, so the gate opened for 2 s at the start (and would after every reset/reconnect: 7 bursts, 3.6%); the first frame after a reset now only teaches the background (1 new test). A drawn clip has no headlights, reflections, people or real compression noise; the real check is in [go-live-checks.md](../go-live-checks.md#p53-motion-gate).

## P5.4: Tracker integration
**Files:** `parking/vision/tracking.py`

**Steps**
1. Wrap `model.track(frame, persist=True, tracker="bytetrack.yaml", classes=[2,3,5,7], conf=0.4, imgsz=640)` and return `Detection`s with `track_id`.
2. When the gate goes inactive, **don't** reset the tracker straight away. Reset after 10 s inactive, to drop stale tracks.
3. Apply the ROI by masking the frame (fill outside the ROI with black) before tracking.

As built: details in [vision.md §7.1](../design/vision.md#71-frame-pipeline-flow-worker). Until the flow worker (P5.6) exists, the annotated export comes from `parking track-check`; on a P5.2 recording:

```bash
parking track-check --camera cam-ramp --source "video:data/recordings/cam-ramp-<date-time>.mp4?realtime=false" \
  --seconds 3600 --debug-video out/tracks/cam-ramp.mp4
```

**Done when:** on a clip, each passing car keeps one track id from entering to leaving the frame (inspect with an annotated video export: `--debug-video out.mp4`).

**PoC check (2026-10-15):** no Camera A recording, so P5.3's synthetic off-peak clip of the rendered ramp (`synth_ramp.py --clip 20 --cars 6`: 20 min, 640×360, 10 fps, 6 drawn cars at 8–15 km/h, 3 in each direction). `parking track-check --camera cam-ramp --source "video:data/recordings/cam-ramp-synthetic-offpeak.mp4?realtime=false" --seconds 1200 --debug-video out/tracks/cam-ramp.mp4` with the committed `cam-ramp` lines and detector (NCNN `yolo11n` @ 640, conf 0.4) on the dev Pi (2 min 23 s): 12 000 frames, 413 gated in (150 ms each), **6 tracks for 6 cars** (#1, #3, #4, #6, #8, #10), each running from one edge of the lane to the other across both lines (e.g. #1 (542,276) → (75,294), #4 (43,293) → (561,274)), 6 resets (one 10 s after each car). The annotated video was checked by eye on a contact sheet of every pass: one box and one id per car, no second id. `yolo11n` sees the drawn cars only on some frames (11–34 of the ~45–90 a car is in view, rarely while it is half out of the frame), so this mostly shows that ByteTrack carries an id across missed detections; the ids skip numbers because ByteTrack also numbers tracks that never confirm. Real cars, headlights, two cars at once and a car stopping on the ramp are in [go-live-checks.md](../go-live-checks.md#p54-tracker-integration).

## P5.5: Two-line counter
**Files:** `parking/vision/flow.py`, tests

Implement `TwoLineCounter` per [vision.md §7.3](../design/vision.md#73-two-line-counter-parkingvisionflowpy). Unit tests listed in [testing.md §2](../design/testing.md#2-what-must-have-unit-tests) (synthetic tracks, no model).

**Done when:** all tests pass.

## P5.6: Flow worker
**Files:** `parking/workers/flow_worker.py`, CLI `worker flow`

**Steps**
1. Loop: `read → gate → track → counter → api_client.queue_flow_events([FlowEventMsg(event_id=uuid4, …)])` (the outbox guarantees delivery).
2. Health every 10 s: fps over the last 10 s, `inference_ms_avg`, gate-active ratio.
3. `--debug-video PATH` writes the annotated frames (boxes, track ids, lines, running in/out) for checking.
4. Enable the compose profile: `docker compose --profile flow up -d`.

**Done when:** driving or walking a car through the lines produces exactly one event in the right direction (check with `--print`, or the API log).

As built: see [vision.md §7.1](../design/vision.md#71-frame-pipeline-flow-worker) and the health fields in [api.md](../design/api.md#health-every-10-s). On the vision host, once Camera A and its line file exist:

```bash
cd deploy
docker compose --profile flow up -d                       # the vision-flow service
docker compose logs -f vision-flow                        # one "IN (track #…)" / "OUT …" line per car
# or by hand, without sending anything:
docker compose run --rm vision-flow flow --camera cam-ramp --print --control-port 0 \
  --debug-video /app/data/flow-debug.mp4
```

**PoC check (2026-10-15):** no Camera A, so P5.3's 20-min synthetic clip of the rendered ramp (6 drawn cars; truth from `synth_ramp.py`'s schedule: IN, OUT, OUT, IN, IN, OUT). (1) `parking worker flow --camera cam-ramp --print --debug-video …` on the dev Pi with `cam-ramp`'s source pointed at the clip (`video:…?realtime=false`, a scratch copy of lot.yaml): **6 events, exactly the truth's directions and order**, one `uuid4` `event_id` each, health lines every 10 s. (2) A **production rehearsal** (`parking-rehearsal`: `docker-compose.yml` + `docker-compose.fake-camera.yml` + a scratch override for names `parking-rh-*`, its own data folder, images built from this commit, `.env` from `prod-env.sh server`; API on loopback :8096): `fake_rtsp.py` looping the clip as `cam-ramp`'s RTSP camera at 10 fps, `--profile flow` → `vision-flow` (1.5 CPUs, `OMP_NUM_THREADS=2`) over the full 20 min: **6 events for 6 cars, all in the right direction** (`IN (track #1 …)` … `OUT (track #8 …)` in `docker compose logs`), API `/healthz` `ingest.flow_events` 6, `rejected` 0, 6 `flow_event` rows (the second OUT stored `applied=false`: the zone started at 0, so it was clamped), 35–46 gated frames per car (~150–180 ms per gated frame in the `--print` runs below). The guide's `docker compose run --rm vision-flow flow --print --control-port 0 --debug-video …` printed the events and health lines and wrote a readable annotated video (checked by eye). In two shorter `--print` runs of the first cars, car 1 (a dark blue drawn car `yolo11n` boxes on only some frames) was counted once and missed once, car 2 counted; no run gave a double count or a wrong direction. Two fixes came out of it: the first two rehearsal runs counted none of the 6 car passes they saw, because (a) ultralytics sets up its predictor on the first detection (~2.5 s, which landed on the first car), now done at start (`warmup`), and (b) NCNN ignores `OMP_NUM_THREADS` and ran 4 threads in the 1.5-CPU quota (benchmark under the same quota: 288 ms median / 513 ms p90 per frame; with other detectors running, ~2 processed frames a second while a car passed, 5–16 per car); the tracker now reloads an NCNN model with `OMP_NUM_THREADS` threads (172 / 198 ms). Real cars, real light and the vision host: [go-live-checks.md](../go-live-checks.md#p56-flow-worker).

## P5.7: FlowCounter in the API + corrections
**Files:** `parking/core/flow_counter.py`, `parking/api/routes/admin.py`, `parking/api/deps.py` (bearer auth with `ADMIN_TOKEN`), migration if needed

**Steps**
1. `FlowCounter` per [vision.md §7.4](../design/vision.md#74-flow-counter-parkingcoreflow_counterpy-in-the-api): idempotent by `event_id` (keep the last 1000 IDs in memory + the DB primary key), clamp, `correct()`, `restore()`.
2. `/internal/flow-events` → `ingest.py` → counter → `flow_event` row → zone change → SSE.
3. `POST /api/admin/zones/{id}/correct` with `Authorization: Bearer $ADMIN_TOKEN` ([api.md §4](../design/api.md#4-admin-endpoints)). Writes a `correction` row. *(Already built in P7.4, with the restore of the confidence counters.)*
4. Optional scheduled reset (`zones[].reset`) via APScheduler. *(Built in P5.7: a `reset-<zone>` job in `MaintenanceJobs` → `Ingestor.correct`, actor `scheduled-reset`, `zone_state` source `reset`; the cron is lot time with crontab weekdays.)*
5. Tests: duplicates ignored; clamp sets `applied=false`; correction; restore after restart.

**Done when:** `curl -X POST -H "Authorization: Bearer …" -d '{"occupied":37}' …/correct` updates the phone at once.

## P5.8: Fusion update and confidence
**Steps**
1. Wire flow zones into `StateStore` with the confidence formula from [vision.md §8](../design/vision.md#8-fusion-confidence-trend-parkingcorefusionpy).
2. **Option B** layouts (entrance camera counts the whole lot): add zone method `derived` with `formula: total_flow - ground`, if Option B was chosen. Otherwise skip this.
3. The frontend already shows "≈" below 0.8. Check it.

**Done when:** the underground card shows ≈ after enough events without a correction, and a correction removes it.

## P5.9: Test clips and flow evaluation
**Files:** `tools/flow-tally/` (`index.html` + `tally.js`, see its README), `parking/vision/evaluate.py` (flow part), CLI `evaluate-flow`

**Steps**
1. Record three 1-hour clips: weekday rush hour, night, and a quiet period (`parking record`).
2. **Tally tool** (static page): open a local video, play it at 1–4× speed, press **I** for in and **O** for out (and **U** to undo) → export CSV in the [labels format](../design/config.md#flow-datalabelsclipcsv).
3. `parking evaluate-flow --video data/recordings/x.mp4 --truth data/labels/x.csv --camera cam-ramp` runs the full pipeline on the file and prints TP/FP/FN, event accuracy and net error ([vision.md §10](../design/vision.md#flow-metrics-per-clip)).
4. Tune `conf`, `min_track_frames`, line positions and ROI. Re-run all three clips after each change and keep a table in Metrics.

**Done when:** ≥ 98% event accuracy on all three clips, or the gap is understood (e.g. queued cars at the barrier) with a plan.

**PoC check (2026-10-16):** no Camera A, so three 20-min synthetic clips of the rendered ramp (the loop's cap instead of 1 h), made by `backend/scripts/synth_ramp.py --scenario` with their truth CSV from the generator's schedule (`--truth`; the time when the car's middle passes the second line on its way): **offpeak** (P5.3's clip, byte-identical; 6 cars, 3 in / 3 out), **night** (`--cars 14`: one lamp off, the other dimmed, more sensor noise, headlights with a beam on the floor and tail lights; 5 in / 9 out) and **rush** (`--cars 60`: 85% in, platoons of up to 4 cars 1.5–4 s apart, a fifth of the platoons queueing at 4–6 km/h; 54 in / 6 out). `parking evaluate-flow --camera cam-ramp` with the committed lines and detector (NCNN `yolo11n` @ 640, conf 0.4, `min_track_frames` 5), one clip at a time on the dev Pi: **offpeak 100%** (TP 6, FP 0, FN 0, net 0), **night 100%** (TP 14, net 0), **rush 62.3%** (TP 38, FP 1, FN 22, net −13); tuning `--conf 0.25` on rush: **71.7%** (TP 43, FP 0, FN 17, net −11). **The rush gap is understood:** it's the COCO detector on flat drawn cars, not the tracker or the counter. Running `yolo11n` once per car on every 3rd frame of its passage between the lines, the 22 missed cars get a box at conf ≥ 0.4 on a median of 9% of those frames (10 never; max conf as low as 0.07: mostly light beige, brown and orange drawn cars), the counted ones on a median of 45%. The rush clip has 60 random colours, so it draws more of them. In the debug video of the first 200 s, the missed cars (a brown one, two beige ones in a platoon) cross the lines with no box, while the cars around them keep their own ids: no id swap and no merged count was seen. A first run found a convention gap: the truth (then the middle of the lane between the lines) came 2.0–2.1 s before the count for cars queueing at 5 km/h (the counter counts at the second line). So the miss and the count showed as an FN plus an FP. The truth, the tally README and vision.md §10 now say "when the car has crossed the second line" (mean offset +1.1 s → +0.2 s). **Plan**, on the real tallied clips (go-live-checks.md): if real cars show the same gap, (1) conf 0.25 (no extra counts here), (2) `yolo11s`, within P5.10's budget, (3) fine-tune on frames from the tallied clips (P9.4's `export-yolo` chain); queues at the barrier are covered by the slow platoons here. **Tally tool:** the night clip (transcoded to VP8 WebM in the vision image's ffmpeg, as Chromium doesn't play mp4v) opened in `tools/flow-tally/` in headless Chromium (Playwright, `/usr/bin/chromium`), played at 4×, I/O pressed 1.2 s of clip after each car's middle passed the lines (the convention at the time), plus an O + U slip: the export (`cam-ramp-synthetic-night.csv`, 5 in / 9 out, no page errors) matched the generator's truth 14/14 within ±2 s (mean +1.3 s) and loads with `load_flow_labels`. Not checked: real clips tallied by a person, real cars, headlights and queues (go-live-checks.md).

## P5.10: Performance on the vision host and the accelerator decision
**Steps**
1. On the **vision host** (not the dev Pi), with the runtime for that machine ([vision.md §11](../design/vision.md#11-runtimes-and-performance)), play the busiest clip in real time (`realtime=true`) with the occupancy worker also running. Record the achieved fps, CPU %, and temperature.
2. If fps drops below 8 while cars pass, or the CPU runs hot: try a smaller ROI or `imgsz=480` first. If still short, add the accelerator that fits that machine: an **AI HAT+** with a `HailoDetector` on a Pi, or a faster runtime / GPU elsewhere. The `Detector` protocol keeps this a contained change.
3. The dev Pi's numbers are only a worst-case reference. Record the vision host's numbers and the decision.

How to measure (on the vision host, from the repo root; set `cam-ramp`'s `source` to `video:data/recordings/<busiest clip>.mp4?realtime=true&loop=true` in a copy of lot.yaml):
```bash
docker compose -f deploy/docker-compose.yml --profile flow up -d   # API + occupancy + flow workers on their usual CPU limits
python3 backend/scripts/soak.py sample --out out/perf/sample.jsonl --interval 10 --duration 900 \
  --containers parking-api,parking-vision-occupancy,parking-vision-flow &   # temperature, throttling, per-container CPU
set -a; . deploy/.env; set +a   # ADMIN_TOKEN
while sleep 10; do curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:${API_HOST_PORT:-8000}/api/admin/cameras \
  | python3 -c 'import json,sys; c=[x for x in json.load(sys.stdin) if x["id"]=="cam-ramp"][0]; print(c["fps"], c["inference_ms_avg"], c["gate_active_ratio"])'
done | tee out/perf/flow.txt   # the flow worker's health every 10 s: fps, inference_ms_avg, gate_active_ratio
```
The workers send their health to the API, they don't log it (only `--print` prints it), so read it from `/api/admin/cameras`. Read fps from the samples with a high `gate_active_ratio` (cars passing, e.g. ≥ 0.6). If the flow container sits at its `FLOW_CPUS` limit (1.5 by default), try `FLOW_CPUS=2` before anything else. `cam-ramp` runs a **480 px** model since P5.10 (`parking models export --model yolo11n --imgsz 480 --runtime ncnn --out models/flow-480`; without `--out` it would overwrite the 640 px export in `models/`); after any model change check that the accuracy holds with `parking evaluate-flow` on the P5.9 clips. The dev Pi references are in PROGRESS.md → Metrics: 2026-10-10 (host venv, no CPU limits) and 2026-10-16 (the PoC check below: Compose limits, rush clip).

**Done when:** the decision is recorded with numbers.

**PoC check (2026-10-16):** the vision host is a Raspberry Pi 5 8 GB like the dev Pi (P4.1) but doesn't exist yet, so a **production rehearsal** on the dev Pi: Compose project `parking-rehearsal` (`docker-compose.yml` + a scratch override for names `parking-rh-*`, images built from this commit, `.env` from `prod-env.sh server`, API on loopback :8096, a scratch config and data folder), `--profile flow` with the usual limits (`vision-flow` 1.5 CPUs, `OMP_NUM_THREADS=2`; occupancy 1 CPU, appearance scorer on the replay feed every 5 s), `cam-ramp`'s source = P5.9's **rush** clip (the busiest: 60 cars in 20 min, platoons, queueing) as `video:…?realtime=true&loop=true`. 15 min per setting, health polled from `/api/admin/cameras` every 10 s and `soak.py sample` every 10 s; fps "while busy" = samples with `gate_active_ratio` ≥ 0.6 (22–26 per run). **640 px, `FLOW_CPUS=1.5`: 6.65 fps** median while busy (6.4 at worst), 148 ms per gated frame, the container at its 1.5-CPU cap (p90 151%): short of 8. **640 px, `FLOW_CPUS=2`: 8.3** (7.9 at worst), 118 ms, at the 2-CPU cap: just at the target. **480 px, `FLOW_CPUS=1.5`: 9.55** (9.3 at worst), 72 ms, p90 135%: headroom. All three: 54–66 °C, `throttled=0x0`, 0 restarts, flow ≤ 507 MB. Counted events in each 15 min (truth 49 by then): 32, 32, 38. Accuracy at 480 (`evaluate-flow`, NCNN `yolo11n` @ 480, conf 0.4, one clip at a time): **offpeak 100%** (6/6), **night 100%** (14/14), **rush 70.0%** (TP 42, FP 0, FN 18; 62.3% at 640): no double counts, so no class-agnostic NMS needed. **Decision: no accelerator; `cam-ramp` runs `yolo11n` @ 480** (`models/flow-480/`, committed lot.yaml), `FLOW_CPUS` stays 1.5 (PROGRESS.md decision log). The health was never in the worker's log (the guide's `grep '"fps"'` found nothing), so `/api/admin/cameras` now also returns `gate_active_ratio` (api.md, 1 test) and the measuring commands above read it from there. Drawn cars on a synthetic clip on the dev Pi, with the dev stack running alongside; the vision host's own numbers on a real clip are in [go-live-checks.md](../go-live-checks.md#p510-performance-on-the-vision-host-and-the-accelerator-decision).

## P5.11: Live week drift test
**Steps**
1. Correct the count to the true value on day 0.
2. Run on the vision host (same temporary setup as P4.11). Each day, at a time you can count the level (or check its occupancy camera if Option C), note true vs app value. **Don't correct** during the test.
3. Drift per day = |error change| / days.
4. If drift is above target: check for a pattern (night? queues? pedestrians?) and use a clip of that situation to fix it. Consider enabling the scheduled reset.

How to run it (on the vision host, from `backend/`; the notes go to the git-ignored `data/labels/drift-<zone>.csv`, format in [config.md §4](../design/config.md#drift-notes-datalabelsdrift-zonecsv), maths in [vision.md §10](../design/vision.md#drift-test-live-flow-zone)). Keep the zone's `reset.enabled: false` and make no corrections during the week, or the test starts again:
```bash
# day 0: count the level, correct the app to it, then write the first note (error 0)
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"occupied": 37, "note": "drift test day 0"}' http://localhost:8000/api/admin/zones/underground/correct
uv run parking drift-note --zone underground --true 37
# every day, when the level can be counted (the app's value is read from the API at that moment;
# counted earlier? pass what the app showed then: --app 41 --at 2026-11-03T08:10+02:00)
uv run parking drift-note --zone underground --true 41 --note "after a rainy night"
# any time; exit 0 only for `verdict: PASSED` (<= 2 cars/day over >= 7 days)
uv run parking drift-report --zone underground
```
The `step` and `/day` columns show which day the error moved: look at that period's flow events (admin page) and record a clip of the same situation for `parking evaluate-flow`.

**Done when:** ≤ 2 cars/day of drift, or an accepted alternative (scheduled reset + display ≈).

**PoC check (2026-10-16):** no Camera A, vision host or level to count, so a **compressed week** in a production rehearsal on the dev Pi: Compose project `parking-rehearsal` (`docker-compose.yml` + a scratch override for names `parking-rh-*`, the local images with this commit's `backend/parking` mounted, its own `.env` from `prod-env.sh server`, its own config and data folder, API on loopback :8096), `--profile flow` (`vision-flow` 1.5 CPUs, NCNN `yolo11n` @ 480 as decided in P5.10), `cam-ramp`'s source = one 18.9-min synthetic clip played in real time (`video:…?realtime=true`): **7 "days" of 2.6 min**, each a fresh `synth_ramp.py` segment (new seeds 501–507, so cars the clips of P5.9 never had) cycling offpeak / rush / night / offpeak / rush / night / offpeak, 38 cars in all (26 in, 12 out), every day starting with 30 s without cars. Day 0: `POST /api/admin/zones/underground/correct` to 20 and `parking drift-note --true 20`; then after each day, in the next day's empty 30 s (the video position taken from the worker's 60-s frame counts), `parking drift-note --true <20 + the generator's net so far> --at <day 0 + n days>` with the app's value read live from the rehearsal's `/api/status`; `reset.enabled: false`, no corrections. `parking drift-report --zone underground`: errors +0, +2, +1, +1, +3, +1, +1, +1 → **drift 0.14 cars/day, `verdict: PASSED`**, exit 0 (largest one-day step ±2). **That number flatters the counter:** the worker counted 21 of 26 INs and **6 of 12 OUTs** (no false events; 71% event accuracy, close to P5.9's rush score), and the misses happened to cancel. Day 1, for example, lost both OUTs (+2) and day 5 lost two INs (−2). On the real lot, where cars enter in the morning and leave in the evening, missed OUTs would not cancel like this, so **the scheduled reset stays the fallback** if the real week fails. The fix is the same as P5.9's: the detector on the real Camera A clips. A compressed day has 3–7 cars rather than a real day's hundreds, so cars/day here are not real-day numbers. The real week is in [go-live-checks.md](../go-live-checks.md#p511-live-week-drift-test).

---

## Exit criteria
- [ ] ≥ 98% event accuracy on the three clips
- [ ] ≤ 2 cars/day drift over a live week (or an accepted mitigation)
- [ ] Corrections work from the command line, and the phone updates at once
- [ ] fps budget OK **on the vision host**, or an accelerator added
