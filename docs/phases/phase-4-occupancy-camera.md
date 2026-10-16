# Phase 4: Live occupancy camera

**Goal:** replace the replay feed with the real Camera B, reaching the accuracy targets in all conditions.
**Needs hardware:** Camera B and the **production vision host** (chosen in P4.1: the standalone box at a window, [hardware.md §4.5–§4.6](../design/hardware.md#45-chosen-in-p41-to-order); with wired access a PoE camera, switch and cabling instead).
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

**PoC check (2026-10-15):** decisions recorded (PROGRESS.md decision log, [hardware.md §4.5](../design/hardware.md#45-chosen-in-p41-to-order)): **T2**; Camera B a **Raspberry Pi Camera Module 3 Wide** (12 MP, 102°) on the box at the sample photo's window, ≥ 5 m up, ≥ 45° down, focus fixed at infinity; vision host a **Raspberry Pi 5 8 GB** + NVMe SSD; the PoE camera only if wired access is granted. Checked on images on the dev Pi: `parking evaluate --camera cam-ground --labels data/labels/cam-ground-feed.json` on the 284-frame replay feed shrunk to 1, 1/2, 1/4, 1/6, 1/8, 1/12, 1/16 of its size gives 100% (4828/4828) every time, at 1/25 99.6% (spaces 8–19 px across): the chosen camera's ≥ 60 px target has about 4× margin over the smallest size that worked. `parking benchmark --runtimes ''` on a 12 MP (3000×4000) frame: scorer 536 ms median (p95 601 ms, 200 MB peak RSS), decode 93 ms, 58 °C, so one frame every 5 s uses ~13% of a core on the vision host's hardware. Nothing was ordered (moved to go-live), and nothing was measured on a real camera or lens.

## P4.2: Install and network
**Steps**
1. Mount per the [checklist](../design/hardware.md#6-mounting-checklist). Before fixing it permanently, take a test snapshot and check every space is visible.
2. Camera VLAN on the lot's router/switch: a new network + firewall rules "camera VLAN → internet: block", "vision host → camera VLAN: allow".
3. Static IP / DHCP reservation, new strong password, firmware update, disable cloud/P2P/UPnP, privacy masks over neighbouring windows and the street.
4. Camera settings: main stream 2560×1440 (or native), H.264 (more compatible than H.265 with OpenCV), sub-stream 640×360. Turn on WDR. Time sync (NTP) and the correct timezone.
5. From the vision host: `ffprobe -rtsp_transport tcp "$CAM_GROUND_RTSP_URL"` and `curl -sf -o /tmp/s.jpg "$CAM_GROUND_SNAPSHOT_URL"`.
6. Put the URLs in the vision host's `deploy/.env`.
7. For development on the dev Pi: grab snapshots and short recordings on site (`parking record`, P5.2) and copy them to the Pi's `data/` folder. Work against `folder:`/`video:` sources; the dev Pi doesn't need to reach the live camera.

**Which steps apply.** With the default since 2026-10-10, the **standalone box** (Pi camera module on the box, [hardware.md §4.6](../design/hardware.md#46-default-from-2026-10-10-standalone-box-no-access-to-the-lots-cameras-or-network)), there is no camera network: instead of steps 2–6 the box has its own 4G link, outbound only (`provision.sh site` already denies everything inbound but SSH) and the camera check is `parking grab` ([deployment.md §4.3](../design/deployment.md#43-a-camera-plugged-into-the-vision-host-p412)). Steps 2–6 are for network cameras: an old phone as the first try, or the PoE camera if wired access is granted.

**Done when:** both commands work from the vision host and from inside its vision container (`docker compose run --rm vision-occupancy ...`).

`deploy/scripts/camera-check.sh` runs all of it, from `deploy/` with the `-f` files of `up` (and `-p` for another Compose project), and prints one PASS/FAIL line per check with any `user:pass@` replaced by `***@`:
```bash
./scripts/camera-check.sh -f docker-compose.site.yml     # T2 lot box; no -f on a one-machine install
```
For each URL in `.env` it runs, on the host, `ffprobe -rtsp_transport tcp` and `curl --anyauth` (a JPEG must come back); without FFmpeg on the host it runs the vision image's `ffprobe` with `--network host`, so the box needs no extra package. In the container (`docker compose run --rm --no-deps -T -e <URL> vision-occupancy`, entrypoint `sh`; the image has no `curl`) it runs `ffprobe` and **`parking grab --source "snapshot:$CAM_GROUND_SNAPSHOT_URL"`**, the worker's own snapshot code. The URLs only travel in environment variables, never on a command line. Not Python's `urllib`: it can't read a URL with `user:pass@` in it (it takes the login as part of the host name, `Name or service not known`).

**Trying it without a camera** (dev / rehearsal): `deploy/docker-compose.fake-camera.yml` adds `fake-camera` (`backend/scripts/fake_rtsp.py` playing an image folder as RTSP + HTTP snapshot behind a login) at a fixed address on an `internal` network that only `vision-occupancy` joins, the rehearsal's camera VLAN. Its header has the commands and the `.env` lines.

**PoC check (2026-10-15):** production rehearsal on the dev Pi (project `parking-rehearsal`: `docker-compose.site.yml` + `docker-compose.fake-camera.yml`, released image `parking-vision:v0.1.0` with the v0.1.0 `config/`, `.env` from `prod-env.sh site` plus `VPN_BIND_IP=127.0.0.1` and the camera URLs, data in `/tmp`); the camera was the 284-image replay feed (`data/replay/ground-feed`) at native size 1932×2576, H.264 2 fps, at `10.99.20.11` with a random password (RTSP basic, snapshot digest). `camera-check.sh`: **4 of 4 PASS**: host-network ffprobe and container ffprobe `h264,1932,2576`, host curl `200 image/jpeg` ~1.47 MB, container `parking grab` a 1932×2576 frame. With a wrong password all 4 FAIL (401, URL shown as `***@`), and with the camera stopped all 4 FAIL in 33 s. Camera VLAN: from the camera, 1.1.1.1:443 and 8.8.8.8:53 are unreachable and DNS doesn't resolve; a container on the egress network only can't reach the camera (time-out) but reaches the internet. The guide's earlier `urllib` command failed against the login camera (`Name or service not known`), so it was replaced. Torn down afterwards. Not checked: a real camera, its firmware/settings/privacy masks, a real router's VLAN rules, the box's 4G link (go-live-checks.md).

## P4.3: Snapshot and RTSP sources
**Files:** `parking/vision/sources.py`, tests

**Steps**
1. `SnapshotSource`: `httpx.Client` with digest and basic auth (parse credentials from the URL); 5 s timeout; decode with `cv2.imdecode`.
2. `RtspSource`: `cv2.VideoCapture(url, cv2.CAP_FFMPEG)` with env `OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp|timeout;5000000"` (FFmpeg ≥ 5, bundled with OpenCV, renamed `stimeout` to `timeout`) and 5 s open/read timeouts. A **reader thread** keeps only the latest frame (a lock + a single slot); `read()` returns it, or `None` if it's older than 5 s.
3. Reconnect with exponential backoff (1, 2, 4 … 60 s), logging each attempt. Report `connect_failed` to health.
4. Prefer `snapshot:` for occupancy (lower CPU, full resolution on demand); `rtsp:` is the fallback.
5. Tests: `SnapshotSource` against a local `http.server` serving a fixture; `RtspSource` reconnect logic with a mocked `VideoCapture`.

**Done when:** the worker runs for 1 h against the real camera with no leaks (check RSS memory over time), and survives unplugging the camera for 2 min.

**PoC check (2026-10-15):** production rehearsal on the dev Pi (project `parking-rehearsal`: `docker-compose.yml` + `docker-compose.fake-camera.yml`, container names changed in a scratch override to run next to the dev stack, images built from this commit's code because the released v0.1.0 predates P4.12's `LiveSource`, `.env` from `prod-env.sh server`, data in `/tmp`, API on loopback :18000). The camera was `fake_rtsp.py` playing the 284-image ground feed at native 1932×2576 behind a login at `10.99.20.11` on the internal camera network; `cam-ground` sampled every **2 s** (production: 5 s) to put more frames through in the shortened run; RSS / threads of the worker's process, docker memory and the API's `/healthz` sampled every 10 s; the camera container stopped for 2 min at minute 8 ("unplugged": its address disappears, connections time out). **`snapshot:`** (digest auth), 21 min, 543 observations: RSS median 171 MB in minutes 1–4, 168 MB in the last 3 (max 190), 6 threads throughout; unplugged → `degraded` after ~40 s, `down` after ~70 s, backoff 1 → 60 s logged per attempt; back 33 s after the camera returned (the 60 s backoff step), `ok` again 70 s after. **`rtsp:`** (basic auth, H.264 2 fps), 33 min with two unplug cycles, 846 observations: RSS stayed in a 195–268 MB band (median 222 MB in minutes 1–4, 207 MB at the end), 7 threads throughout, the same health sequence, frames back 36 s and ~50 s after each return (the backoff step). No container restart, healthcheck healthy throughout. Found and fixed: the fake camera's stream was corrupt at 5 MP (its keyframes overflowed its own local UDP hop; plain `ffmpeg` saw the same errors), now capped at 120 KB per frame (`--max-frame-kb`), after which 40 s decoded without an error; and with any source but `snapshot:` the workers logged every POST to the API at INFO (httpx), now WARNING for all workers. Torn down afterwards. Not checked: a real camera's stream or snapshot, an hour-long run, a real cable pull, the box's `picamera:` source on a real camera module (go-live-checks.md).

## P4.4: Health reporting from the real camera
**Steps**
1. Tune the [health thresholds](../design/vision.md#5-frame-health-parkingvisionhealthpy) on real frames: record `mean`, `laplacian_var` and `frame_diff` at noon, dusk, night and in rain (log them at DEBUG for 24 h).
2. Set `blur_laplacian_min` below the night value; `black_mean_max` below the darkest valid frame.

**How (prepared on the dev Pi):** run the worker with `LOG_LEVEL=DEBUG` in the vision host's `deploy/.env` for 24 h (one `health_metrics …` line per frame), then
```bash
docker compose logs --no-color vision-occupancy > /tmp/health.log   # or the log file
docker compose run --rm --no-deps -v /tmp/health.log:/tmp/health.log \
  --entrypoint /app/backend/.venv/bin/parking vision-occupancy \
  health-stats /tmp/health.log --camera cam-ground --until <time you covered the lens>
```
Put the suggested values in `cameras[].health` in `lot.yaml` (check the night and rain rows by eye), set `LOG_LEVEL` back to `INFO`, then cover the lens and watch the `health` message turn `black`.

**Done when:** no false `blurry`/`black` during a normal day + night, and covering the lens triggers `black` within 30 s.

**PoC check (2026-10-15):** on a lit day of images on the dev Pi. `parking light-feed` made 24 h of light from the 284-image ground feed (colour day, darker/warmer/noisier dusk, IR night 18:45–06:45: grey, lit from the camera, softer; rain 14–16 h and 22–23 h; [vision.md §5](../design/vision.md#5-frame-health-parkingvisionhealthpy)), 240 frames = one every 6 simulated minutes, played by `fake_rtsp.py` as a digest-protected `snapshot:` camera (container `parking-p44-camera`, 5 s per image) to the occupancy worker on the host (`--print`, `LOG_LEVEL=DEBUG`, every 5 s): **20 min = 24 h**. Recorded at the defaults (12 / 40 / 0.5): **74 of 243** frames falsely `blurry`: every IR night hour from midnight on (lap p1 24–35) and the night rain (~11); quiet daytime frames differed by only 0.23, under `frozen_diff_max`. `parking health-stats` (log times mapped to the simulated clock in a scratch copy, so the hour rows mean simulated hours) suggested 13.6 / 5.9 / 0.117 → `cam-ground` in `lot.yaml`: `black_mean_max` 13, `blur_laplacian_min` 6, `frozen_diff_max` 0.12. Checked on a second day (another seed, from 06:00), lens covered for 45 s at simulated noon and midnight (`--cover` + SIGUSR1): **0 false issues in 219 frames** outside the covers (lowest mean 28.8 at dusk, lowest lap 8.3 in night rain, smallest frame difference 0.38); both covers read `black` on the next frame (+0.8 s) and in the health message after **10.8 s / 0.8 s** (worst case: 5 s sample + 10 s heartbeat); `ok` again ~6 s after uncovering. Not checked: a real camera's day and night (its IR, noise, rain, headlights, a dirty lens), a real hand over the lens (an IR camera's LEDs can light a hand up instead of going black), a 24 h log (go-live-checks.md).

## P4.5: Calibrate slots on the real view
**Steps**
1. Keep the image the current slots were drawn on, then grab a full-resolution frame at a busy time → `data/reference/cam-ground.jpg` (`--force` replaces the older grab; without `--source` it reads the camera's `source` from `lot.yaml`). Use the main stream's full resolution (a network camera's snapshot, the box's `picamera:`), not the RTSP sub-stream (if `source` is the sub-stream, add `--source "snapshot:<url>"`):
   ```bash
   cp ../data/reference/cam-ground.jpg ../data/reference/cam-ground-old.jpg
   docker compose run --rm --no-deps --entrypoint /app/backend/.venv/bin/parking vision-occupancy \
     grab --camera cam-ground --force
   ```
2. Draft the slot file, then refine it in the slot editor (`tools/slot-editor`, or the admin screen's slot editor) → `config/slots/cam-ground.json`:
   - **Same place as an existing slot file** (the real camera at the sample photo's window, a camera that was moved or replaced): carry the old spaces over ([vision.md §4](../design/vision.md#4-bootstrapping-slots-parking-bootstrap-slots)). The two views are matched and every polygon moves through the ground plane's homography; ids, zones and types stay:
     ```bash
     docker compose run --rm --no-deps --entrypoint /app/backend/.venv/bin/parking vision-occupancy \
       bootstrap-slots --image data/reference/cam-ground.jpg --camera cam-ground \
       --carry-from config/slots/cam-ground.json --carry-ref data/reference/cam-ground-old.jpg --force
     ```
     It refuses (nothing written) when the views don't match (another place, a black frame); then draw by hand.
   - **A new place, angled view:** `parking bootstrap-slots --image data/reference/cam-ground.jpg --camera cam-ground` drafts a slot per detected car; add the empty spaces by hand. (Seen from above the detector finds almost no cars: draw by hand.)
3. Check the polygons cover the **ground area of each space** (where tyres are), not the space's air above. Add spaces the old view didn't have; delete ones now out of view (the command lists spaces it had to clip at the image edge).
4. **Empty-lot reference** (`occupancy.appearance.reference_empty`) for the new view: grab a quiet frame (few cars), label it in the slot editor (*Label* mode), and let `simulate-feed` paint its cars out:
   ```bash
   parking simulate-feed --camera cam-ground --base data/reference/cam-ground-quiet.jpg \
     --labels data/labels/cam-ground.json --frames 1 --out out/sim --labels-out out/sim.json \
     --empty-reference data/reference/cam-ground-empty.jpg
   ```
5. Commit the slot file. Restart the worker (or send `reload`; saving in the admin slot editor does both the write and the reload). The worker's camera-shift check uses the new `data/reference/cam-ground.jpg` from then on.

**Done when:** `parking analyze` on a live snapshot (`--image` of a fresh grab) gives the hand count.

**PoC check (2026-10-15):** production rehearsal on the dev Pi (project `parking-rehearsal`: `docker-compose.yml` + `docker-compose.fake-camera.yml`, the dev Pi's locally built images with this commit's code mounted and container names changed in a scratch override, `.env` from `prod-env.sh server`, `config/` and data in `/tmp`, API on loopback :18000). The "real view" was a camera **mounted differently from the sample photo**: six replay-feed frames (simulated, the real `ground-02` photo, a generated image; 2–14 of 17 taken) warped by one fixed homography (turned 4°, top edge 7% narrower, zoomed out 8%, moved; 1944×2592), served by `fake_rtsp.py` as a digest-protected snapshot at `10.99.20.11`, 25 s per image; `cam-ground.source` = `snapshot:${CAM_GROUND_SNAPSHOT_URL}`. **Before:** with the committed slot file and reference frame (the sample photo's view) the worker flagged `shifted` (88.1 px, `degraded`) after its 3 checks, and `parking evaluate` on the six frames scored 78.4% of spaces right (count within 1 on 2 of 6). **Steps 1–5:** grab in the vision container (1944×2592, the 14-taken frame, old reference kept as `cam-ground-old.jpg`); `bootstrap-slots --carry-from` (new, [vision.md §4](../design/vision.md#4-bootstrapping-slots-parking-bootstrap-slots)): 883 of 902 matches agree, 17 spaces within **1.8 px** of the true warped polygons (IoU ≥ 0.978), checked by eye on an overlay: each covers its painted bay, so nothing was moved by hand; for comparison the detector draft (`--footprint box`) found 1 slot for the 14 cars; empty-lot reference re-made with `simulate-feed` from the 2-taken frame; saved with the admin `PUT …/slots` → `reloaded: true`. **After:** `parking evaluate` 100% (102/102) on the six frames (also 100% with the old, misframed empty reference); **Done when:** three fresh grabs at other moments → `parking analyze` **15 / 12 / 5 free = the hand count** (2, 5, 12 taken), every space's taken/free as labelled; the API's `ground` count at the end of each image shown = the hand count for **12 of 12** images over 5 min after the reload, health back to `ok` (shift check restarted on the new reference). Torn down afterwards. The committed `config/slots/cam-ground.json` is unchanged: it still matches the dev replay feed. Not checked: a real camera's view (a real lens, a mount that also sees things the photo didn't, a lot that isn't one flat plane), drawing by hand in the editor, a hand count at the lot (go-live-checks.md).

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

**Implementation:** `parking/workers/debug_capture.py`, spec in [vision.md §6.1](../design/vision.md#61-debug-frame-capture-parkingworkersdebug_capturepy). N = `DEBUG_CAPTURE_EVERY_MIN` (10). On the vision host: set `DEBUG_CAPTURE=true` in `deploy/.env`, `docker compose up -d vision-occupancy`, and the files appear under `data/debug/cam-ground/`.

## P4.8: Build the validation set
**Steps**
1. Turn on debug capture for ~1–2 weeks on the vision host: `DEBUG_CAPTURE=true`, `DEBUG_RETENTION_HOURS=336` (so nothing expires before the pick; back to 24 afterwards) in `deploy/.env`.
2. Pick ~200 frames spread over: morning/noon/evening/night, dry/rain, empty/busy/full, plus any hard cases (low sun glare, snow). `parking validation pick --camera cam-ground [--count 200] [--dry-run]` does the spread: strata of lot-local time of day (night 21–06, morning 06–11, noon 11–15, evening 15–21) × occupancy from the capture's observation (empty ≤ 20 % taken, full ≥ 90 %), quotas shared evenly, evenly spaced in time inside each stratum. Add rainy/glare frames by hand if the pick missed them.
3. The pick copies them to `data/validation/cam-ground/` (outside the retention-pruned folder) as `<date>_<HHMMSS>_<ms>.jpg`. Label them with the slot editor's label mode → `data/labels/cam-ground-validation.json`, tagging conditions (`morning|noon|evening|night`, `dry|rain`, `empty|busy|full`, plus `glare`, `snow`, … for hard cases). `parking validation check --camera cam-ground` prints the per-tag counts and exits 1 until the target below is met.
4. Blur faces/plates if you keep these longer than the retention policy allows (OpenCV Gaussian blur on boxes detected as `person`, plus manual plates): `parking validation blur --camera cam-ground [--boxes plates.json] [--dry-run]` blurs YOLO `person` boxes and the hand-drawn boxes (`{"<image>.jpg": [[x1, y1, x2, y2], ...]}`, read the pixel corners off the slot editor or any image viewer) in place, each grown 15 %; `data/validation/cam-ground/blurred.json` records what was done, so it can be re-run after adding frames or boxes.

**Done when:** ≥ 200 labelled frames covering every condition tag at least 15 times (`parking validation check --camera cam-ground` prints `ok`; hard-case tags are optional, see the decision log).

**PoC check (2026-10-15):** on images on the dev Pi. `parking validation timelapse --camera cam-ground --days 14 --every-min 20 --start 2026-10-01` turned the 284 labelled replay frames into **14 days of debug captures** (1008 frames, one every 20 min, lit for their hour: colour day, dusk, IR night, 22 rainy spells at any hour; `FrameHealth` with the P4.4 values, the camera's `appearance` analysis and the worker's `DebugCapture` at simulated times; 0 unhealthy, 1008 captured, ~17 min). `parking validation pick --camera cam-ground --labels-from data/labels/cam-ground-timelapse.json` picked **200** (20 in each of the 10 strata it found) and labelled them from the known truth instead of a person; `parking validation check --camera cam-ground` printed **`ok`**: morning 60, noon 60, evening 60, night 20, dry 178, rain 22, empty 76, busy 95, full 29 (+ dusk 24, simulated 200). Before the labels it exited 1. `parking validation blur` on the 200: 0 `person` boxes (top-down cars, no people), all 200 marked checked; on Ultralytics' `bus.jpg` it found the 4 people and blurred them plus a hand-drawn plate box (Laplacian variance inside 2400–5200 → 1–2, outside unchanged). Seen on the way (for P4.9): the worker's own guesses on the 1008 captures were right for 68.6% of spaces: noon 99.9%, morning 80.8%, evening 74.2%, **night 44.3%** (IR frames read every space as taken against the colour empty-lot reference), so the pick, which goes by the worker's count, saw every night frame as `full` and gave night only 20 picks. Not checked: real frames, real weather, a person labelling in the slot editor, real people or plates to blur (go-live-checks.md).

## P4.9: Evaluate and tune
**Steps**
1. `parking evaluate --camera cam-ground --images data/validation/cam-ground --labels data/labels/cam-ground-validation.json --sweep 0.1:0.6:0.05 --mode both` (with `occupancy.method: appearance`, `--mode` is ignored and the sweep re-thresholds the appearance scores). The overall line and the sweep's best threshold each end with a `target (…): met | missed (…)` line (the Done-when below).
2. Read the per-condition breakdown. The usual fixes, in order:
   - wrong threshold → sweep
   - small cars missed → `imgsz 1280` / tiling
   - night misses → raise exposure or IR in the camera settings, or lower `conf` at night (config by time of day)
   - polygons slightly off → recalibrate
3. Record the numbers in Metrics and the decisions in the log.

**Done when:** slot accuracy ≥ 97% and count error ≤ 1 on ≥ 95% of frames, **or** you decide P4.10 is needed.

**PoC check (2026-10-15):** on images on the dev Pi, on P4.8's 200 PoC validation frames (lit copies of the sample scene: colour day, dusk, IR night, rain; labels from the known truth). Step 1's command at first: slot accuracy **85.4%**, count error ≤ 1 on 73.5%, **`target … missed`**; the sweep didn't help (best 85.7% at 0.20). The breakdown: noon 99.7%, but dusk 68.6% (frames at ~1/5 of the light, too dark for the scorer's light matching) and **night 46.8%** (IR: every space taken against the colour reference). Not a detector problem (`method: appearance`), so the fix went into the scorer (vision.md §2.1 step 2): the frame is relit to `reference_empty` with a smooth per-channel gain fitted on the background outside the spaces (a grey reference for IR frames), and the shadow rule is off in grey frames (every darker car passed for a shadow). After: **99.7%** (3390/3400), count error ≤ 1 on **99.5%**, **`target … met`** at 0.30 (also the sweep's best; 0.25–0.35 all ≥ 99.6%); night 98.5%, rain 99.2%, dusk 100%. On all 1008 timelapse frames (the 200 included): 99.0%, count error ≤ 1 on 98.5%, met; rain is the weakest there (96.6%, count ≤ 1 on 83.3% of 84 frames). The 284-frame replay feed (daytime) stays 4828/4828. So P4.10 isn't needed on the PoC set. Left: G14 called taken on 6 empty IR frames, one rainy evening frame with 3 cars missed. Not checked: real frames, a real camera's IR and rain, real labelling (go-live-checks.md).


## P4.10: Per-slot classifier (only if P4.9 misses the target)
**Files:** `backend/parking/vision/slot_classifier.py`, `backend/scripts/train_slot_classifier.py`

**Steps:** follow [vision.md §9](../design/vision.md#9-per-slot-classifier). Train on a laptop or desktop with a GPU (or Google Colab), not on the vision host; the dev Pi's CPU manages a 200-frame set (~75 s per epoch, measured in P4.10's PoC check), not public pre-training. Export ONNX into `models/`. Add `classifier` and `ensemble` to `occupancy.method` (it replaces `appearance` on top-down views). Re-evaluate on the **same** validation set; keep 20% of frames held out from fine-tuning.

The code, the methods and the training script are in place (prepared while P4.9 was blocked); what's left needs P4.8's labelled frames:

1. On the GPU machine, from `backend/` (copy `data/validation/cam-ground/` and `data/labels/cam-ground-validation.json` over; never commit them):
   `uv sync --extra vision && uv run --with onnx python scripts/train_slot_classifier.py --camera cam-ground --images ../data/validation/cam-ground --labels ../data/labels/cam-ground-validation.json --out ../models/slot_classifier.onnx`
   (optionally pre-train first with `--crops <PKLot/CNRPark crops>` and fine-tune with `--init ../models/<pre-trained>.pt --lr 3e-4`). Copy `models/slot_classifier.{onnx,json,holdout.json}` to the vision host's and dev Pi's `models/`.
2. Compare on the held-out frames only: `parking evaluate --camera cam-ground --images data/validation/cam-ground --labels models/slot_classifier.holdout.json --method M --sweep 0.1:0.9:0.05` for `M` = `appearance`, `classifier`, `ensemble` (add `--classifier FILE` to try another model).
3. Set the winner as `occupancy.method` (and `classifier.threshold` if the sweep says so) in `config/lot.yaml`, `reload` the worker, record the numbers in PROGRESS.md Metrics.

**Done when:** targets are met, or the gap and next steps are documented.

**PoC check (2026-10-15):** on images on the dev Pi, on P4.8's 200 PoC validation frames. P4.9 already met the target with `appearance` (re-run: 99.7%, count error ≤ 1 on 99.5%, `target … met`), so the classifier isn't needed and `occupancy.method` stays `appearance`. To show the path works end to end and to have the comparison, step 1 was run on the dev Pi's CPU (`--device cpu --epochs 6`, ImageNet start, no public pre-training, ~75 s per epoch, ~8 min, output to a scratch folder, not `models/`): 2839 training crops (1254 taken), **33 frames / 561 crops held out**, best held-out crop accuracy 99.8%. Step 2 on the held-out frames only (`--sweep 0.1:0.9:0.05`): `appearance` **99.1%** (556/561, count error ≤ 1 on 97.0%; misses: G14 on 2 empty IR frames, 3 cars in one rainy evening frame), `classifier` **99.8%** (560/561, count ≤ 1 on 100%, best threshold 0.50), `ensemble` **99.8%** (same frame, G12). All three meet the target. Step 3: `appearance` stays the winner in `config/lot.yaml`: it needs no model file (the public repo can't ship one, `models/` is ignored) and the classifier only learned the five simulated cars of one scene, so its 0.7-point edge on 33 frames says nothing about a real camera. Not checked: a classifier trained on real frames, public pre-training (PKLot/CNRPark), training on a GPU machine (go-live-checks.md).

## P4.11: Soak test (on the vision host)
**Steps**
1. On the **vision host at the lot**, run the full stack (API + occupancy worker) from the same compose files. This is temporary: the production API moves to its final machine in Phase 8 if the topology is T2/T3. For phone testing, expose it with the tunnel container under a test hostname, and point the Pages preview at it.
2. Run for 7 days with the Phase 3 app in use.
3. Each day, glance at the app against reality once or twice and note any mismatch in PROGRESS.md.
4. Watch for memory growth, CPU temperature, reconnects, and stale periods, and record the vision host's speed in Metrics. Run the sampler on the vision host for the whole week ([testing.md §7](../design/testing.md#7-soak-test-p411-on-the-vision-host)):
   ```bash
   mkdir -p out/soak && nohup python3 backend/scripts/soak.py sample --out out/soak/soak.jsonl --interval 60 > out/soak/sampler.log 2>&1 &
   python3 backend/scripts/soak.py report out/soak/soak.jsonl   # any time; the verdict after 7 days
   ```

**Done when:** 7 days with no unrecovered outage and no memory growth (`soak.py report` says `verdict: PASSED`, exit 0).

**PoC check (2026-10-15):** a **21-minute** soak in a production rehearsal on the dev Pi (the vision host's hardware: Pi 5 8 GB) instead of 7 days at the lot: project `parking-rehearsal` (`docker-compose.yml` + `docker-compose.fake-camera.yml`, container names `parking-rh-*` in a scratch override to run next to the dev stack, locally built images with this commit's code, `.env` from `prod-env.sh server`, config and data in `/tmp`, API on loopback :18000). The camera was `fake_rtsp.py` serving the 284-image replay feed as a digest-protected snapshot (30 s per image), `cam-ground` every 5 s. "The app in use": 8 emulated phones doing what the frontend does (`/api/lots`, `/api/lot`, `/api/status`, `/api/maps/ground`, `/api/history`, then `/api/stream` for 1–3 min, round and round): 564 requests, 860 SSE events, 72 connection errors (all `URLError`, no HTTP error; the API was restarted once). "Glances" every ~90 s, once an image had been shown ≥ 25 s: the API's ground spaces vs that image's labels: **7 of 8 right**; the miss was 10 s after the camera came back (pre-outage count still shown as fresh until smoothing's 3 readings; recorded in the decision log), right again 2 s later. Incidents compressed into the run: camera gone 2 min at minute 6, worker process `kill -9` at minute 12, API restart at minute 15. `soak.py sample --interval 15 --duration 1260 --containers parking-rh-api,parking-rh-vision-occupancy,parking-rh-autoheal`, then `soak.py report … --days 0.0139 --interval 15 --warmup-min 3 --window-h 0.1` (new options: the memory windows scaled to the run): **`verdict: PASSED`, exit 0**: 85 samples; 3 recovered outages (camera `down` 2.8 min, zone `ground` stale 2.0 min, API unreachable 0.2 min); the crashed worker restarted by Docker within one sample (1 restart); 1 reconnect; memory API 109.5 → 123.2 MB, worker 145.0 → 162.3 MB (max 203.8), both within the 25 MB allowance; 54.0–57.3 °C, never throttled, load ≤ 1. `cam-ramp` / `underground` listed as never ok (no ramp camera). Torn down afterwards. Not checked: 7 days, a real camera and the lot's day/night, heat in a case at a window, the box's 4G link, real phones (go-live-checks.md).

## P4.12: Camera plugged into the vision host (`device:` source)
**Files:** `backend/parking/vision/sources.py`, `deploy/docker-compose.device.yml`, `deploy/docker-compose.picamera.yml`, `deploy/Dockerfile.picamera`, `docs/design/config.md` (source formats), `docs/design/hardware.md`, `docs/design/deployment.md` §4.3, tests

**Why:** the lot's existing cameras and the building's network are **not available** (2026-10-10 decision), so the default hardware is a **standalone box**: a Raspberry Pi with its own camera at a window overlooking the lot and its own mobile internet (hardware.md §4.6). That camera is not a network camera, so it needs its own source.

**Steps**
1. `device:` source: `device:/dev/video0?width=3840&height=2160&fourcc=MJPG` (USB/UVC webcams and anything V4L2) via OpenCV; and `picamera:0?width=4608&height=2592` for the Raspberry Pi camera modules through `rpicam-still`/`rpicam-vid` (libcamera), since they don't appear as plain V4L2 capture devices. Latest-frame behaviour and reconnects as for `rtsp:`; clear errors when the device is missing or busy.
2. Tests with a fake capture object (no hardware in CI); a manual check command (`parking grab --camera …` already exists) documented for the real device.
3. Compose: how the `vision-occupancy` container gets the device (`devices:` entry, `video` group) as an opt-in override file, not in the base file.
4. hardware.md §4.6 and config.md updated with the formats and the tested cameras.

**Done when:** tests pass and the docs say how to run the worker from a plugged-in camera. (The check on a real camera happens when the box exists; a USB webcam on the dev Pi is enough if one is ever plugged in.)

**Implementation:** `LiveSource` (the reader thread `rtsp:` already had) with `RtspSource`, `DeviceSource` and `PicameraSource` on it; formats and options in [config.md → Source URI formats](../design/config.md#source-uri-formats). `picamera:` reads MJPEG from one long-running `rpicam-vid` (not `rpicam-still` per sample: exposure and focus stay settled). Containers: `deploy/docker-compose.device.yml` and `deploy/docker-compose.picamera.yml` + `deploy/Dockerfile.picamera`, [deployment.md §4.3](../design/deployment.md#43-a-camera-plugged-into-the-vision-host-p412). Tests: `tests/unit/test_device_sources.py` (fake V4L2 capture, a script standing in for `rpicam-vid`), `test_compose_hardening.py`.

**When a camera is plugged in** (not done yet: there is none on the dev Pi):
```bash
cd backend
uv run parking grab --camera cam-ground --source "device:/dev/video0?width=3840&height=2160&fourcc=MJPG" --out ../out/grab.jpg --force
uv run parking grab --camera cam-ground --source "picamera:0?width=4608&height=2592&focus=0" --out ../out/grab.jpg --force
```
It prints the frame's size, or the reason there is none. Then set that URI as `cam-ground`'s `source` in `config/lot.yaml`, start the worker with the matching override file (deployment.md §4.3), unplug the camera for a minute (`connect_failed`, then back by itself), and fill in the *Tested* column of [hardware.md §4.6](../design/hardware.md#46-default-from-2026-10-10-standalone-box-no-access-to-the-lots-cameras-or-network).

---

## Exit criteria
- [ ] Space accuracy ≥ 97% on the validation set (all conditions)
- [ ] Count within ±1 on ≥ 95% of frames
- [ ] 7-day soak passed
- [ ] Shift detection and health alerts verified
- [ ] Privacy items for the camera done (signage, masks, retention)
