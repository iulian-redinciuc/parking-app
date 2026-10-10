# Fine-tuned detector and the licence swap (P9.4)

Two things the stock detector can't give:

- **Accuracy on this lot's view.** COCO-pretrained YOLO11n doesn't recognise cars seen straight down ([vision.md §1](vision.md#1-detector)); a detector trained on the lot's own frames can.
- **A detector that isn't AGPL.** The Ultralytics models and code are AGPL-3.0; YOLOX is Apache-2.0.

**Status: the tools are built, no model is trained.** Training needs the labelled frames of the real camera (P4.8, about 200) and a machine with a GPU. Nothing in the running system changes until a camera's `detector` in `lot.yaml` points at a new model.

## 1. The two routes

| | A: fine-tune **YOLO11n** | B: train **YOLOX-nano** |
|---|---|---|
| Licence | AGPL-3.0 (Ultralytics), as today | Apache-2.0 |
| Training | `yolo detect train`, one command | YOLOX's own repo and an experiment file |
| Runs through | `YoloDetector` (NCNN / OpenVINO / ONNX, as exported) | `YoloxDetector` (ONNX Runtime) |
| `lot.yaml` | `model:` the new export, `classes: [car]` | `type: yolox`, `runtime: onnx`, `classes: [car]` |
| Masks | no (a `-seg` model would need mask labels) | no |
| Flow camera | yes | **no**: tracking still goes through Ultralytics (§6) |

Both train on the same dataset (§2) and are compared with `parking evaluate` on the same held-out frames (§5). Neither gives masks, so the camera's `occupancy.mode` is `box_bottom` for an angled view; for a straight-down view the whole box is the car's footprint, which `box_bottom` (the bottom 35%) under-counts, so tune `occupancy.threshold` with `--sweep` or prefer the per-slot classifier ([vision.md §9](vision.md#9-per-slot-classifier)), which answers the same question from the same labelled frames without boxes.

## 2. The dataset: `parking export-yolo`

`parking export-yolo --camera ID [--images data/validation/<camera>] [--labels data/labels/<camera>-validation.json] [--out data/yolo/<camera>] [--holdout 0.2] [--pad 0] [--review] [--coco] [--force] [--config …]` (`parking/vision/yolo_dataset.py`) turns frames labelled in the slot editor ([config.md §4](config.md#4-ground-truth-labels)) into a detector training set under git-ignored `data/`:

```
data/yolo/<camera>/
  images/train|val/<frame>.jpg     copies of the frames
  labels/train|val/<frame>.txt     one line per box: `0 cx cy w h`, 0–1 of the frame
  data.yaml                        for Ultralytics (absolute `path`, class 0 = car)
  annotations/train.json|val.json  COCO format for YOLOX, built from the .txt files
  holdout.json                     the val frames' slot labels, for `parking evaluate --labels`
  review/<frame>.jpg               with --review: the boxes drawn in red, at most 1600 px
```

- **Boxes.** One box per `taken` slot: the bounding box of its polygon (scaled to the frame), grown by `--pad` × its size per side, clipped to the frame. One class, `car`, for anything parked in a space. A frame without taken slots gets an empty label file (a background image).
- **The boxes are rough**: they outline the *space*, not the car, so a small car gets a loose box, a car over the line a cut one, and a car parked outside any space none at all. **Look through `review/` before training** and correct the `.txt` files where it matters (any YOLO-format labelling tool opens `images/` + `labels/`).
- **Unsure slots** are painted grey (114, the letterbox padding colour) in the copy, so the space is neither a car nor background.
- **Hold-out.** `--holdout` (20%) of the frames go to `val`, picked by the same hash of the file name as the slot classifier's hold-out (`held_out` in `parking/vision/validation.py`), so both models are judged on the same unseen frames.
- **`--coco`** only rebuilds `annotations/*.json` from the label files of an existing dataset: run it after correcting boxes. A class other than 0 or a malformed line is an error naming the file.
- An existing dataset is only replaced with `--force` (corrected label files would be lost). Labelled images missing from the folder are skipped with a message; slot ids in the labels that the slot file doesn't have are an error.

## 3. Route A: fine-tune YOLO11n

On a machine with a GPU (or Colab), with the repo checked out and `data/yolo/<camera>/` copied over (re-run `export-yolo` there, or fix `path:` in `data.yaml`):

```bash
cd backend
uv run yolo detect train model=yolo11n.pt data=../data/yolo/cam-ground/data.yaml \
  imgsz=1280 epochs=100 batch=8 patience=20 fliplr=0.5 flipud=0.5 project=../out/train name=cam-ground
cp ../out/train/cam-ground/weights/best.pt ../models/yolo11n-cam-ground.pt
```

Then on the machine that runs the camera, with that `.pt` in `models/`: `uv run parking models export --model yolo11n-cam-ground --imgsz 1280 --runtime ncnn` and in `lot.yaml`

```yaml
    detector:
      runtime: ncnn
      model: models/yolo11n-cam-ground_ncnn_model
      imgsz: 1280
      classes: [car]          # the fine-tuned model has only this class
      use_masks: false
    occupancy: { method: detector, mode: box_bottom }
```

`flipud` is for a straight-down view only (a car can point either way); leave it at 0 for an angled camera. `imgsz` is the size the cars need: at 640 a car in a 2560 px frame is a few dozen pixels.

## 4. Route B: YOLOX-nano

### 4.1 `YoloxDetector` (`parking/vision/yolox.py`)

Implements the `Detector` protocol on **ONNX Runtime** (CPU, 2 threads), with no Ultralytics import. It reads the ONNX file that YOLOX's `tools/export_onnx.py` writes:

- **Input:** the frame resized with its aspect ratio kept into the top-left of a grey (114) canvas of the model's size, BGR, 0–255 float, NCHW. A model with a fixed input size dictates it: `detector.imgsz` must equal it (the error says which value to set); a dynamic one takes `imgsz` (a multiple of 32).
- **Output:** `[1, N, 5 + classes]` = `cx, cy, w, h, objectness, class scores` per grid cell of strides 8, 16, 32, decoded here (`decode`: offsets + cell index, × stride; sizes `exp` × stride). Score = objectness × the best of the **wanted** classes; below `detector.conf` dropped; per-class NMS at IoU 0.45; boxes scaled back to frame pixels and clipped.
- **Sidecar** `<model>.json` next to the `.onnx` (optional): `{"classes": ["car"], "decoded": false, "strides": [8, 16, 32]}`. Without it the 80 COCO class names are assumed, which fits the official pre-trained models; a fine-tuned model needs `classes`. `decoded: true` is for a model exported with `--decode_in_inference`. A model whose class count doesn't match the names is an error.

`lot.yaml` ([config.md §1](config.md#1-configlotyaml)):

```yaml
    detector:
      type: yolox             # default: yolo
      runtime: onnx           # required for yolox
      model: models/yolox-cam-ground.onnx
      imgsz: 640              # the size the model was exported at
      classes: [car]
      use_masks: false        # required for yolox
```

The official COCO-pretrained YOLOX-nano works as it is, for an angled camera or to try the code path: download `yolox_nano.onnx` from the [YOLOX releases](https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0) into `models/`, `imgsz: 416`, the default `classes`.

### 4.2 Training (not run here: it follows YOLOX's own custom-data guide)

On the GPU machine:

```bash
git clone https://github.com/Megvii-BaseDetection/YOLOX && cd YOLOX && pip install -v -e .
ln -s /path/to/data/yolo/cam-ground datasets/parking
ln -s images/train datasets/parking/train2017 && ln -s images/val datasets/parking/val2017   # the folder names YOLOX's COCO loader expects
wget https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.pth
```

`exps/parking_nano.py`:

```python
import os
from exps.default.yolox_nano import Exp as Nano

class Exp(Nano):
    def __init__(self):
        super().__init__()
        self.exp_name = "parking_nano"
        self.data_dir = os.path.join("datasets", "parking")
        self.train_ann, self.val_ann = "train.json", "val.json"
        self.num_classes = 1
        self.input_size = self.test_size = (640, 640)
        self.max_epoch, self.no_aug_epochs, self.eval_interval = 100, 10, 5
```

```bash
python tools/train.py -f exps/parking_nano.py -d 1 -b 16 --fp16 -c yolox_nano.pth
python tools/export_onnx.py -f exps/parking_nano.py -c YOLOX_outputs/parking_nano/best_ckpt.pth --output-name yolox-cam-ground.onnx
echo '{"classes": ["car"]}' > yolox-cam-ground.json      # both files go to models/ on the vision host
```

## 5. Comparing

On the held-out frames, each candidate against what runs today (edit the camera's `detector` / `occupancy` in a copy of `lot.yaml`, or use the flags):

```bash
cd backend
uv run parking evaluate --camera cam-ground --images ../data/yolo/cam-ground/images/val \
  --labels ../data/yolo/cam-ground/holdout.json --method detector --mode box_bottom --sweep 0.1:0.6:0.05
uv run parking evaluate --camera cam-ground --images ../data/yolo/cam-ground/images/val \
  --labels ../data/yolo/cam-ground/holdout.json --method appearance      # or classifier: today's method
```

The verdict is `parking evaluate`'s target line ([vision.md §10](vision.md#10-evaluation-parkingvisionevaluatepy)): slot accuracy ≥ 97% and count error ≤ 1 on ≥ 95% of the images. A new detector replaces the current method only if it meets the target and beats it on these frames; record the table in PROGRESS.md → Metrics. Detections are cached per model name and input size, so the sweep is quick.

## 6. What the licence swap covers, and what it doesn't

`type: yolox` on the **occupancy camera** means no Ultralytics code is imported by that worker. It is not yet a project without AGPL code:

- The **flow camera** tracks through Ultralytics (`model.track`, its ByteTrack) and its YOLO11n model; `type: yolox` on a flow camera is a config error. A swap there needs a tracker of its own on top of `YoloxDetector` (the original ByteTrack is MIT) and a re-run of P5.9's clips.
- The `parking-vision` image still **installs** `ultralytics` (the `vision` extra), as do `parking models export`, `parking benchmark` and `bootstrap-slots`' default. Leaving it out needs a second extra and image.
- The appearance scorer and the per-slot classifier (the methods the straight-down sample view uses today) are the project's own code and never used Ultralytics.

Whether any of this is needed depends on open question 10 (commercial use), which only Iulian can answer; the alternative is an Ultralytics Enterprise licence. This note is not legal advice.

## 7. Checked on the dev Pi (P9.4)

No real frames and no GPU exist yet, so this checks the tools, **not** the accuracy of any model. The frames are the simulated feed ([vision.md §10](vision.md#simulated-feed-parkingvisionsimulatepy-p212)): the same five cars moved around one photo.

- **Export.** `parking export-yolo --camera cam-ground --images data/replay/ground-sim --labels data/labels/cam-ground-sim.json --review` → 158 train + 42 val frames, 1692 boxes; the review pictures show each box on its space.
- **The dataset trains (route A).** `yolo detect train` on the Pi's CPU, `imgsz=640`, 8 epochs, 29 min: mAP50 0.995, mAP50-95 0.92 on the 42 val frames (the boxes are slot outlines, so this says the model found the taken spaces). Exported to NCNN and run through `parking evaluate --method detector --mode box_bottom --sweep 0.1:0.6:0.05` on `images/val` + `holdout.json`: 96.8% (691/714) at the default threshold 0.30, **100% (714/714) at 0.20–0.25**, about 50% from 0.35 up, because `box_bottom` is 35% of a box that is the space itself (§1). The appearance scorer on the same frames: 100%. A model that has seen five cars says nothing about the lot; the weights were deleted.
- **`YoloxDetector` (route B, inference only).** The official `yolox_nano.onnx` (416 px, COCO) on Ultralytics' `bus.jpg` test picture: the bus at 0.91 and three people at 0.68–0.85, boxes within 3 px of YOLO11n NCNN @ 640 on the bus (YOLO11n also finds a fourth, half-hidden person at 0.41). **52 ms** per frame (median of 20, pre- and post-processing included, 2 threads). Through `parking evaluate` with `type: yolox` on the simulated hold-out: 48.7%, every taken space said free: like YOLO11n, a COCO model doesn't see cars from straight above, which is what the training is for.
- YOLOX **training** (§4.2) was not run: no GPU, and no YOLOX install on the dev Pi.
- Tests: `tests/unit/test_yolox.py` (letterbox, grid decoding, class filter, per-class NMS, clipping, sidecar, input size, config rules, worker and `analyze` through a stand-in session), `test_yolo_dataset.py` (boxes, split, unsure slots, COCO rebuild, the command and its errors).
