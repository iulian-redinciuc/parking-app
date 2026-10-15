"""Synthetic Camera A reference frame: the underground ramp seen side-on (P5.1 PoC).

There is no ramp camera yet, so this renders the view hardware.md §2 asks for: the camera on the
wall opposite the ramp lane, ~2.5 m up, looking across it, so cars drive left/right through the
frame. 640×360 like the sub-stream. Scale: ~42 px per metre along the lane (wall panels are
2.5 m wide), so the frame shows ~15 m of lane: room for a car length between the lines and
~3 m or more before and after them. The ramp goes down to the left (into the garage).

    uv run python scripts/synth_ramp.py ../data/reference/cam-ramp.jpg

Deterministic (fixed seed). Not a real photo: it's only good for drawing and checking lines.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

W, H = 640, 360
PX_PER_M = 42
# lane edges: (y at x=0, y at x=W); the ramp rises 25 px to the right (towards the street)
FAR_EDGE = (176, 151)
NEAR_EDGE = (330, 305)
CEILING = (52, 30)  # underside of the slab above the back wall


def edge_y(edge: tuple[int, int], x: float) -> float:
    return edge[0] + (edge[1] - edge[0]) * x / W


def band(img: np.ndarray, top: tuple[int, int], bottom: tuple[int, int], colour) -> None:
    pts = np.array([[0, top[0]], [W, top[1]], [W, bottom[1]], [0, bottom[0]]], np.int32)
    cv2.fillPoly(img, [pts], colour)


def render(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((H, W, 3), np.uint8)

    # ceiling slab, back wall, lane, near kerb (BGR)
    band(img, (0, 0), CEILING, (52, 54, 58))
    band(img, CEILING, FAR_EDGE, (128, 132, 134))
    band(img, FAR_EDGE, NEAR_EDGE, (66, 68, 70))
    band(img, NEAR_EDGE, (H, H), (112, 116, 120))

    # wall: panel seams every 2.5 m, a darker skirting, hazard paint on the far kerb
    for x in range(40, W, int(2.5 * PX_PER_M)):
        cv2.line(
            img,
            (x, int(edge_y(CEILING, x)) + 2),
            (x, int(edge_y(FAR_EDGE, x)) - 14),
            (104, 108, 110),
            2,
        )
    for x0 in range(0, W, 24):
        colour = (20, 190, 230) if (x0 // 24) % 2 == 0 else (30, 30, 30)
        x1 = x0 + 24
        pts = np.array(
            [
                [x0, edge_y(FAR_EDGE, x0) - 12],
                [x1, edge_y(FAR_EDGE, x1) - 12],
                [x1, edge_y(FAR_EDGE, x1)],
                [x0, edge_y(FAR_EDGE, x0)],
            ],
            np.int32,
        )
        cv2.fillPoly(img, [pts], colour)

    # signs: level and clearance, a fire extinguisher box
    cv2.rectangle(img, (300, 70), (360, 112), (150, 60, 20), -1)
    cv2.putText(img, "-1", (309, 104), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (250, 250, 250), 2)
    cv2.rectangle(img, (470, 58), (560, 84), (20, 190, 230), -1)
    cv2.putText(img, "2.10 m", (478, 78), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 2)
    cv2.rectangle(img, (95, 104), (113, 132), (30, 30, 190), -1)

    # lane: edge lines, centre dashes, an arrow down the ramp, tyre wear
    for edge, off in ((FAR_EDGE, 6), (NEAR_EDGE, -6)):
        cv2.line(img, (0, int(edge[0] + off)), (W, int(edge[1] + off)), (200, 204, 206), 3)
    mid = ((FAR_EDGE[0] + NEAR_EDGE[0]) // 2, (FAR_EDGE[1] + NEAR_EDGE[1]) // 2)
    for x in range(10, W, 70):
        cv2.line(
            img, (x, int(edge_y(mid, x))), (x + 35, int(edge_y(mid, x + 35))), (190, 194, 196), 2
        )
    for edge in ((FAR_EDGE[0] + 40, FAR_EDGE[1] + 40), (NEAR_EDGE[0] - 30, NEAR_EDGE[1] - 30)):
        for x in range(0, W, 3):
            y = int(edge_y(edge, x) + rng.normal(0, 1.5))
            cv2.circle(img, (x, y), 3, (56, 57, 59), -1)
    arrow_y = int(edge_y(mid, 540)) + 26
    cv2.arrowedLine(img, (600, arrow_y - 2), (500, arrow_y), (200, 204, 206), 4, tipLength=0.3)

    # light: two wall lamps with glow, a fall-off towards the frame edges
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    light = np.full((H, W), 0.55, np.float32)
    for lx, ly in ((170, 62), (430, 50)):
        cv2.ellipse(img, (lx, ly), (22, 6), 0, 0, 360, (235, 245, 250), -1)
        light += 0.55 * np.exp(-((xx - lx) ** 2 / (2 * 160**2) + (yy - ly) ** 2 / (2 * 140**2)))
    out = img.astype(np.float32) * np.clip(light, 0, 1.25)[..., None]

    # concrete texture, sensor noise, a little softness like a compressed sub-stream
    texture = cv2.GaussianBlur(rng.normal(0, 7, (H, W)).astype(np.float32), (0, 0), 1.5)
    out += texture[..., None] + rng.normal(0, 3, out.shape).astype(np.float32)
    out = cv2.GaussianBlur(out, (0, 0), 0.7)
    return np.clip(out, 0, 255).astype(np.uint8)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("out", type=Path, help="JPEG to write, e.g. data/reference/cam-ramp.jpg")
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(a.out), render(a.seed), [cv2.IMWRITE_JPEG_QUALITY, 90]):
        raise SystemExit(f"can't write {a.out}")
    print(f"{W}x{H} -> {a.out}")


if __name__ == "__main__":
    main()
