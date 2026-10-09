"""Train the per-slot free/taken classifier and export it to ONNX (docs/design/vision.md §9).

Run it on a laptop/desktop with a GPU or on Google Colab, not on the dev Pi or the vision host:

    cd backend
    uv sync --extra vision          # torch + torchvision (on a GPU machine: the CUDA wheels)
    uv run --with onnx python scripts/train_slot_classifier.py --camera cam-ground \\
        --images ../data/validation/cam-ground --labels ../data/labels/cam-ground-validation.json \\
        [--crops ../data/pklot-crops] [--epochs 15] [--out models/slot_classifier.onnx]

Data:
- `--images` + `--labels`: frames labelled in the slot editor; each slot polygon from the
  camera's slot file is warped to a 128×128 crop exactly as at inference. `unsure` slots are
  skipped. 20% of the **frames** (`--holdout`, picked by a hash of the file name, so the same
  frames every run) are never trained on: their labels go to `<out>.holdout.json` for
  `parking evaluate --labels <out>.holdout.json` (the same validation set, unseen frames).
- `--crops DIR` (repeatable): ready-made crops in `DIR/free/` and `DIR/taken/`, e.g. cut from
  PKLot / CNRPark-EXT for pre-training (check their licences; never commit them).
- `--init FILE.pt`: start from an earlier run's weights (pre-train on public crops, then
  fine-tune on ours with a lower `--lr`).

Writes `<out>` (ONNX, dynamic batch), `<out>.pt` (weights for `--init`) and `<out>.json`
(classes, crop size, data counts, held-out accuracy), all under the git-ignored `models/`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from parking.config import cli_env, load_config, load_labels, load_slots
from parking.vision.occupancy import _scaled
from parking.vision.slot_classifier import CLASSES, CROP_SIZE, MEAN, STD, meta_path, slot_square

IMAGE_EXT = {".jpg", ".jpeg", ".png"}


def held_out(name: str, share: float) -> bool:
    """Deterministic frame split: the same frames are held out every run."""
    return int(hashlib.sha1(name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < share


def frame_crops(images: Path, labels_path: Path, slot_file, share: float):
    """(train, holdout) lists of (crop BGR, label) and the held-out labels subset."""
    labels = load_labels(labels_path)
    train, hold, hold_labels = [], [], {}
    for name, lab in sorted(labels.images.items()):
        img = cv2.imread(str(images / name))
        if img is None:
            print(f"skipped {name}: not in {images} or unreadable", file=sys.stderr)
            continue
        h, w = img.shape[:2]
        is_hold = held_out(name, share)
        if is_hold:
            hold_labels[name] = lab.model_dump()
        for slot in slot_file.slots:
            if slot.id in lab.unsure:
                continue
            poly = _scaled(slot.polygon, slot_file.image_size, (w, h))
            item = (slot_square(img, poly, CROP_SIZE), int(slot.id in lab.taken))
            (hold if is_hold else train).append(item)
    return train, hold, {"version": 1, "camera_id": labels.camera_id, "images": hold_labels}


def folder_crops(folder: Path):
    out = []
    for idx, cls in enumerate(CLASSES):
        for p in sorted((folder / cls).glob("*")):
            if p.suffix.lower() in IMAGE_EXT and (img := cv2.imread(str(p))) is not None:
                out.append((cv2.resize(img, (CROP_SIZE, CROP_SIZE)), idx))
    return out


def build_model(pretrained: bool):
    import torch.nn as nn
    from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

    weights = MobileNet_V3_Small_Weights.IMAGENET1K_V1 if pretrained else None
    model = mobilenet_v3_small(weights=weights)
    model.classifier[-1] = nn.Linear(model.classifier[-1].in_features, len(CLASSES))
    return model


def to_tensor(items, augment: bool):
    """(N, 3, S, S) normalised RGB batch; light augmentation for training."""
    import torch

    arr = []
    for crop, _ in items:
        c = crop
        if augment:
            if random.random() < 0.5:
                c = c[:, ::-1]
            if random.random() < 0.5:
                c = c[::-1]
            # brightness/contrast jitter (day/dusk/overcast) and a small shift (calibration slop)
            c = np.clip(c * random.uniform(0.7, 1.3) + random.uniform(-25, 25), 0, 255)
            dx, dy = (random.randint(-6, 6) for _ in range(2))
            m = np.float32([[1, 0, dx], [0, 1, dy]])
            c = cv2.warpAffine(c.astype(np.uint8), m, (CROP_SIZE, CROP_SIZE), borderMode=1)
        arr.append(np.asarray(c, np.uint8)[..., ::-1])
    batch = (np.stack(arr).astype(np.float32) / 255.0 - MEAN) / STD
    x = torch.from_numpy(np.ascontiguousarray(batch.transpose(0, 3, 1, 2)))
    y = torch.tensor([lab for _, lab in items])
    return x, y


def accuracy(model, items, device, batch: int) -> float | None:
    import torch

    if not items:
        return None
    model.eval()
    correct = 0
    with torch.no_grad():
        for i in range(0, len(items), batch):
            x, y = to_tensor(items[i : i + batch], augment=False)
            correct += int((model(x.to(device)).argmax(1).cpu() == y).sum())
    return correct / len(items)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=Path, default=Path("../config/lot.yaml"))
    ap.add_argument("--camera", help="camera id (for its slot file); needed with --images")
    ap.add_argument("--images", type=Path, help="folder of labelled frames")
    ap.add_argument("--labels", type=Path, help="labels file for --images")
    ap.add_argument("--crops", type=Path, action="append", default=[], help="DIR/{free,taken}/")
    ap.add_argument("--holdout", type=float, default=0.2, help="share of frames held out")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--init", type=Path, help="start from these weights (.pt)")
    ap.add_argument("--no-pretrained", action="store_true", help="no ImageNet weights")
    ap.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path("../models/slot_classifier.onnx"))
    a = ap.parse_args(argv)

    import torch

    random.seed(a.seed)
    torch.manual_seed(a.seed)
    if a.device == "auto":
        a.device = "cuda" if torch.cuda.is_available() else "cpu"

    train, hold, hold_labels = [], [], None
    if a.images or a.labels:
        if not (a.images and a.labels and a.camera):
            ap.error("--images needs --labels and --camera")
        root = a.config.resolve().parent.parent  # paths in lot.yaml are relative to the repo
        cam = load_config(a.config, cli_env(root)).camera(a.camera)
        slot_file = load_slots(root / cam.slots_file)  # an absolute slots_file wins
        train, hold, hold_labels = frame_crops(a.images, a.labels, slot_file, a.holdout)
    for folder in a.crops:
        train += folder_crops(folder)
    if not train:
        ap.error("no training crops: give --images/--labels and/or --crops")
    taken = sum(lab for _, lab in train)
    print(f"train: {len(train)} crops ({taken} taken), held out: {len(hold)} crops")

    device = torch.device(a.device)
    model = build_model(pretrained=not a.no_pretrained and a.init is None)
    if a.init:
        model.load_state_dict(torch.load(a.init, map_location="cpu"))
    model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, max(a.epochs, 1))
    # balance the classes: a lot is mostly one or the other for hours
    w = torch.tensor([len(train) / max(len(train) - taken, 1), len(train) / max(taken, 1)])
    loss_fn = torch.nn.CrossEntropyLoss(weight=(w / w.sum()).to(device))

    best, best_state = -1.0, None
    for epoch in range(1, a.epochs + 1):
        t0 = time.monotonic()
        model.train()
        random.shuffle(train)
        total = 0.0
        for i in range(0, len(train), a.batch):
            x, y = to_tensor(train[i : i + a.batch], augment=True)
            opt.zero_grad()
            loss = loss_fn(model(x.to(device)), y.to(device))
            loss.backward()
            opt.step()
            total += loss.item() * len(y)
        sched.step()
        acc = accuracy(model, hold, device, a.batch)
        shown = "n/a" if acc is None else f"{acc * 100:.1f}%"
        print(
            f"epoch {epoch}/{a.epochs}: loss {total / len(train):.4f}, "
            f"held-out accuracy {shown}, {time.monotonic() - t0:.0f} s"
        )
        score = acc if acc is not None else -total  # no hold-out: keep the last epoch
        if acc is None or score >= best:
            best = score
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    model.load_state_dict(best_state)
    model.to("cpu").eval()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    torch.save(best_state, a.out.with_suffix(".pt"))
    torch.onnx.export(
        model,
        (torch.zeros(1, 3, CROP_SIZE, CROP_SIZE),),
        str(a.out),
        input_names=["crops"],
        output_names=["logits"],
        dynamic_axes={"crops": {0: "n"}, "logits": {0: "n"}},
        opset_version=17,
        dynamo=False,
    )
    meta = {
        "classes": list(CLASSES),
        "crop_size": CROP_SIZE,
        "trained": datetime.now(UTC).isoformat(timespec="seconds"),
        "train_crops": len(train),
        "train_taken": taken,
        "holdout_crops": len(hold),
        "holdout_accuracy": None if not hold else round(best, 4),
        "camera_id": a.camera,
        "epochs": a.epochs,
        "init": str(a.init) if a.init else ("none" if a.no_pretrained else "imagenet"),
    }
    meta_path(a.out).write_text(json.dumps(meta, indent=2) + "\n")
    if hold_labels and hold_labels["images"]:
        holdout = a.out.with_suffix(".holdout.json")
        holdout.write_text(json.dumps(hold_labels, indent=2) + "\n")
        print(f"held-out frames: {len(hold_labels['images'])} -> {holdout}")
    print(f"wrote {a.out}, {a.out.with_suffix('.pt')}, {meta_path(a.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
