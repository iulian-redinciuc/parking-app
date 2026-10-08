"""Top-down appearance scoring on synthetic images (vision.md §2.1)."""

import cv2
import numpy as np
import pytest

from parking.config import AppearanceCfg, Slot
from parking.vision.appearance import score_slots_appearance, slot_crop

W, H = 1040, 300  # frame size
SLOT_W, SLOT_H, X0, Y0 = 120, 220, 40, 40
N = 8
PAVEMENT = (115, 135, 155)  # BGR, brownish grey like the sample's pavers


def rect(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


SLOTS = [
    Slot(
        id=f"G{i + 1:02}",
        zone="ground",
        polygon=rect(X0 + i * SLOT_W, Y0, X0 + (i + 1) * SLOT_W, Y0 + SLOT_H),
    )
    for i in range(N)
]


def pavement(seed=0) -> np.ndarray:
    """Textured pavers with darker joints and painted white space lines."""
    rng = np.random.default_rng(seed)
    img = np.empty((H, W, 3), np.float32)
    img[:] = PAVEMENT
    img += rng.normal(0, 6, (H, W, 1))  # brightness texture
    img[::12, :] *= 0.8  # paver joints
    img[:, ::24] *= 0.8
    img = np.clip(img, 0, 255).astype(np.uint8)
    for i in range(N + 1):  # painted lines between spaces
        x = X0 + i * SLOT_W
        cv2.line(img, (x, Y0), (x, Y0 + SLOT_H), (235, 235, 235), 5)
    return img


def car(img, slot: int, colour, dx=0):
    """A car roughly filling a space (shifted by `dx` px)."""
    x = X0 + slot * SLOT_W + 15 + dx
    cv2.rectangle(img, (x, Y0 + 20), (x + SLOT_W - 30, Y0 + SLOT_H - 20), colour, -1)
    # windscreen and roof highlights, as seen from above
    cv2.rectangle(img, (x + 10, Y0 + 50), (x + SLOT_W - 40, Y0 + 80), (60, 60, 60), -1)


def shadow(img, slot: int, factor=0.6):
    """A car's shadow: the same pavement, only darker, over half the space."""
    x = X0 + slot * SLOT_W
    region = img[Y0 : Y0 + SLOT_H, x : x + SLOT_W // 2 + 20]
    region[:] = (region.astype(np.float32) * factor).astype(np.uint8)


def moss(img, slot: int, seed=1):
    """Dark green specks scattered over a free space."""
    rng = np.random.default_rng(seed)
    x = X0 + slot * SLOT_W
    for _ in range(25):
        cx, cy = rng.integers(x + 10, x + SLOT_W - 10), rng.integers(Y0 + 10, Y0 + SLOT_H - 10)
        cv2.circle(img, (int(cx), int(cy)), 3, (40, 70, 50), -1)


def scene() -> tuple[np.ndarray, set[str]]:
    img = pavement()
    car(img, 0, (25, 25, 30))  # black
    car(img, 2, (190, 110, 40))  # blue
    car(img, 5, (150, 150, 150), dx=30)  # grey, poking into G07
    shadow(img, 1)  # the black car's shadow on a free space
    moss(img, 3)
    return img, {"G01", "G03", "G06"}


def taken(results) -> set[str]:
    return {r.id for r in results if r.taken}


def test_scene_scored_correctly():
    img, expected = scene()
    results = score_slots_appearance(img, SLOTS, (W, H))
    assert [r.id for r in results] == [s.id for s in SLOTS]
    assert taken(results) == expected
    assert all(0.0 <= r.score <= 1.0 for r in results)
    by_id = {r.id: r.score for r in results}
    assert by_id["G02"] < 0.1  # shadow
    assert by_id["G04"] < 0.1  # moss
    assert by_id["G07"] < 0.3  # the grey car's overhang
    assert min(by_id[s] for s in expected) > 0.5


def test_empty_lot_is_free():
    results = score_slots_appearance(pavement(), SLOTS, (W, H))
    assert taken(results) == set()


def test_threshold_boundary():
    img, _ = scene()
    score = {r.id: r.score for r in score_slots_appearance(img, SLOTS, (W, H))}["G06"]
    at = score_slots_appearance(img, SLOTS, (W, H), threshold=score)
    above = score_slots_appearance(img, SLOTS, (W, H), threshold=min(score + 1e-6, 0.999))
    assert "G06" in taken(at)
    assert "G06" not in taken(above)


def test_shadow_counts_without_suppression():
    img, _ = scene()
    p = AppearanceCfg(shadow_l_range=(0.0, 0.0))  # no pixel is a shadow
    by_id = {r.id: r for r in score_slots_appearance(img, SLOTS, (W, H), params=p)}
    assert by_id["G02"].taken


def test_polygons_rescaled_from_image_size():
    img, expected = scene()
    small = cv2.resize(img, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    results = score_slots_appearance(small, SLOTS, (W // 2, H // 2), image_size=(W, H))
    assert taken(results) == expected


def test_reference_empty_handles_a_nearly_full_lot():
    img = pavement(seed=3)
    colours = [(25, 25, 30), (190, 110, 40), (40, 40, 180), (30, 30, 30), (200, 200, 40)]
    colours += [(60, 140, 60), (35, 35, 40)]
    for i, c in enumerate(colours):
        car(img, i, c)
    expected = {s.id for s in SLOTS[: len(colours)]}
    ref = cv2.resize(pavement(seed=4), (W * 2, H * 2))  # any size
    results = score_slots_appearance(img, SLOTS, (W, H), reference=ref)
    assert taken(results) == expected
    # without it the pooled median drifts towards car colours (the known MVP limit)
    assert taken(score_slots_appearance(img, SLOTS, (W, H))) != expected


def test_crop_upright_and_inset():
    img = pavement()
    crop = slot_crop(img, SLOTS[0].polygon, inset=0.0)
    assert crop.shape[:2] == (SLOT_H, SLOT_W)
    inset = slot_crop(img, SLOTS[0].polygon, inset=0.25)
    assert inset.shape[:2] == (SLOT_H - 2 * 55, SLOT_W - 2 * 30)
    # more than 4 points: the minimum-area rectangle
    five = [(40, 40), (100, 40), (160, 40), (160, 260), (40, 260)]
    assert sorted(slot_crop(img, five, 0.0).shape[:2]) == sorted((SLOT_H, SLOT_W))


@pytest.mark.parametrize("bad", [{"shadow_l_range": (0.9, 0.3)}, {"inset": 0.5}])
def test_params_validated(bad):
    with pytest.raises(ValueError):
        AppearanceCfg(**bad)
