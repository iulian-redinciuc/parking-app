"""A day of light on a feed of daytime images (P4.4; docs/design/vision.md §5, Tuning).

Stands in for 24 h of a real camera until there is one, so the frame-health thresholds can be
tuned and checked on images: `light()` shows a frame as the camera would see it at a given hour,
`covered()` a hand over the lens. A rough model, not a measurement:

- **day** (colour): the camera's auto-exposure keeps the picture bright until the light runs
  out; towards dusk and dawn it gets darker, warmer and noisier (more gain).
- **night** (`IR_FROM`..`IR_UNTIL`): the camera switches to infrared: grey, lit by its own LEDs
  (bright in the middle, dark at the edges), softer (its noise reduction) and noisier.
- **rain**: flatter and darker (overcast), streaks, a few blurred drops on the lens.

Frames are the same for the same `seed` and `index`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

SUNRISE, SUNSET = 7.0, 18.5  # lot-local hours (Bucharest in October, roughly)
IR_UNTIL, IR_FROM = 6.75, 18.75  # the camera's night mode, a little past sunrise / sunset
IR_MEAN = 70.0  # grey level the IR picture's auto-exposure aims for
DROPS = 6  # drops on the lens while it rains


@dataclass(frozen=True)
class Light:
    """What `light()` did at one hour (for the labels / the run's notes)."""

    hour: float
    phase: str  # day | dusk | night (IR)
    rain: bool


def daylight(hour: float) -> float:
    """0 at night, 1 at noon: the sun's height, roughly."""
    if not SUNRISE <= hour <= SUNSET:
        return 0.0
    return math.sin(math.pi * (hour - SUNRISE) / (SUNSET - SUNRISE))


def phase(hour: float) -> str:
    h = hour % 24
    if h < IR_UNTIL or h >= IR_FROM:
        return "night"
    return "dusk" if daylight(h) < 0.3 else "day"


def light(
    image: np.ndarray, hour: float, rain: bool = False, seed: int = 1, index: int = 0
) -> tuple[np.ndarray, Light]:
    """`image` (a daytime BGR frame) as the camera would see it at `hour` (0..24)."""
    rng = np.random.default_rng([seed, index])
    hour %= 24
    p = phase(hour)
    img = image.astype(np.float32)
    h, w = img.shape[:2]
    if p == "night":
        img = _infrared(img)
        noise, soften = 7.0, 2.2
    else:
        # auto-exposure keeps up until about an hour after sunrise / before sunset
        b = min(1.0, 0.22 + 1.6 * daylight(hour))
        warm = 1 - b
        img *= b
        img[..., 2] *= 1 + 0.18 * warm  # red
        img[..., 0] *= 1 - 0.25 * warm  # blue
        noise, soften = 1.5 + 9 * warm, 0.6 * warm
    if rain:
        img = _rain(img, rng, night=p == "night")
        soften += 0.8
    grey = p == "night"  # the IR picture's noise is grey too
    img += rng.normal(0, noise, (h, w, 1 if grey else 3)).astype(np.float32)
    if soften > 0:  # the camera's noise reduction, more of it the more gain
        sigma = soften * w / 1000
        img = cv2.GaussianBlur(img, (0, 0), sigma)
    if rain:
        img = _drops(img, rng)
    out = np.clip(img, 0, 255).astype(np.uint8)
    return out, Light(hour, p, rain)


def _infrared(img: np.ndarray) -> np.ndarray:
    """Grey, lit from the camera: bright in the middle, falling off towards the edges."""
    h, w = img.shape[:2]
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    grey = (grey - grey.mean()) * 0.75 + grey.mean()  # IR flattens paint colours
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    spot = np.exp(-(((x - w / 2) / (0.55 * w)) ** 2 + ((y - 0.6 * h) / (0.6 * h)) ** 2))
    grey *= 0.3 + 0.7 * spot
    grey *= IR_MEAN / max(float(grey.mean()), 1.0)
    return np.repeat(grey[..., None], 3, axis=2)


def _rain(img: np.ndarray, rng: np.random.Generator, night: bool) -> np.ndarray:
    """Overcast (darker, flatter) with streaks; at night the IR lights the drops near the lens."""
    h, w = img.shape[:2]
    mean = float(img.mean())
    img = (img - mean) * 0.7 + mean * (1.0 if night else 0.8)
    streaks = np.zeros((h, w), np.float32)
    n = int(w * h / 4000)
    xs = rng.integers(0, w, n)
    ys = rng.integers(0, h, n)
    length = max(int(h / 40), 3)
    for x0, y0 in zip(xs, ys, strict=True):
        cv2.line(streaks, (int(x0), int(y0)), (int(x0) + length // 6, int(y0) + length), 1.0, 1)
    streaks = cv2.GaussianBlur(streaks, (0, 0), 1.0)
    return img + streaks[..., None] * (60.0 if night else 25.0)


def _drops(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A few drops on the lens: blurred, slightly brighter discs."""
    h, w = img.shape[:2]
    for _ in range(DROPS):
        r = int(rng.uniform(0.02, 0.05) * w)
        cx, cy = int(rng.integers(r, w - r)), int(rng.integers(r, h - r))
        y0, y1, x0, x1 = cy - r, cy + r, cx - r, cx + r
        patch = img[y0:y1, x0:x1]
        blurred = cv2.GaussianBlur(patch, (0, 0), r / 3) * 1.1
        mask = np.zeros(patch.shape[:2], np.float32)
        cv2.circle(mask, (r, r), r, 1.0, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), r / 6)[..., None]
        img[y0:y1, x0:x1] = patch * (1 - mask) + blurred * mask
    return img


def covered(shape: tuple[int, ...], seed: int = 1) -> np.ndarray:
    """A hand over the lens: nearly black, a little light leaking in at one edge, sensor noise."""
    rng = np.random.default_rng(seed)
    h, w = shape[:2]
    leak = np.linspace(9.0, 1.0, h, dtype=np.float32)[:, None] * np.ones((1, w), np.float32)
    img = np.repeat(leak[..., None], 3, axis=2)
    img += rng.normal(0, 1.5, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)
