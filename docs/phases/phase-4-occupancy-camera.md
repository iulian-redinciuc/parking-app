# Phase 4: Live occupancy camera

**Goal:** replace the replay feed with the real Camera B, reaching the accuracy targets in all conditions.
**Needs hardware:** Camera B, PoE switch, cabling, and the **production vision host** (or, temporarily, any machine on the camera network that can record).
**Where the work happens:** code is developed and tested on the **dev Pi** using recordings and snapshots from the real camera. The live soak test runs on the vision host at the lot.
**Specs used:** [hardware.md](../design/hardware.md), [vision.md §2–3, §5–6, §9–10](../design/vision.md), [config.md](../design/config.md), [security-privacy.md §4](../design/security-privacy.md#4-privacy-and-gdpr-checklist).

## Deliverables
- Camera installed, isolated on its own network, streaming to the worker
- Slots calibrated on the real view; shift detection active
- A ~200-frame validation set with measured accuracy
- 7-day soak test passed

---

## P4.1: Choose the production layout, the camera and the vision host
**Steps**
1. **Choose the production topology** T1 / T2 / T3 ([deployment.md §3](../design/deployment.md#3-production-topologies-to-be-chosen)), which answers open question #2. This decides where vision runs in production.
2. Use the Phase 1 findings (angle, distance, occlusion) and [hardware.md §2](../design/hardware.md#2-cameras) to choose the camera's resolution, lens and mounting height.
3. Choose the **vision host** for the lot ([hardware.md §4.2](../design/hardware.md#42-production-vision-host-at-the-lot-topologies-t1t2)). If unsure between candidates, buy one first and benchmark it on recorded clips (P5.2) before committing.
4. Record the topology, camera model, lens, mount position and vision host in the Decision log.

**Done when:** the decisions are recorded and the hardware is ordered.

## P4.2: Install and network
**Steps**
1. Mount per the [checklist](../design/hardware.md#6-mounting-checklist). Before fixing it permanently, take a test snapshot and check every space is visible.
2. Camera VLAN on the lot's router/switch: a new network + firewall rules "camera VLAN → internet: block", "vision host → camera VLAN: allow".
3. Static IP / DHCP reservation, new strong password, firmware update, disable cloud/P2P/UPnP, privacy masks over neighbouring windows and the street.
4. Camera settings: main stream 2560×1440 (or native), H.264 (more compatible than H.265 with OpenCV), sub-stream 640×360. Turn on WDR. Time sync (NTP) and the correct timezone.
5. From the vision host: `ffprobe -rtsp_transport tcp "$CAM_GROUND_RTSP_URL"` and `curl -sf -o /tmp/s.jpg "$CAM_GROUND_SNAPSHOT_URL"`.
6. Put the URLs in the vision host's `deploy/.env`.
7. For development on the dev Pi: grab snapshots and short recordings on site (`parking record`, P5.2) and copy them to the Pi's `data/` folder. Work against `folder:`/`video:` sources; the dev Pi doesn't need to reach the live camera.

**Done when:** both commands work from the vision host and from inside its vision container (`docker compose run --rm vision-occupancy ...`).

## P4.3: Snapshot and RTSP sources
**Files:** `parking/vision/sources.py`, tests

**Steps**
1. `SnapshotSource`: `httpx.Client` with digest and basic auth (parse credentials from the URL); 5 s timeout; decode with `cv2.imdecode`.
2. `RtspSource`: `cv2.VideoCapture(url, cv2.CAP_FFMPEG)` with env `OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp|stimeout;5000000"`. A **reader thread** keeps only the latest frame (a lock + a single slot); `read()` returns it, or `None` if it's older than 5 s.
3. Reconnect with exponential backoff (1, 2, 4 … 60 s), logging each attempt. Report `connect_failed` to health.
4. Prefer `snapshot:` for occupancy (lower CPU, full resolution on demand); `rtsp:` is the fallback.
5. Tests: `SnapshotSource` against a local `http.server` serving a fixture; `RtspSource` reconnect logic with a mocked `VideoCapture`.

**Done when:** the worker runs for 1 h against the real camera with no leaks (check RSS memory over time), and survives unplugging the camera for 2 min.

## P4.4: Health reporting from the real camera
**Steps**
1. Tune the [health thresholds](../design/vision.md#5-frame-health-parkingvisionhealthpy) on real frames: record `mean`, `laplacian_var` and `frame_diff` at noon, dusk, night and in rain (log them at DEBUG for 24 h).
2. Set `blur_laplacian_min` below the night value; `black_mean_max` below the darkest valid frame.

**Done when:** no false `blurry`/`black` during a normal day + night, and covering the lens triggers `black` within 30 s.

## P4.5: Calibrate slots on the real view
**Steps**
1. Grab a full-resolution frame at a busy time → `data/reference/cam-ground.jpg`.
2. `parking bootstrap-slots` → refine in the slot editor → `config/slots/cam-ground.json`.
3. Check the polygons cover the **ground area of each space** (where tyres are), not the space's air above.
4. Commit the slot file. Restart the worker (or send `reload`).

**Done when:** `parking analyze` on a live snapshot (`--image` of a fresh grab) gives the hand count.

## P4.6: Camera shift detection
**Files:** `parking/vision/shift.py`, tests, worker integration

**Steps:** implement [vision.md §6](../design/vision.md#6-camera-shift-detection-parkingvisionshiftpy). Use only background regions (the inverse of the slot polygons). Report `shifted` in health. Handle the `save_reference` command.

**Done when:** the unit tests pass, and physically nudging the camera (or replaying a shifted test image) raises `shifted` within 15 min.

## P4.7: Debug frame capture
**Steps**
1. When `DEBUG_CAPTURE=true`: save a frame + its observation JSON every N minutes and on every slot flip, into `data/debug/<camera>/<date>/`.
2. A pruning thread deletes files older than `DEBUG_RETENTION_HOURS`.
3. Off by default. Note it in the privacy checklist.

**Done when:** captures appear when enabled and disappear after the retention period (test with 1 h).

## P4.8: Build the validation set
**Steps**
1. Turn on debug capture for ~1–2 weeks (or write a small `parking grab --every 15m --days 7` command).
2. Pick ~200 frames spread over: morning/noon/evening/night, dry/rain, empty/busy/full, plus any hard cases (low sun glare, snow).
3. Move them to `data/validation/cam-ground/` (outside the retention-pruned folder). Label them with the slot editor's label mode → `data/labels/cam-ground-validation.json`, tagging conditions.
4. Blur faces/plates if you keep these longer than the retention policy allows (OpenCV Gaussian blur on boxes detected as `person`, plus manual plates).

**Done when:** ≥ 200 labelled frames covering every condition tag at least 15 times.

## P4.9: Evaluate and tune
**Steps**
1. `parking evaluate --images data/validation/cam-ground --labels … --sweep … --mode both`.
2. Read the per-condition breakdown. The usual fixes, in order:
   - wrong threshold → sweep
   - small cars missed → `imgsz 1280` / tiling
   - night misses → raise exposure or IR in the camera settings, or lower `conf` at night (config by time of day)
   - polygons slightly off → recalibrate
3. Record the numbers in Metrics and the decisions in the log.

**Done when:** slot accuracy ≥ 97% and count error ≤ 1 on ≥ 95% of frames, **or** you decide P4.10 is needed.

## P4.10: Per-slot classifier (only if P4.9 misses the target)
**Files:** `parking/vision/slot_classifier.py`, `scripts/train_slot_classifier.py`

**Steps:** follow [vision.md §9](../design/vision.md#9-fallback-per-slot-classifier-only-if-phase-4-accuracy--target). Train on a laptop or desktop with a GPU (or Google Colab), not on the dev Pi or the vision host. Export ONNX into `models/`. Add `occupancy.mode: classifier | ensemble`. Re-evaluate on the **same** validation set; keep 20% of frames held out from fine-tuning.

**Done when:** targets are met, or the gap and next steps are documented.

## P4.11: Soak test (on the vision host)
**Steps**
1. On the **vision host at the lot**, run the full stack (API + occupancy worker) from the same compose files. This is temporary: the production API moves to its final machine in Phase 8 if the topology is T2/T3. For phone testing, expose it with the tunnel container under a test hostname, and point the Pages preview at it.
2. Run for 7 days with the Phase 3 app in use.
3. Each day, glance at the app against reality once or twice and note any mismatch in PROGRESS.md.
4. Watch for memory growth, CPU temperature, reconnects, and stale periods, and record the vision host's speed in Metrics.

**Done when:** 7 days with no unrecovered outage and no memory growth.

---

## Exit criteria
- [ ] Space accuracy ≥ 97% on the validation set (all conditions)
- [ ] Count within ±1 on ≥ 95% of frames
- [ ] 7-day soak passed
- [ ] Shift detection and health alerts verified
- [ ] Privacy items for the camera done (signage, masks, retention)
