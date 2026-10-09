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

## P5.3: Motion gate
**Files:** `parking/vision/motion.py`, tests

Implement [vision.md §7.2](../design/vision.md#72-motion-gate-parkingvisionmotionpy). Tests: a static synthetic scene → inactive; a moving rectangle → active; the hold-over keeps it active for 2 s after the motion stops.

As built: `motion_min_area_px` is in pixels of the line file's `image_size`; details in vision.md §7.2. To check an off-peak hour of a P5.2 recording:

```bash
parking motion-check --camera cam-ramp --source "video:data/recordings/cam-ramp-<date-time>.mp4?realtime=false"
```

**Done when:** on a recorded clip, the gate is active for < 20% of an off-peak hour (log the ratio): `motion-check` prints the share and exits 0.

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

## P5.7: FlowCounter in the API + corrections
**Files:** `parking/core/flow_counter.py`, `parking/api/routes/admin.py`, `parking/api/deps.py` (bearer auth with `ADMIN_TOKEN`), migration if needed

**Steps**
1. `FlowCounter` per [vision.md §7.4](../design/vision.md#74-flow-counter-parkingcoreflow_counterpy-in-the-api): idempotent by `event_id` (keep the last 1000 IDs in memory + the DB primary key), clamp, `correct()`, `restore()`.
2. `/internal/flow-events` → `ingest.py` → counter → `flow_event` row → zone change → SSE.
3. `POST /api/admin/zones/{id}/correct` with `Authorization: Bearer $ADMIN_TOKEN` ([api.md §4](../design/api.md#4-admin-endpoints)). Writes a `correction` row. *(Already built in P7.4, with the restore of the confidence counters.)*
4. Optional scheduled reset (`zones[].reset`) via APScheduler.
5. Tests: duplicates ignored; clamp sets `applied=false`; correction; restore after restart.

**Done when:** `curl -X POST -H "Authorization: Bearer …" -d '{"occupied":37}' …/correct` updates the phone at once.

## P5.8: Fusion update and confidence
**Steps**
1. Wire flow zones into `StateStore` with the confidence formula from [vision.md §8](../design/vision.md#8-fusion-confidence-trend-parkingcorefusionpy).
2. **Option B** layouts (entrance camera counts the whole lot): add zone method `derived` with `formula: total_flow - ground`, if Option B was chosen. Otherwise skip this.
3. The frontend already shows "≈" below 0.8. Check it.

**Done when:** the underground card shows ≈ after enough events without a correction, and a correction removes it.

## P5.9: Test clips and flow evaluation
**Files:** `tools/flow-tally/index.html`, `parking/vision/evaluate.py` (flow part), CLI `evaluate-flow`

**Steps**
1. Record three 1-hour clips: weekday rush hour, night, and a quiet period (`parking record`).
2. **Tally tool** (static page): open a local video, play it at 1–4× speed, press **I** for in and **O** for out (and **U** to undo) → export CSV in the [labels format](../design/config.md#flow-datalabelsclipcsv).
3. `parking evaluate-flow --video data/recordings/x.mp4 --truth data/labels/x.csv --camera cam-ramp` runs the full pipeline on the file and prints TP/FP/FN, event accuracy and net error ([vision.md §10](../design/vision.md#flow-metrics-per-clip)).
4. Tune `conf`, `min_track_frames`, line positions and ROI. Re-run all three clips after each change and keep a table in Metrics.

**Done when:** ≥ 98% event accuracy on all three clips, or the gap is understood (e.g. queued cars at the barrier) with a plan.

## P5.10: Performance on the vision host and the accelerator decision
**Steps**
1. On the **vision host** (not the dev Pi), with the runtime for that machine ([vision.md §11](../design/vision.md#11-runtimes-and-performance)), play the busiest clip in real time (`realtime=true`) with the occupancy worker also running. Record the achieved fps, CPU %, and temperature.
2. If fps drops below 8 while cars pass, or the CPU runs hot: try a smaller ROI or `imgsz=480` first. If still short, add the accelerator that fits that machine: an **AI HAT+** with a `HailoDetector` on a Pi, or a faster runtime / GPU elsewhere. The `Detector` protocol keeps this a contained change.
3. The dev Pi's numbers are only a worst-case reference. Record the vision host's numbers and the decision.

**Done when:** the decision is recorded with numbers.

## P5.11: Live week drift test
**Steps**
1. Correct the count to the true value on day 0.
2. Run on the vision host (same temporary setup as P4.11). Each day, at a time you can count the level (or check its occupancy camera if Option C), note true vs app value. **Don't correct** during the test.
3. Drift per day = |error change| / days.
4. If drift is above target: check for a pattern (night? queues? pedestrians?) and use a clip of that situation to fix it. Consider enabling the scheduled reset.

**Done when:** ≤ 2 cars/day of drift, or an accepted alternative (scheduled reset + display ≈).

---

## Exit criteria
- [ ] ≥ 98% event accuracy on the three clips
- [ ] ≤ 2 cars/day drift over a live week (or an accepted mitigation)
- [ ] Corrections work from the command line, and the phone updates at once
- [ ] fps budget OK **on the vision host**, or an accelerator added
