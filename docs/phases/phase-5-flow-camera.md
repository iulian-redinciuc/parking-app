# Phase 5: Entry/exit camera and combining levels

**Goal:** count cars entering and leaving with Camera A, keep a correct running count for the level it covers, and combine all levels into one status.
**Needs hardware:** Camera A (and maybe the AI HAT+, decided in P5.10).
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

**Done when:** the lines file is committed and the reference frame shows the whole lane.

## P5.2: Low-latency RTSP + video file source
**Files:** `parking/vision/sources.py`

**Steps**
1. Reuse `RtspSource` (P4.3). Make sure the reader thread always returns the **newest** frame and drops the backlog.
2. `VideoFileSource` (`video:` scheme) plays at native fps, or as fast as possible with `realtime=false`, for evaluation.
3. `parking record --camera cam-ramp --minutes 60 --out data/recordings/`: save the sub-stream to MP4 with **ffmpeg stream copy** (`ffmpeg -rtsp_transport tcp -i URL -c copy -t 3600 out.mp4`), with no re-encoding.

**Done when:** a 1-hour recording plays back, and the source reports a steady fps.

## P5.3: Motion gate
**Files:** `parking/vision/motion.py`, tests

Implement [vision.md §7.2](../design/vision.md#72-motion-gate-parkingvisionmotionpy). Tests: a static synthetic scene → inactive; a moving rectangle → active; the hold-over keeps it active for 2 s after the motion stops.

**Done when:** on a recorded clip, the gate is active for < 20% of an off-peak hour (log the ratio).

## P5.4: Tracker integration
**Files:** `parking/vision/tracking.py`

**Steps**
1. Wrap `model.track(frame, persist=True, tracker="bytetrack.yaml", classes=[2,3,5,7], conf=0.4, imgsz=640)` and return `Detection`s with `track_id`.
2. When the gate goes inactive, **don't** reset the tracker straight away. Reset after 10 s inactive, to drop stale tracks.
3. Apply the ROI by masking the frame (fill outside the ROI with black) before tracking.

**Done when:** on a clip, each passing car keeps one track id from entering to leaving the frame (inspect with an annotated video export: `--debug-video out.mp4`).

## P5.5: Two-line counter
**Files:** `parking/vision/flow.py`, tests

Implement `TwoLineCounter` per [vision.md §7.3](../design/vision.md#73-two-line-counter-parkingvisionflowpy). Unit tests listed in [testing.md §2](../design/testing.md#2-what-must-have-unit-tests) (synthetic tracks, no model).

**Done when:** all tests pass.

## P5.6: Flow worker
**Files:** `parking/workers/flow_worker.py`, CLI `worker flow`

**Steps**
1. Loop: `read → gate → track → counter → publish FlowEventMsg (qos 1, uuid4 event_id)`.
2. Health every 10 s: fps over the last 10 s, `inference_ms_avg`, gate-active ratio.
3. `--debug-video PATH` writes the annotated frames (boxes, track ids, lines, running in/out) for checking.
4. Enable the compose profile: `docker compose --profile flow up -d`.

**Done when:** driving or walking a car through the lines produces exactly one event in the right direction (`mosquitto_sub -t 'parking/main/camera/cam-ramp/flow'`).

## P5.7: FlowCounter in the API + corrections
**Files:** `parking/core/flow_counter.py`, `parking/api/routes/admin.py`, `parking/api/deps.py` (bearer auth with `ADMIN_TOKEN`), migration if needed

**Steps**
1. `FlowCounter` per [vision.md §7.4](../design/vision.md#74-flow-counter-parkingcoreflow_counterpy-in-the-api): idempotent by `event_id` (keep the last 1000 IDs in memory + the DB primary key), clamp, `correct()`, `restore()`.
2. The consumer handles `flow` messages → counter → `flow_event` row → zone change → SSE.
3. `POST /api/admin/zones/{id}/correct` with `Authorization: Bearer $ADMIN_TOKEN` ([api.md §4](../design/api.md#4-admin-endpoints)). Writes a `correction` row.
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

## P5.10: Performance and the AI HAT+ decision
**Steps**
1. On the busiest clip, played in real time (`realtime=true`): record the achieved fps, CPU %, and temperature, with the occupancy worker and the Pi's other services also running.
2. If fps drops below 8 while cars pass, or CPU temp > 80 °C: try a smaller ROI or `imgsz=480` first. If still short, order the **AI HAT+** and add a `HailoDetector` behind the `Detector` protocol (the Hailo model zoo has YOLO models compiled for Hailo-8/8L).
3. Record the decision.

**Done when:** the decision is recorded with numbers.

## P5.11: Live week drift test
**Steps**
1. Correct the count to the true value on day 0.
2. Each day, at a time you can count the level (or check its occupancy camera if Option C), note true vs app value. **Don't correct** during the test.
3. Drift per day = |error change| / days.
4. If drift is above target: check for a pattern (night? queues? pedestrians?) and use a clip of that situation to fix it. Consider enabling the scheduled reset.

**Done when:** ≤ 2 cars/day of drift, or an accepted alternative (scheduled reset + display ≈).

---

## Exit criteria
- [ ] ≥ 98% event accuracy on the three clips
- [ ] ≤ 2 cars/day drift over a live week (or an accepted mitigation)
- [ ] Corrections work from the command line, and the phone updates at once
- [ ] CPU/fps budget OK, or the AI HAT+ installed
