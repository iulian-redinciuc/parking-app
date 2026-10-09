# Computer vision

All numbers below (thresholds, timings) are **starting values** to be tuned with real data. Each tuning result goes in PROGRESS.md → Decision log.

## 1. Detector

### Interface (`parking/vision/detector.py`)

```python
@dataclass(frozen=True)
class Detection:
    cls: str                        # "car" | "motorcycle" | "bus" | "truck"
    conf: float
    box: tuple[float, float, float, float]   # x1, y1, x2, y2 in frame pixels
    mask: np.ndarray | None = None  # polygon (N×2 float32) in frame pixels, if use_masks
    track_id: int | None = None     # set by the tracker (flow only)

class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> list[Detection]: ...

class YoloDetector:
    def __init__(self, model_path: str, imgsz: int, conf: float,
                 classes: list[str], use_masks: bool): ...
    def detect(self, frame: np.ndarray) -> list[Detection]: ...
```

- Model: **YOLO11n** (boxes) or **YOLO11n-seg** (boxes + masks), COCO-pretrained.
- COCO class IDs: car = 2, motorcycle = 3, bus = 5, truck = 7. Pass `classes=[2, 3, 5, 7]` to the model so it skips everything else (people, bicycles).
- Export once per machine with `parking models export --runtime <runtime>`, which wraps `YOLO(name).export(format=<runtime>, imgsz=…)`, into `models/` (git-ignored). The runtime depends on the machine's hardware: see §11. Load the exported model with `YOLO(path, task="detect"|"segment")`.
- Masks come from `result.masks.xy` (already in original frame pixels).
- `FakeDetector(json_path, conf=0.0, classes=…)` (tests, CI, `PARKING_FAKE_DETECTOR=1`) ignores the frame and returns the detections in a JSON sidecar, after the same class/confidence filter as `YoloDetector`:
  ```json
  {"detections": [{"cls": "car", "conf": 0.91, "box": [x1, y1, x2, y2], "mask": [[x, y], …]}]}
  ```
  `mask` is optional. Masks with fewer than 3 points become `None`.
- `parking models export --model <name> --imgsz <n> --runtime ncnn|openvino|onnx|engine [--out models]` downloads `<out>/<name>.pt` and exports next to it. The `vision` extra installs **CPU-only** PyTorch from the PyTorch CPU index (`[tool.uv.sources]` in `backend/pyproject.toml`) and `pnnx` (needed by the NCNN export). The task (`segment`/`detect`) is taken from the model name (`*-seg`).
- **Viewpoint:** COCO-pretrained models recognise cars seen from the side or at an angle. On the Phase 1 sample photo, taken from high up looking **straight down**, YOLO11n/s/m (boxes and seg, 640 and 1280, with rotation or crops) find 0–1 of the 5 visible cars, and the DOTA-trained `yolo11n/s-obb` aerial models find at most the 2 fully visible cars, depending on `imgsz`. A straight-down occupancy camera therefore needs a different model (P9.4) or an angled camera; see PROGRESS.md decision log 2026-10-08 (P1.4).
- **Small far-away cars:** use `imgsz=1280` for occupancy (one frame every 5 s, so speed isn't critical). If cars are still missed, add tiling: split into 2×2 overlapping tiles, detect each, merge with NMS (IoU 0.5).
- Keep the detector behind the `Detector` protocol so a YOLOX (Apache-2.0) implementation can be swapped in if the AGPL licence becomes a problem.

## 2. Occupancy scoring (`parking/vision/occupancy.py`)

```python
@dataclass(frozen=True)
class SlotResult:
    id: str
    score: float      # 0..1 overlap ratio
    taken: bool       # score >= threshold (before temporal smoothing)

def score_slots(detections, slots, frame_size, mode="mask", threshold=0.30,
                image_size=None) -> list[SlotResult]: ...
def count_in_zones(detections, count_zones, frame_size, image_size=None) -> dict[str, int]: ...
```

`slots` / `count_zones` are the slot file's `Slot` / `CountZone` lists; `frame_size` and `image_size` are `(width, height)`. Pass the slot file's `image_size` to rescale; `None` means the polygons are already in frame pixels. Shapes are built with `parking/geometry.py` (`to_polygon`, `box_bottom`, `overlap_ratio`, `bottom_center`, `point_in`).

### Algorithm
1. Rescale slot polygons from `image_size` to the actual `frame_size` (both axes).
2. For each detection, make its **footprint** polygon:
   - `mode="mask"`: the mask polygon (`shapely.Polygon(mask).buffer(0)` to fix self-intersections). A detection without a mask, or whose repaired mask is empty, falls back to `box_bottom`.
   - `mode="box_bottom"`: the bottom 35% of the box: `(x1, y2 - 0.35*(y2-y1), x2, y2)`. Approximates where the car touches the ground.
3. For each slot: `score = max over detections of area(footprint ∩ slot) / area(slot)`.
   - Use **max**, not sum: two cars each overlapping 20% of the same slot from the sides don't make it taken.
   - Use an STRtree (shapely) to only test detections near each slot.
4. `taken = score >= threshold`.

### Why these choices
- At an angle, a car's **bounding box** covers the neighbouring slot behind it. The **mask** follows the car's real outline and overlaps far less. `box_bottom` is the cheap fallback.
- A threshold around 0.3 tolerates cars parked off-centre while ignoring a neighbour's mirror or bumper poking in. Tune it with `parking evaluate --sweep`.

### 2.1 Top-down appearance scoring (MVP) (`parking/vision/appearance.py`)

For cameras looking **straight down** at marked spaces, where COCO detectors don't recognise cars (the only sample photo, `ground-01.jpg`, is like this). Selected per camera with `occupancy.method: appearance`; `detector` stays the default. It needs no model and no training, so it's an **MVP stand-in**: the trained per-slot classifier (§9) replaces it once the real camera gives enough labelled photos.

```python
def score_slots_appearance(frame, slots, frame_size, image_size=None, threshold=0.30,
                           params: AppearanceParams | None = None,   # = config.AppearanceCfg
                           reference: np.ndarray | None = None) -> list[SlotResult]: ...
```

1. Rescale slot polygons as in §2 (honour EXIF orientation when reading files).
2. Warp each slot to an upright crop (4 corners; more points → minimum-area rectangle, as in §9) and shrink it by `inset` (default 12% per side) to drop the painted lines and a neighbour's mirror.
3. **Pavement model:** convert each crop to float Lab (L 0..100) after a light Gaussian blur (kernel ≈ 3% of the crop's short side; removes the paver joints). The pavement colour is the median of the pixels of all slot crops pooled together (robust while at least about half the slots are free), or of the same crops of `reference_empty` (an image of the empty lot, passed as `reference`; the caller loads the file) if one is configured. Its spread is the median ΔE of the pooled pixels from that colour.
4. A pixel is **non-pavement** when its colour distance (ΔE) from the pavement colour exceeds `max(k_mad × spread, min_delta_e)`. **Shadow suppression:** pixels that are only darker count as pavement: lightness ratio `r = L / L_pavement` within `shadow_l_range` and `|ab − r × ab_pavement| ≤ shadow_chroma_max` (a shadow's chroma shrinks with its lightness), so a neighbour's shadow doesn't make a free space taken; dark neutral pixels (black cars) stay non-pavement because their chroma differs from the brownish/grey pavement. Defaults tuned in P1.11 on `ground-01.jpg` and `ground-02.jpg` (same scene, slightly shifted framing): `k_mad 1.5`, `shadow_chroma_max 2` give free slots ≤ 0.02 and taken slots ≥ 0.61 on both, so any threshold 0.05–0.60 is 34/34 and the configured 0.30 sits in the middle of the gap. `k_mad` is the sensitive knob: at 1.75–2.0 the grey car on `ground-02.jpg` drops to 0.21–0.25 (the P1.4 defaults, `k_mad 2.0`/`shadow_chroma_max 3`, left a gap of only 0.21 and missed it at 0.30); 1.25–1.5 is a plateau (free ≤ 0.06). A looser chroma test (6) lets the grey car pass as shadow.
5. Clean the mask with a morphological open then close (kernel ≈ `morph_frac` of the crop's short side) and keep the **largest connected blob**, so moss spots, leaves and paint flecks don't add up.
6. `score = blob area / crop area` (0..1), `taken = score >= threshold`. Same `SlotResult` as §2, so smoothing, totals, the annotated image and evaluation are unchanged.

Limits (MVP): tuned on two daytime shots of one scene; night, rain, snow, strong sun shadows and a nearly full lot (the median then drifts towards car colours) are untested, as are pale cars close to the pavement colour. `reference_empty` and the §9 classifier are the fixes.

### Count mode
`count_in_zones`: a vehicle counts toward a zone if the **bottom-centre point of its box** is inside the zone polygon. occupied = count, free = capacity − count (clamped at 0). Several polygons for the same zone are combined; a vehicle counts once per zone. Every zone in `count_zones` appears in the result, with 0 if no vehicle is in it.

### Annotated output (`parking/vision/annotate.py`)
`annotate_occupancy(frame, slots, results, detections, totals, inference_ms=None, image_size=None) -> np.ndarray` returns an annotated copy of the BGR frame: free slots get a green outline, taken slots a red translucent fill (id + score in each slot, kept inside the frame for slots cut off at the edge), detections are drawn in thin yellow (mask if there is one, else the box), and a dark top banner shows `totals` (`{zone: (free, capacity)}`) and the inference time: `Ground: 12 free / 40 | 143 ms`. The banner uses `|`, not `·`: OpenCV's Hershey fonts only draw ASCII. Slot polygons are rescaled from `image_size` as in `score_slots`. Text and line widths scale with the frame width (font scale = width / 1280, at least 0.5), and a banner that is too long shrinks to fit.

### Analyze pipeline (`parking/vision/pipeline.py`)
`analyze_frame(frame, camera_cfg, slot_file, detector, capacities=None, reference=None, classifier=None) -> AnalysisResult` runs detect → `score_slots` (the camera's `occupancy.mode`/`threshold`, slot polygons rescaled from the slot file's `image_size`) → `count_in_zones`, and totals each zone: slot zones take `capacities[zone]` or their number of slots, count zones get a total only if `capacities` has them; `free` is clamped at 0. `AnalysisResult` holds `detections`, `slots`, `zone_counts`, `totals` (`{zone: ZoneTotal(free, taken, capacity)}`) and `timings` (`detect_ms`, `score_ms`, `total_ms`; `inference_ms` = `detect_ms`, or `score_ms` when no detector ran). `to_observation()` gives the [observation payload](api.md#observation) plus `totals` and `timings`. With `occupancy.method: appearance` it never touches `detector` (may be `None`): `detections = []`, `detect_ms = 0`, slots scored by §2.1 (with `reference`, the loaded `reference_empty`); `classifier` / `ensemble` work the same way with the §9 `classifier`. Phase 2's occupancy worker calls the same function.

`parking analyze --image PATH --camera ID [--config config/lot.yaml] [--out out/analyze] [--threshold X] [--mode mask|box_bottom] [--imgsz N] [--method detector|appearance] [--fake-detector]` writes `<out>/<image stem>.json` (`to_observation()`) and `<out>/<image stem>.png` (annotated), and prints `ground: 12 free / 40 (28 taken) in 1.2 s -> …` (the time is `total_ms`, without loading the model). Flags override the camera's config. For `appearance` no model is loaded and `--fake-detector` is ignored. `--fake-detector` (or `PARKING_FAKE_DETECTOR=1`) reads detections from the sidecar `<image>.json` next to the image. Paths in lot.yaml (`slots_file`, `detector.model`) and an image path that doesn't exist from the current folder are resolved against the repo root (the folder holding `config/`), so the command works from the repo root and from `backend/`. `${VAR}`s come from [`cli_env`](config.md#loader-rules-parkingconfigpy).

## 3. Temporal smoothing (`parking/core/smoothing.py`, runs in the API)

```python
class SlotSmoother:
    """A slot changes state only after `k` consecutive readings agree."""
    def __init__(self, k: int = 3): ...
    def update(self, slot_id: str, taken_now: bool) -> bool:   # returns smoothed state
        # state[slot] = current smoothed value (first reading initialises it directly)
        # pending[slot] = (candidate, count)
        # if taken_now == state: reset pending
        # else: count += 1 if candidate == taken_now else 1; flip when count >= k
```

- First reading after start-up sets the state directly. No waiting, so a restart shows numbers immediately.
- `restore(states: dict[str, bool])` seeds smoothed states at start-up (from the DB); pending flips are dropped, and a restored slot then needs `k` contrary readings like any other. `update_many(readings)` smooths a whole observation; `state(slot_id)` / `states` read the current values.
- `CountSmoother` (count zones): the median of the last `k` counts. While the window holds fewer than `k` readings (or an even number), `median_low` is used, so the result is always a count that was really read and the first reading shows directly. It also has `restore(counts)` (seeds each window with one count) and `value(zone)`.
- `k` comes from the camera's `smoothing.consistent_readings` (config.md §1).
- With `sample_every_s: 5` and `k = 3`, a real change appears after ~10–15 s, and a one-frame glitch (person walking past, headlight flash) never shows.

## 4. Bootstrapping slots (`parking bootstrap-slots`) (`parking/vision/bootstrap.py`)

For a quick first draft of the slot file from an image taken when the lot is busy:
1. Detect vehicles (`use_masks=true`, `imgsz=1280`, `conf=0.25`; `--imgsz`/`--conf` override, the rest of the camera's `detector` settings are used).
2. For each detection, take the `box_bottom` footprint (`--footprint box` takes the whole box, for a camera looking straight down), shrink it 10% around its centre, and turn it into a 4-point polygon (top-left, top-right, bottom-right, bottom-left, whole pixels). A footprint overlapping a more confident one by IoU > 0.5 is a duplicate and dropped.
3. Sort in reading order: rows top→bottom (a space joins a row while its centre is within half the median footprint height of the row's first space), each row left→right. Name them `<Z>01…` (`<Z>100` past 99) using the first letter of the camera's first zone; every slot gets that zone and type `standard`.
4. Write the slot file (config.md §2; `reference_image` = the image path relative to the repo root, `count_zones: []`) in the slot editor's JSON layout, so importing and exporting it in the editor changes nothing. `--out` defaults to the camera's `slots_file`; an existing file is **never overwritten unless `--force`** is given. With no vehicles found nothing is written (exit 1). `--fake-detector` reads `<image>.json` like `analyze`.
5. **Then fix it by hand in the slot editor** (empty spaces won't have been detected; angles will be rough).

## 5. Frame health (`parking/vision/health.py`)

Run on every frame before detection. Unhealthy frames are skipped and reported in the `health` message.

| Issue | Test (on a 320-px-wide grayscale copy) | Default |
|-------|---------------------------------------|---------|
| `black` | mean brightness < `black_mean_max` | 12 |
| `frozen` | mean absolute difference vs previous frame < `frozen_diff_max` for `frozen_frames` frames in a row | 0.5, 6 |
| `blurry` | variance of Laplacian < `blur_laplacian_min` | 40 (night IR frames are naturally softer, so tune per camera) |
| `connect_failed` | source raised / returned nothing | — |

A camera is **`degraded`** if more than 50% of the last 20 frames were unhealthy, and **`down`** if there's been no healthy frame for `stale_after_s`.

Checks run in the table's order and the first match wins (`connect_failed` → `black` → `frozen` → `blurry`). The `frozen` run counts consecutive frame pairs that differ by less than `frozen_diff_max`. **Replay sources** (`file:`, `folder:`) repeat stored images on purpose, so the worker turns the `frozen` check off for them (`FrameHealth(check_frozen=False)`, driven by `FrameSource.replay`). `down` is counted from worker start-up if there has never been a healthy frame.

**Tuning (P4.4).** All three numbers (`mean`, `laplacian_var`, `frame_diff`) are measured on every frame, even when an earlier check already matched, and kept in `FrameHealth.last_metrics`. With `LOG_LEVEL=DEBUG` the occupancy worker logs one line per frame: `health_metrics camera=… ts=<ISO> mean=… lap=… diff=…|- issue=…|-`. `parking health-stats <log> --camera ID` groups those by lot-local hour (frames, p1 and median of each) and suggests `black_mean_max` = ½ × p1 of `mean` and `blur_laplacian_min` = ½ × p1 of `laplacian_var` (below the darkest / softest valid frame), and lowers `frozen_diff_max` to ½ × p1 of `frame_diff` only if real frame-to-frame differences go under it. It uses p1, not the minimum, so a few odd frames don't set the threshold; cut lens-cover tests out with `--since/--until`. It warns if fewer than 24 hours are covered.

## 6. Camera shift detection (`parking/vision/shift.py`)

A bumped camera makes every slot polygon point at the wrong pixels.
1. When slots are calibrated, save a **reference frame** (`data/reference/<camera>.jpg`).
2. Every `shift_check_every_s` (300 s): ORB features (1000) on reference and current grayscale frames, ratio-test matching, `cv2.estimateAffinePartial2D` with RANSAC.
3. Use the **median displacement of inlier matches** in pixels. If it's over `shift_max_px` (8 px at reference size) in 3 checks in a row, set health issue `shifted`.
4. Only use matches from the **static background** (outside slot polygons), since parked cars move.
5. `shifted` never auto-corrects. An admin recalibrates (Phase 7) or re-saves the reference.

**Implementation (P4.6).** `ShiftDetector` works on grayscale copies scaled to 1280 px wide (the current frame is first resized to the reference's size) and reports pixels at reference size. The background mask is the inverse of the slot polygons, each grown by 1% of the image width so overhanging cars don't count; if under 5% of the frame is left (or there are no slots) the whole frame is used. Ratio test 0.75, RANSAC reprojection threshold 3 px. A check with fewer than 12 inliers (fog, a black or IR frame against a daytime reference) is **inconclusive**: it neither counts towards nor resets the run of 3. A view turned far away still finds chance inliers with a huge displacement, so it counts. The worker checks only frames that passed the health checks, the first one right away and then every `shift_check_every_s` (DEBUG line `shift check: <px> px (<inliers> inliers of <matches> matches), <n> over in a row`, a WARNING when it becomes shifted), and keeps analysing while shifted. Health then reports `state: degraded` (unless `down`) and `issue: shifted` (unless the current frame has its own issue). No `data/reference/<camera>.jpg` (or an unreadable one) = check off. `save_reference` (admin "reference frame", `/control/save-reference`) and `reload` (recalibrated slots) start over with a fresh detector. On the sample photo a check takes ~100 ms on the dev Pi and measures 5 / 10 px nudges to ±0.5 px; a 1° turn reads 17 px.

## 6.1 Debug frame capture (`parking/workers/debug_capture.py`)

Off by default (`DEBUG_CAPTURE=false`); frames can show people and number plates ([security-privacy.md §4](security-privacy.md#4-privacy-and-gdpr-checklist)). When on, the worker saves the analysed frame (JPEG q90, full resolution, not annotated) and a JSON file next to it into `data/debug/<camera>/<lot-local YYYY-MM-DD>/`:
- **periodic:** the first analysed frame, then the next one at least `DEBUG_CAPTURE_EVERY_MIN` (10) minutes after the last periodic capture;
- **flip:** every analysed frame where a slot's raw per-frame `taken` differs from the previous observation (slots new after a reload don't count). A flip doesn't restart the periodic timer.

Names are `<HHMMSS>_<ms>-<reasons>.jpg|json` (lot-local time, reasons `periodic`, `flip` or `periodic+flip`); the JSON is `{camera_id, frame, reasons, flipped: [slot ids], observation}` with the observation exactly as sent ([api.md §5](api.md)). Unhealthy (skipped) frames aren't captured. A write error (full disk) is logged and never stops the worker.

A `debug-prune` thread deletes files under `data/debug/<camera>/` whose mtime is older than `DEBUG_RETENTION_HOURS` (24) and then empty date folders, at start and every 5 min (at most ¼ of the retention). It runs **even with capture off**, so turning capture off still clears what an earlier run left. The captures are the raw material for the validation set (P4.8): copy frames out of `data/debug/` before they expire.

## 7. Entry/exit counting

### 7.1 Frame pipeline (flow worker)
```
RTSP sub-stream (640×360, ~10 fps)
  → reader thread keeps only the newest frame (avoids latency build-up)
  → crop/mask to ROI
  → MotionGate: idle? skip detection, but still feed the tracker an empty update
  → detector (imgsz 640, conf 0.4)
  → ByteTrack (via ultralytics `model.track(persist=True, tracker="bytetrack.yaml")`)
  → TwoLineCounter.update(tracks, frame_ts)
  → publish FlowEvent(s)
```

As built (P5.4, `parking/vision/tracking.py`): `VehicleTracker.update(image, ts, active)` takes the gate's verdict. **Active**: the frame outside the line file's `roi` (scaled from its `image_size`) is filled black, then `UltralyticsTracker` runs `model.track(frame, persist=True, tracker="bytetrack.yaml", classes=[2, 3, 5, 7], conf=0.4, imgsz=640)` (the camera's `detector` settings) and returns `Detection`s with `track_id` (only confirmed tracks; `cls` from this frame's box). **Inactive**: no detection; ByteTrack gets an empty update (so a lost track ages out after its `track_buffer` of 30 updates), and after **10 s** inactive (`RESET_AFTER_S`) the tracker is reset to drop stale tracks: never at once, so a car that stops on the ramp keeps its id through a short idle spell. ByteTrack restarts its ids at 1 after a reset; the wrapper adds an offset (the highest id handed out so far), so an id is never reused while the worker runs. The backend sits behind a small `TrackBackend` protocol (`track` / `idle` / `reset`) so another detector or tracker (Hailo, YOLOX) can be swapped in. `parking track-check --camera ID [--source URI] [--seconds 600] [--debug-video FILE] [--json]` runs gate + tracker over a camera or a clip, lists each track (frames, seconds, first → last anchor) and writes an annotated MP4 (ROI, A/B, IN arrow, a box + `#id cls conf` + anchor dot per track, a banner with the time and gate state) for checking that each car keeps one id from entering to leaving.

As built (P5.6, `parking/workers/flow_worker.py`, `parking worker flow --camera ID [--print] [--debug-video FILE]`): the loop processes each **new** frame (same `seq` = skipped, polled every 10 ms) as soon as it arrives, there is no fixed interval. Each `FlowEvent` becomes a `FlowEventMsg` with a fresh `uuid4` `event_id` (frame time as `ts`, confidence rounded to 3 decimals) and goes to `ApiClient.queue_flow_events` (the outbox). A frame-time gap of more than 5 s (reconnect, a looped video) resets the gate and the tracker. Health every 10 s: `fps`, `inference_ms_avg`, `gate_active_ratio` over the last 10 s ([api.md](api.md#health-every-10-s)); the frame-health check runs once a second without `frozen`. `/control/reload` re-reads lot.yaml and the line file (fresh gate and counter, the tracker is reset but its ids keep rising, the detector and source are rebuilt only if their config changed) and answers `{"camera_id", "in_direction"}`; `/control/snapshot` is the latest frame with the debug overlay; `/control/save-reference` saves `data/reference/<camera>.jpg` (the frame the lines are drawn on). `--debug-video` writes every processed frame with the ROI, lines, IN arrow, track boxes/ids and a banner `HH:MM:SS (UTC) ACTIVE|idle IN n OUT m`. Compose service `vision-flow` (profile `flow`).

### 7.2 Motion gate (`parking/vision/motion.py`)
- `cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=32, detectShadows=True)` on a downscaled ROI. Ignore shadow pixels (value 127).
- Active when the largest contour area is ≥ `motion_min_area_px` (scaled to the downscale).
- Stay active for 2 s after motion stops, so a car that stops on the ramp keeps being tracked.

As built (P5.3): the ROI's bounding box (the line file's `roi` scaled from its `image_size` to the frame; no ROI = the whole frame) is cropped, blacked out outside the polygon and scaled to ≤ 320 px wide; the foreground is opened with a 3×3 kernel before the contours. `motion_min_area_px` is in **pixels of the line file's `image_size`** (the frame's own pixels without a line file), so it means the same on any stream resolution. The first frame only teaches the background; a frame of another size, or `reset()` (reconnect/reload), starts over. `update(image, ts)` returns `active`, `moving` (motion in this frame) and the largest area. About 3 ms per 640×360 frame on the dev Pi. `parking motion-check --camera ID [--source URI] [--seconds 3600] [--max-ratio 0.2]` runs the gate over a camera or a recording (frame time, so `video:…?realtime=false` covers its own hour) and prints the active share and bursts; exit 1 when the share is ≥ `--max-ratio`.

### 7.3 Two-line counter (`parking/vision/flow.py`)

For each track keep: which side of line A and line B its **anchor point** (bottom-centre of the box) is on, plus the order in which it crossed.

```python
class TwoLineCounter:
    def update(self, tracks: list[Detection], ts: float) -> list[FlowEvent]:
        for t in tracks:
            st = self.state.setdefault(t.track_id, TrackState(first_ts=ts))
            st.frames += 1
            p = anchor(t.box)
            for name, line in (("a", line_a), ("b", line_b)):
                side = sign(cross(line.end - line.start, p - line.start))
                if st.last_side[name] is not None and side != st.last_side[name] and side != 0:
                    st.crossings.append(name)          # e.g. ["a", "b"]
                st.last_side[name] = side or st.last_side[name]
            seq = "".join(st.crossings[-2:])
            if not st.counted and st.frames >= min_track_frames and seq in ("ab", "ba"):
                direction = "in" if (seq == "ab") == (in_direction == "a_to_b") else "out"
                events.append(FlowEvent(direction, t.track_id, ts, conf=t.conf))
                st.counted = True
        # forget tracks not seen for 5 s
```

Rules this enforces:
- A car must cross **both** lines, in order. Touching one line and reversing doesn't count.
- **One event per track.** If the tracker loses a car and re-finds it with a new ID mid-crossing, it might be missed: the evaluation clips measure how often.
- Segments only count where the anchor is between the line's endpoints. A pedestrian path beside the lane can be excluded with the ROI.

As built (P5.5): a side change counts as a crossing only if the anchor's move from its last off-line point crosses the line **between its endpoints** (passing beyond an end just updates the side); an anchor exactly on a line keeps the last side. Lines are in the line file's `image_size` pixels, scaled to `update(tracks, ts, frame_size=(w, h))` (default: the line file's size). `FlowEvent(direction, track_id, ts, conf, cls)` takes `conf`/`cls` from the frame that completed the count; the worker (P5.6) adds `event_id`/`camera_id`. Tracks unseen for `TRACK_TTL_S` = 5 s are dropped; detections without a `track_id` are ignored.

### 7.4 Flow counter (`parking/core/flow_counter.py`, in the API)
```
occupied = clamp(occupied + (+1 if in else -1), 0, capacity)
```
- Idempotent: events carry `event_id`; duplicates (worker retries from its outbox) are ignored.
- `correct(new_value, actor, note)` sets the value and resets the confidence counters.
- The current value is persisted (latest `zone_state` row), so a restart continues from it.
- When clamping happens (e.g. OUT at 0), log a `clamped` warning. A frequent clamp means the count is off.

## 8. Fusion, confidence, trend (`parking/core/fusion.py`)

| Zone method | occupied | confidence |
|-------------|----------|-----------|
| `slots` | number of smoothed-taken slots | 1.0 if camera `ok`; 0.6 if `degraded`; unchanged but `stale=true` if `down` or no data for `stale_after_s` |
| `count` | median-smoothed count | 0.9 / 0.5, same rule |
| `flow` | FlowCounter value | `max(0.3, 1 − 0.01 × events_since_correction − 0.02 × hours_since_correction)`. An initial heuristic, to be tuned in Phase 5 from measured drift |

- **Trend** over `trend_window_min` (15 min): Δfree ≤ −max(2, 5% of capacity) → `filling`; ≥ +that → `emptying`; else `steady`.
- **Level**: computed from free/capacity (see [api.md](api.md#levels)).
- The UI shows **"≈"** before a number when confidence < 0.8.

`StateStore(config, clock, slot_files)` implements this (P2.5):
- `apply_observation(obs)`, `apply_health(msg)`, `tick()` (every second) and `restore(slot_states, zone_counts, updated_at, trend)` each return a list of changes: `SlotChange(ts, camera_id, slot_id, taken)` when a smoothed slot flips (or is first seen), and `ZoneChange(ts, zone_id, occupied, free, level, confidence, stale, trend, count_changed, source)` when anything a client sees about a zone changes. `count_changed` tells the DB layer whether to write a `zone_state` row; `source` is `observation | health | tick | startup`. `status(lang)` builds the `LotStatus` (`has_data` is false until the first observation or restore → `503`).
- **Stale** = the zone never had data, or one of its cameras reported `down`, or no fresh data from one of its cameras for `stale_after_s`. Fresh data is an observation (occupancy cameras) or a non-`down` health message (flow cameras: no cars crossing is normal). Freshness is measured with the API's clock at receipt, never the worker's `ts`. Zone `updated_at` = receipt time of the latest data.
- Confidence uses the last non-`down` health state of the zone's cameras (`ok` until the first health message; any `degraded` camera lowers the zone). Flow confidence is rounded to 2 decimals.
- Slot readings for slots not in the camera's slot file (or in a zone the camera doesn't cover) are ignored with a warning. `occupied` is clamped to the zone capacity. A count zone seen by several cameras sums their medians.
- Trend: a per-zone buffer of `(ts, free)` appended whenever `free` changes, kept 20 min; Δfree = free now − free at `now − trend_window_min` (the oldest sample while the history is shorter). `tick()` also reports trend flips.
- Payloads naming an unknown camera (or an observation from a flow camera) raise `UnknownCameraError`.
- `FlowCounter` (`core/flow_counter.py`) already has `apply(event_id, direction)` (→ `True` applied / `False` clamped / `None` duplicate, last 1000 ids), `correct`, `restore` and the confidence formula; feeding flow events into the store comes in P5.7–P5.8.

## 9. Per-slot classifier

Replaces the MVP appearance scorer (§2.1) for top-down views once the real camera has given enough labelled photos, and is the fallback for angled views if Phase 4 accuracy is below target. Built in P4.10 (`parking/vision/slot_classifier.py`, `backend/scripts/train_slot_classifier.py`); a trained model needs the real camera's labelled frames (P4.8).

- **Crops:** perspective-warp each slot polygon to 128×128 (`cv2.getPerspectiveTransform` on the 4 corners; polygons with more than 4 points use their minimum-area rectangle), no inset: the same `slot_square` for training and inference.
- **Model:** MobileNetV3-Small (torchvision), 2 classes (`free`, `taken`), ImageNet weights. Input RGB, ImageNet-normalised, NCHW, dynamic batch; output logits.
- **Data:** pre-train on public datasets (PKLot, CNRPark-EXT; check their licences; as `DIR/free/*.jpg` + `DIR/taken/*.jpg` crops via `--crops`), then fine-tune (`--init <pre-trained>.pt`, lower `--lr`) on ≥ 300 crops cut from frames labelled in the slot editor (`--images` + `--labels`; `unsure` slots skipped). **20% of the frames** (not crops: a frame's slots stay together) are held out by a hash of the file name, so it's the same frames every run; their labels go to `<model>.holdout.json` for `parking evaluate --labels`. Training augments with flips, brightness/contrast jitter and a ±6 px shift (calibration slop), weights the classes by their counts, keeps the epoch with the best held-out crop accuracy. Train on a laptop/desktop GPU or Colab, not on the Pis.
- **Export:** ONNX (`torch.onnx.export`, opset 17; needs the `onnx` package, hence `uv run --with onnx`) → `models/slot_classifier.onnx` + `.pt` (weights, for `--init`) + `.json` sidecar (classes, crop size, data counts, held-out accuracy; the loader takes the class order and crop size from it, default `free, taken` at 128). Run with onnxruntime (CPU, 2 threads), all slots in one batch: **~70 ms for 17 slots on the Pi 5's CPU** (~60 ms with 4 threads, ~300 ms for 68 slots), measured in P4.10; still far inside a 5 s sample interval.
- **Combining:** `occupancy.method: classifier` → score = P(taken), `taken = score ≥ occupancy.classifier.threshold` (0.5). `ensemble` → score = mean of P(taken) and the appearance score (§2.1, with `reference_empty` if set) mapped piecewise-linearly so that `occupancy.threshold` lands on 0.5 (0 → 0, threshold → 0.5, 1 → 1), same cut-off. The ensemble pairs the classifier with the appearance scorer, not the detector's overlap, because the classifier is for the top-down views where the detector doesn't work (P1.4). Pick between `appearance`, `classifier` and `ensemble` by `parking evaluate` on the held-out frames.
- **Plumbing:** `analyze_frame(…, classifier=…)` raises if the method needs one and none is given; the worker loads it at start/`reload` (`WorkerError` if the file is missing), keeps it across a reload if `occupancy.classifier` didn't change; `parking analyze` / `evaluate` take `--method classifier|ensemble` and `--classifier FILE` (overrides `occupancy.classifier.model`). `evaluate`'s sweep re-thresholds the probabilities (default threshold = `classifier.threshold`).

## 10. Evaluation (`parking/vision/evaluate.py`)

### Occupancy metrics (per image set and per condition tag)
- **Slot accuracy** = correct slots / labelled slots (excluding `unsure`).
- **Precision / recall for "free"**: saying "free" when it's taken is the worse error (it sends someone to a full lot), so watch free-precision.
- **Count error** = |predicted free − true free| per image: report the mean, and the % of images with error ≤ 1.
- **Sweep:** `--sweep 0.1:0.6:0.05` reruns scoring (detection is cached, so it's fast) and prints accuracy per threshold.
- Output: a table in the terminal + `out/eval/<camera>-<date>.json` + annotated images where predictions were wrong.

`parking evaluate --camera ID [--images data/samples|IMAGE] [--labels data/labels/<camera>.json] [--config …] [--sweep start:stop:step] [--mode mask|box_bottom|both] [--threshold X] [--imgsz N] [--method detector|appearance] [--out out/eval] [--cache out/cache] [--no-cache] [--fake-detector]`:
- "Free" is the positive class: **free-precision** = said free and really free / said free; **free-recall** = said free and really free / really free. Precision is `n/a` when nothing was said free. Unsure slots count in neither the metrics nor the free counts. Every slot id in the labels must exist in the slot file (`check_labels`).
- Detections are cached in `out/cache/<image file>.<model folder>.<imgsz>.json` (the `FakeDetector` sidecar format plus `frame_size` and a `key`: image size + mtime, `conf`, `classes`, `use_masks`). A cache file with another key is ignored and rewritten; the model is loaded only on a cache miss.
- With `occupancy.method: appearance` (or `--method appearance`) no detector or cache is used: each image's slot scores are computed once and the sweep re-thresholds them; the report's mode key is `appearance`, and `--mode`/`--fake-detector` are ignored. `classifier` / `ensemble` (§9) work the same way (mode key = the method, default threshold `occupancy.classifier.threshold`).
- Without `--mode` the camera's mode is used; `both` evaluates `mask` and `box_bottom` from the same detections. `--sweep` bounds are inclusive and every value must be strictly between 0 and 1.
- **Best threshold:** highest slot accuracy, then highest free-precision, then lowest mean count error; a tie left after that goes to the threshold closest to the configured one.
- Terminal: per image `correct/labelled`, accuracy, `free true/predicted`, count error and the mistakes by slot id (`G05 (taken, said free)`), then an overall line, the **target verdict** and one line per condition tag; with `--sweep` a table per threshold, `best threshold (<mode>): …` and the target verdict at the best threshold.
- **Target** (P4.9): slot accuracy ≥ 97% **and** count error ≤ 1 on ≥ 95% of images (`TARGET_ACCURACY`, `TARGET_COUNT_WITHIN_1` in `evaluate.py`). The verdict line reads `target (…): met`, `missed (<the part that missed>)` or `n/a`; every summary in the JSON has `meets_target` (true/false/null). Missed after tuning → P4.10.
- Files: `out/eval/<camera>-<YYYY-MM-DD>.json` (per mode: threshold, overall, by condition, per image, sweep, best threshold; each summary with `meets_target`) and `out/eval/<camera>-<image stem>-<mode>.png` for each image with a mistake: the annotated frame (§2) with the wrong slots outlined in magenta and "should be taken" / "should be free" (`highlight_slots` in `annotate.py`).

### Flow metrics (per clip)
- Match predicted events to truth events with the same direction within **±2 s** (greedy, by time).
- Report **TP, FP (extra counts), FN (missed)**, event accuracy = TP / (TP + FP + FN), and **net error** = (pred in − pred out) − (true in − true out). Net error is what causes drift.

## 11. Runtimes and performance

The model is exported once **per machine** for the runtime that suits its hardware (`detector.runtime` in [config.md](config.md#1-configlotyaml)); the code path is the same.

| Machine type | Runtime | Export (`parking models export --runtime …`) |
|--------------|---------|-----------------------------------------------|
| ARM CPU (dev Pi, other ARM boards) | **NCNN** | `ncnn` → `models/<name>_ncnn_model/` |
| Intel x86 CPU (mini PC, server) | **OpenVINO** | `openvino` → `models/<name>_openvino_model/` |
| Any CPU (fallback) | ONNX Runtime | `onnx` → `models/<name>.onnx` |
| NVIDIA GPU / Jetson | CUDA / TensorRT | `engine` (built on that machine) |
| Raspberry Pi + AI HAT+ (Hailo) | Hailo | compiled `.hef` from the Hailo model zoo, used via a `HailoDetector` |

Guidelines on any machine:
- Limit threads per worker (`OMP_NUM_THREADS`, `torch.set_num_threads`) so the workers don't fight each other or the API.
- Occupancy: one inference every 5 s, so even 1–2 s at `imgsz=1280` with masks is fine.
- Flow: aim for ≥ 8 fps while active. If detect + track takes over 120 ms per frame, first shrink the ROI and use `imgsz=480`; then use a faster runtime or an accelerator for that machine.
- Measure with `parking benchmark` and record results in PROGRESS.md → Metrics, **labelled with the machine**.

`parking benchmark` (`parking/vision/benchmark.py`) runs every runtime × `imgsz` × model case, and the appearance scorer (`method: appearance` on `--camera`'s slots, the whole `analyze_frame` without a detector), each in a **fresh spawned process** so the peak RSS (`ru_maxrss`) belongs to that case alone. Each case does `--warmup` untimed runs, then `--runs` timed `detect(frame)` calls on the full image (pre-processing included, model loading excluded); p95 is nearest-rank. NCNN exports have a fixed input size, so it exports each size once into `models/bench/<model>-<imgsz>/` (copying the local `.pt`). A failing case shows as an error row; the others go on. It prints a Markdown table plus the CPU temperature before/after (`vcgencmd measure_temp`, else `/sys/class/thermal`; warns over 80 °C) and writes `out/benchmark/benchmark-<time>.json`.

### Reference numbers (dev Pi 5 8 GB, CPU, measured 2026-10-08 in P1.10)
| Task | PyTorch | NCNN |
|------|---------|------|
| YOLO11n @ 640 px | 305 ms | **83 ms** |
| YOLO11n-seg @ 640 px | 430 ms | 117 ms |
| YOLO11n @ 1280 px | 1312 ms | 367 ms |
| YOLO11n-seg @ 1280 px | 1827 ms | 520 ms |
| Appearance scorer (17 slots, 1932×2576) | 158 ms, no model | |

Medians of 20 runs on `ground-01.jpg`; full table with p95 and memory in PROGRESS.md → Metrics.

The **dev Pi is a worst case.** Production hardware is benchmarked again in P8.2 (and on the candidate vision host during Phase 5), and the settings are re-tuned there.
