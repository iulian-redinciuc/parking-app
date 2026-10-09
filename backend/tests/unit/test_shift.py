"""Camera shift detection (vision/shift.py) on generated scenes."""

import cv2
import numpy as np
import pytest

from parking.config import HealthCfg, SlotFile
from parking.vision.shift import SHIFT_CHECKS, ShiftDetector, background_mask

W, H = 960, 540
# one row of spaces across the middle; everything else is "background"
SLOT_POLY = [[200, 180], [760, 180], [760, 360], [200, 360]]


def scene(seed: int = 0, w: int = W, h: int = H) -> np.ndarray:
    """A lot-like view: grey gradient with lots of random shapes (corners for ORB)."""
    rng = np.random.default_rng(seed)
    img = np.tile(np.linspace(70, 150, w, dtype=np.uint8), (h, 1))
    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    for _ in range(400):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        s = int(rng.integers(4, max(5, w // 30)))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + s, y + int(rng.integers(4, s + 5))), color, -1)
        else:
            cv2.circle(img, (x, y), s // 2, color, -1)
    return img


def with_cars(img: np.ndarray, seed: int) -> np.ndarray:
    """New "cars" (plain rectangles) inside the slot row only."""
    rng = np.random.default_rng(seed)
    out = img.copy()
    sx, sy = out.shape[1] / W, out.shape[0] / H
    for x in range(220, 740, 110):
        if rng.random() < 0.6:
            color = tuple(int(c) for c in rng.integers(0, 256, 3))
            cv2.rectangle(
                out,
                (round(x * sx), round(200 * sy)),
                (round((x + 80) * sx), round(340 * sy)),
                color,
                -1,
            )
    return out


def moved(img: np.ndarray, dx: float = 0, dy: float = 0, angle: float = 0) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    m[:, 2] += (dx, dy)
    return cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REFLECT)


def slots(polys, size=(W, H)) -> SlotFile:
    return SlotFile.model_validate(
        {
            "version": 1,
            "camera_id": "cam",
            "image_size": list(size),
            "slots": [{"id": f"S{i}", "zone": "z", "polygon": p} for i, p in enumerate(polys)],
        }
    )


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_background_mask_is_the_inverse_of_the_slots():
    mask = background_mask(slots([SLOT_POLY]), (W // 2, H // 2))  # slots scale to the size
    assert mask.shape == (H // 2, W // 2)
    assert mask[135, 240] == 0  # inside the slot
    assert mask[92, 240] == 0  # just outside: grown by the margin
    assert mask[20, 20] == 255
    assert (background_mask(None, (W, H)) == 255).all()
    assert (background_mask(slots([]), (W, H)) == 255).all()
    everything = [[0, 0], [W, 0], [W, H], [0, H]]
    assert (background_mask(slots([everything]), (W, H)) == 255).all()  # nothing left: whole


def test_same_view_measures_about_zero_even_with_new_cars():
    ref = scene()
    d = ShiftDetector(ref, slots([SLOT_POLY]))
    m = d.measure(ref)
    assert m.displacement_px == pytest.approx(0, abs=0.5)
    assert m.inliers >= 50
    m = d.measure(with_cars(ref, 7))
    assert m.displacement_px is not None and m.displacement_px < 1.5


@pytest.mark.parametrize(
    ("kw", "expected"),
    [({"dx": 20}, 20), ({"dx": -6, "dy": 8}, 10), ({"dy": 3}, 3)],
)
def test_translation_is_measured_in_reference_pixels(kw, expected):
    ref = scene(1)
    d = ShiftDetector(ref, slots([SLOT_POLY]))
    m = d.measure(with_cars(moved(ref, **kw), 3))
    assert m.displacement_px == pytest.approx(expected, abs=1.0)


def test_rotation_moves_the_background():
    ref = scene(2)
    m = ShiftDetector(ref, slots([SLOT_POLY])).measure(moved(ref, angle=2))
    assert m.displacement_px is not None and m.displacement_px > 8


def test_large_reference_is_measured_at_reference_size():
    ref = scene(3, 1920, 1080)  # worked on at 1280 px, reported at 1920
    d = ShiftDetector(ref, slots([SLOT_POLY]))
    assert d.scale == pytest.approx(1.5)
    m = d.measure(moved(ref, dx=24))
    assert m.displacement_px == pytest.approx(24, abs=1.5)
    # a frame at another size is compared at the reference's size
    small = cv2.resize(moved(ref, dx=24), (960, 540), interpolation=cv2.INTER_AREA)
    assert d.measure(small).displacement_px == pytest.approx(24, abs=2.5)


def test_featureless_frame_is_inconclusive():
    d = ShiftDetector(scene(4), None)
    m = d.measure(np.full((H, W, 3), 128, np.uint8))
    assert m.displacement_px is None


def test_a_different_view_is_far_over_the_limit():
    m = ShiftDetector(scene(4), None).measure(scene(99))  # the camera turned somewhere else
    assert m.displacement_px is not None and m.displacement_px > 100


def test_shifted_after_three_checks_in_a_row_and_never_clears():
    ref = scene(5)
    clock = Clock()
    cfg = HealthCfg(shift_check_every_s=300, shift_max_px=8)
    d = ShiftDetector(ref, slots([SLOT_POLY]), cfg, clock)
    bumped = moved(ref, dx=15)
    flat = np.full((H, W, 3), 128, np.uint8)

    assert d.check(bumped) is not None and d.over == 1
    clock.t = 100
    assert d.check(bumped) is None  # not due yet
    assert d.check(bumped, force=True) is not None and d.over == 2
    clock.t = 400
    d.check(ref)  # back in place: the run starts over
    assert d.over == 0 and not d.shifted
    for i in range(SHIFT_CHECKS):
        clock.t = 1000 + 600 * i
        d.check(bumped)
        if i == 0:
            clock.t += 300
            d.check(flat)  # inconclusive: neither counts nor resets
            assert d.over == 1
    assert d.shifted and d.last.displacement_px == pytest.approx(15, abs=1)
    clock.t = 5000
    d.check(ref)
    assert d.shifted  # never auto-corrects
    d.reset()
    assert not d.shifted and d.over == 0 and d.due()
