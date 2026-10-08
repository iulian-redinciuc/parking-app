# Phase 1: Still-image proof of concept

**Goal:** given **one image** of the lot, correctly say which spaces are taken and how many are free.
**Needs hardware:** no, only the sample image(s) from P0.8.
**Specs used:** [vision.md §1–4, 10–11](../design/vision.md), [config.md §1–2, §4](../design/config.md).

## Deliverables
- `tools/slot-editor/`: draw spaces on an image, label images
- `parking analyze`, `parking evaluate`, `parking bootstrap-slots`, `parking benchmark`, `parking models export`
- An annotated output image you've checked by eye
- Measured accuracy and speed recorded in PROGRESS.md

## Order
```
P1.1 data ─┬─> P1.3 slot editor ──> P1.8 labels ──┐
           │                                      ├─> P1.10 benchmark ─> P1.11 tune + decide
P1.2 config┴─> P1.4 detector ─> P1.5 occupancy ─> P1.6 annotate ─> P1.7 analyze ─> P1.9 bootstrap
```

---

## P1.1: Sample data layout
**Files:** `data/samples/`, `data/labels/`, `data/reference/` (all git-ignored)

**Steps**
1. Name images `<camera>-<nn>.jpg`, e.g. `ground-01.jpg`.
2. Record each image's conditions (day/night/rain/full/empty). They go into the labels file in P1.8.
3. Copy the best "normal day" image to `data/reference/cam-ground.jpg`. Slots are drawn on it.

**Done when:** `ls data/samples` shows the images, and `git status` doesn't.

## P1.2: Config loader
**Files:** `backend/parking/config.py`, `backend/tests/unit/test_config.py`

**Steps**
1. Pydantic models: `LotConfig`, `Lot`, `Zone`, `Camera`, `DetectorCfg`, `OccupancyCfg`, `SmoothingCfg`, `HealthCfg`, `FlowCfg`, `ApiCfg`, `SlotFile`, `Slot`, `CountZone`, `LineFile`, following [config.md](../design/config.md).
2. `load_config(path) -> LotConfig`: read text → replace `${VAR}` with `os.environ` (regex `\$\{([A-Z0-9_]+)\}`, skip lines starting with `#`) → `yaml.safe_load` → validate.
3. `load_slots(path) -> SlotFile` and `SlotFile.scaled(frame_w, frame_h) -> list[Slot]` (scales polygons).
4. Cross-validation rules from [config.md "Loader rules"](../design/config.md#loader-rules-parkingconfigpy).
5. `Settings(BaseSettings)` with every `.env` variable (all optional for now).
6. Tests: valid example loads; each rule fails with a clear message; env interpolation; scaling.

**Done when:** `pytest tests/unit/test_config.py` passes, and loading `config/lot.yaml` works.

## P1.3: Slot editor (standalone web page)
**Files:** `tools/slot-editor/index.html`, `tools/slot-editor/editor.js`, `tools/slot-editor/README.md`

Plain HTML and JavaScript with no build step, so you open it by double-clicking or via `python -m http.server`. The core lives in `editor.js` as an ES module (`createEditor(canvas, opts)`) so the Phase 7 admin can import it.

**Features**
1. **Load image** (file picker) → drawn on a canvas, fitted to the window. Mouse-wheel / pinch zoom, drag to pan.
2. **Slots mode**
   - Click to add points; click the first point or press Enter to close a polygon. Esc cancels.
   - Select a slot → drag its corners; Delete removes it; edit `id`, `zone`, `type` in a side panel.
   - Auto-name the next slot (`G01`, `G02`, …), with the zone chosen in a dropdown.
   - "Duplicate right" (`D`): copies the selected slot shifted by its own width. Rows of identical spaces become fast to draw.
3. **Lines mode** (for Phase 5): draw `line_a`, `line_b`, the ROI polygon, and pick `in_direction`.
4. **Label mode**: load a slot file + an image → click slots to cycle *free → taken → unsure* → saves into a labels JSON ([config.md §4](../design/config.md#4-ground-truth-labels)). Keyboard: arrow keys move to the next image in a multi-file selection.
5. **Import/Export JSON** in the exact formats of [config.md §2–4](../design/config.md#2-slot-file-configslotscamerajson). `image_size` = the natural size of the loaded image.
6. Each slot shows its id at its centroid. Colours: free = green outline, taken = red fill (label mode), selected = blue.

**Done when:** you can draw all ground spaces on `data/reference/cam-ground.jpg`, export `config/slots/cam-ground.json`, re-import it unchanged, and it passes `load_slots()`.

## P1.4: Detector module and model export
**Files:** `backend/parking/vision/detector.py`, `backend/parking/cli.py` (`models export`), `backend/tests/unit/test_detector_filter.py`

**Steps**
1. `uv sync --extra vision` (installs ultralytics + torch CPU + ncnn + onnxruntime; takes a while on the dev Pi).
2. `parking models export --model yolo11n-seg --imgsz 1280 --runtime ncnn` (NCNN suits the dev Pi's ARM CPU; other machines use other runtimes, see [vision.md §11](../design/vision.md#11-runtimes-and-performance)) → `models/yolo11n-seg_ncnn_model/`. Also export `yolo11n` at 640 (used in Phase 5 and for benchmarking).
3. `YoloDetector` per [vision.md §1](../design/vision.md#1-detector): load once, `detect(frame)` → `list[Detection]`, mapping class ids ↔ names, with masks from `result.masks.xy`.
4. `FakeDetector(json_path)` returning detections from a JSON sidecar (for tests and CI).
5. A unit test for the class-filter/mapping logic using `FakeDetector`. A `@pytest.mark.slow` test that runs the real model on a synthetic fixture and expects ≥ 1 car.

**Done when:** `uv run python -c "from parking.vision.detector import YoloDetector; ..."` on `ground-01.jpg` prints a sensible number of cars.

## P1.5: Geometry and occupancy scoring
**Files:** `backend/parking/geometry.py`, `backend/parking/vision/occupancy.py`, `backend/tests/unit/test_occupancy.py`, `backend/tests/unit/test_geometry.py`

**Steps**
1. `geometry.py`: `to_polygon(points)` (with `.buffer(0)` repair), `box_bottom(box, frac=0.35)`, `overlap_ratio(footprint, slot)`, `bottom_center(box)`, `point_in(poly, pt)`.
2. `occupancy.py`: `score_slots(...)` and `count_in_zones(...)`, exactly as in [vision.md §2](../design/vision.md#2-occupancy-scoring-parkingvisionoccupancypy). Use `shapely.STRtree` over footprints.
3. Tests with hand-made `Detection`s (no model):
   - a car fully inside a slot → score ≈ 1
   - a car half over two slots → each ≈ 0.5
   - two cars each covering 20% of a slot → score 0.2 (max, not sum)
   - mask vs box_bottom on an angled car where the box spills into the neighbour but the mask doesn't
   - threshold boundary (0.30 → taken, 0.2999 → free)

**Done when:** tests pass, with branch coverage of `occupancy.py` ≥ 90% (`pytest --cov`; add `pytest-cov` to dev deps).

## P1.6: Annotated output image
**Files:** `backend/parking/vision/annotate.py`

**Steps**
1. `annotate_occupancy(frame, slots, results, detections, totals) -> np.ndarray`:
   - slot polygons: green outline (free) / red translucent fill (taken), with id + score text
   - detection masks/boxes in thin yellow
   - a top banner: "Ground: 12 free / 40 · 143 ms"
2. Text size scales with the image width so it's readable on 4K and 720p.

**Done when:** the output PNG is readable when viewed on a phone.

## P1.7: `parking analyze`
**Files:** `backend/parking/cli.py`, `backend/parking/vision/pipeline.py`

**Steps**
1. `pipeline.analyze_frame(frame, camera_cfg, slot_file, detector) -> AnalysisResult` (detections, slot results, zone totals, timings). Phase 2's worker reuses exactly this function.
2. CLI: `parking analyze --image PATH --camera cam-ground [--config config/lot.yaml] [--out out/analyze] [--threshold X] [--mode mask|box_bottom] [--imgsz N]`. CLI flags override the config for experiments.
3. Writes `out/analyze/<image>.json` (the shape of the [observation payload](../design/api.md#observation) plus totals) and `out/analyze/<image>.png`. Prints a one-line summary.

**Done when:** `uv run parking analyze --image data/samples/ground-01.jpg --camera cam-ground` prints e.g. `ground: 12 free / 40 (28 taken) in 1.2 s` and writes both files.

## P1.8: Ground-truth labels and `parking evaluate`
**Files:** `data/labels/cam-ground.json` (git-ignored), `backend/parking/vision/evaluate.py`, CLI `evaluate`, `backend/tests/unit/test_evaluate.py`

**Steps**
1. In the slot editor's **label mode**, label every sample image carefully (zoom in; use *unsure* when you genuinely can't tell).
2. `evaluate.py`: the metrics from [vision.md §10](../design/vision.md#10-evaluation-parkingvisionevaluatepy). Cache detections per image (`out/cache/<image>.<model>.<imgsz>.json`) so a threshold sweep only reruns scoring.
3. CLI: `parking evaluate --images data/samples --labels data/labels/cam-ground.json --camera cam-ground [--sweep 0.1:0.6:0.05] [--mode both]`.
4. Output: a terminal table per image + overall, mistakes listed by slot id, and wrong slots highlighted in `out/eval/…png`.
5. Unit test the metric maths on toy predictions and labels.

**Done when:** the command prints slot accuracy, free-precision, count error and the best threshold.

## P1.9: `parking bootstrap-slots`
**Files:** `backend/parking/vision/bootstrap.py`, CLI

**Steps:** implement [vision.md §4](../design/vision.md#4-bootstrapping-slots-parking-bootstrap-slots). Output a valid slot file. Never overwrite an existing file unless `--force` is given.

**Done when:** running it on the busy sample produces a slot file that opens in the editor, needing only adjustments rather than drawing from scratch.

## P1.10: Benchmark on the dev Pi
**Files:** CLI `benchmark`, results in PROGRESS.md → Metrics

**Steps**
1. `parking benchmark --image data/samples/ground-01.jpg --runs 20`: for each combination of {PyTorch, NCNN} × {640, 1280} × {yolo11n, yolo11n-seg}, warm up 3 runs, then report median and p95 ms, plus peak RSS memory.
2. Run it with nothing else heavy running. Note the CPU temperature (`vcgencmd measure_temp`) before and after; if it's over 80 °C, the active cooler is needed.

**Done when:** a results table is pasted into PROGRESS.md.

## P1.11: Tune and decide
**Steps**
1. Run `evaluate --sweep --mode both` with the seg and non-seg models at 640 and 1280.
2. Pick the combination with the best **free-precision**, then the best slot accuracy, that runs in < 2 s.
3. If small far-away cars are missed even at 1280: try tiling (2×2) and note the effect.
4. If accuracy is clearly poor because of the angle (heavy occlusion), note it. It informs camera mounting in Phase 4 and whether the per-slot classifier will be needed.
5. Write the chosen `model / imgsz / mode / threshold` into `config/lot.yaml` and the Decision log, with the numbers.

**Done when:** the decision is recorded, and `parking analyze` on every sample gives the hand-counted free number (or the remaining errors are understood and explained).

---

## Exit criteria
- [ ] Free count matches the hand count on the sample image(s), or the remaining error is explained with a plan
- [ ] < 2 s per image on the dev Pi with the chosen settings (production hardware is re-checked in P8.2)
- [ ] You've looked at the annotated image(s) and agree
- [ ] Benchmarks + decisions in PROGRESS.md
- [ ] All unit tests green in CI
