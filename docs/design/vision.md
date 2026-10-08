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

### Count mode
`count_in_zones`: a vehicle counts toward a zone if the **bottom-centre point of its box** is inside the zone polygon. occupied = count, free = capacity − count (clamped at 0). Several polygons for the same zone are combined; a vehicle counts once per zone. Every zone in `count_zones` appears in the result, with 0 if no vehicle is in it.

### Annotated output (`parking/vision/annotate.py`)
`annotate_occupancy(frame, slots, results, detections, totals, inference_ms=None, image_size=None) -> np.ndarray` returns an annotated copy of the BGR frame: free slots get a green outline, taken slots a red translucent fill (id + score in each slot, kept inside the frame for slots cut off at the edge), detections are drawn in thin yellow (mask if there is one, else the box), and a dark top banner shows `totals` (`{zone: (free, capacity)}`) and the inference time: `Ground: 12 free / 40 | 143 ms`. The banner uses `|`, not `·`: OpenCV's Hershey fonts only draw ASCII. Slot polygons are rescaled from `image_size` as in `score_slots`. Text and line widths scale with the frame width (font scale = width / 1280, at least 0.5), and a banner that is too long shrinks to fit.

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
- `CountSmoother` (count zones): the median of the last `k` counts.
- With `sample_every_s: 5` and `k = 3`, a real change appears after ~10–15 s, and a one-frame glitch (person walking past, headlight flash) never shows.

## 4. Bootstrapping slots (`parking bootstrap-slots`)

For a quick first draft of the slot file from an image taken when the lot is busy:
1. Detect vehicles (`use_masks=true`, `imgsz=1280`, `conf=0.25`).
2. For each detection, take the `box_bottom` footprint, shrink it 10%, and turn it into a 4-point polygon.
3. Sort left→right, top→bottom, and name them `<Z>01…` using the camera's first zone letter.
4. Write the slot file. **Then fix it by hand in the slot editor** (empty spaces won't have been detected; angles will be rough).

## 5. Frame health (`parking/vision/health.py`)

Run on every frame before detection. Unhealthy frames are skipped and reported in the `health` message.

| Issue | Test (on a 320-px-wide grayscale copy) | Default |
|-------|---------------------------------------|---------|
| `black` | mean brightness < `black_mean_max` | 12 |
| `frozen` | mean absolute difference vs previous frame < `frozen_diff_max` for `frozen_frames` frames in a row | 0.5, 6 |
| `blurry` | variance of Laplacian < `blur_laplacian_min` | 40 (night IR frames are naturally softer, so tune per camera) |
| `connect_failed` | source raised / returned nothing | — |

A camera is **`degraded`** if more than 50% of the last 20 frames were unhealthy, and **`down`** if there's been no healthy frame for `stale_after_s`.

## 6. Camera shift detection (`parking/vision/shift.py`)

A bumped camera makes every slot polygon point at the wrong pixels.
1. When slots are calibrated, save a **reference frame** (`data/reference/<camera>.jpg`).
2. Every `shift_check_every_s` (300 s): ORB features (1000) on reference and current grayscale frames, ratio-test matching, `cv2.estimateAffinePartial2D` with RANSAC.
3. Use the **median displacement of inlier matches** in pixels. If it's over `shift_max_px` (8 px at reference size) in 3 checks in a row, set health issue `shifted`.
4. Only use matches from the **static background** (outside slot polygons), since parked cars move.
5. `shifted` never auto-corrects. An admin recalibrates (Phase 7) or re-saves the reference.

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

### 7.2 Motion gate (`parking/vision/motion.py`)
- `cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=32, detectShadows=True)` on a downscaled ROI. Ignore shadow pixels (value 127).
- Active when the largest contour area is ≥ `motion_min_area_px` (scaled to the downscale).
- Stay active for 2 s after motion stops, so a car that stops on the ramp keeps being tracked.

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

## 9. Fallback: per-slot classifier (only if Phase 4 accuracy < target)

- **Crops:** perspective-warp each slot polygon to 128×128 (`cv2.getPerspectiveTransform` on the 4 corners; polygons with more than 4 points use their minimum-area rectangle).
- **Model:** MobileNetV3-Small (torchvision), 2 classes, ImageNet weights.
- **Data:** pre-train on public datasets (PKLot, CNRPark-EXT; check their licences), then fine-tune on ≥ 300 crops labelled from our own camera (the labelling mode of the slot editor produces these).
- **Export:** ONNX → run with onnxruntime. All slots in one batch take well under 50 ms even on the dev Pi's CPU.
- **Combining:** `taken = classifier_prob ≥ 0.5`, or an ensemble: average with the overlap score mapped through the threshold. Decide by measurement.

## 10. Evaluation (`parking/vision/evaluate.py`)

### Occupancy metrics (per image set and per condition tag)
- **Slot accuracy** = correct slots / labelled slots (excluding `unsure`).
- **Precision / recall for "free"**: saying "free" when it's taken is the worse error (it sends someone to a full lot), so watch free-precision.
- **Count error** = |predicted free − true free| per image: report the mean, and the % of images with error ≤ 1.
- **Sweep:** `--sweep 0.1:0.6:0.05` reruns scoring (detection is cached, so it's fast) and prints accuracy per threshold.
- Output: a table in the terminal + `out/eval/<camera>-<date>.json` + annotated images where predictions were wrong.

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

### Reference numbers (estimates, to be measured)
| Task | Dev Pi 5 (NCNN, CPU) |
|------|----------------------|
| YOLO11n @ 640 px | ~80–120 ms per frame |
| YOLO11n-seg @ 640 px | ~1.5–2× the above |

The **dev Pi is a worst case.** Production hardware is benchmarked again in P8.2 (and on the candidate vision host during Phase 5), and the settings are re-tuned there.
