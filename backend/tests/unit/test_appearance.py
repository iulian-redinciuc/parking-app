"""Top-down appearance scoring on synthetic images (vision.md §2.1)."""

import cv2
import numpy as np
import pytest

from parking.config import AppearanceCfg, Slot
from parking.vision.appearance import (
    frame_pavement,
    illumination,
    is_grey,
    match_light,
    pavement_model,
    score_slots_appearance,
    slot_crop,
    to_lab,
)

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


def test_reference_follows_the_light():
    """The reference's pavement colour is scaled to the frame's light (free slots vote)."""
    ref = pavement(seed=4)
    for gain in (0.85, 1.15):
        img, expected = scene()
        img = np.clip(img.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        results = score_slots_appearance(img, SLOTS, (W, H), reference=ref)
        assert taken(results) == expected, gain
        assert max(r.score for r in results if not r.taken) < 0.1
        labs = [to_lab(slot_crop(img, s.polygon, 0.12)) for s in SLOTS]
        ref_labs = [to_lab(slot_crop(ref, s.polygon, 0.12)) for s in SLOTS]
        assert illumination(labs, ref_labs) == pytest.approx(gain, abs=0.05)


def test_illumination_ignores_cars_and_needs_a_free_slot():
    ref = pavement(seed=4)
    ref_labs = [to_lab(slot_crop(ref, s.polygon, 0.12)) for s in SLOTS]
    img = np.clip(pavement(seed=3).astype(np.float32) * 0.9, 0, 255).astype(np.uint8)
    for i in range(N - 1):  # seven cars of one colour, one free slot
        car(img, i, (150, 150, 150))
    labs = [to_lab(slot_crop(img, s.polygon, 0.12)) for s in SLOTS]
    assert illumination(labs, ref_labs) == pytest.approx(0.9, abs=0.04)
    car(img, N - 1, (150, 150, 150))  # full: nothing to go by
    labs = [to_lab(slot_crop(img, s.polygon, 0.12)) for s in SLOTS]
    assert illumination(labs, ref_labs) == 1.0
    assert len(taken(score_slots_appearance(img, SLOTS, (W, H), reference=ref))) == N


def cast(img: np.ndarray, gains=(0.7, 0.8, 0.84)) -> np.ndarray:
    """Another light: darker and warmer (less blue), as per-channel BGR gains."""
    return np.clip(img.astype(np.float32) * np.float32(gains), 0, 255).astype(np.uint8)


def crops(img, ref):
    labs = [to_lab(slot_crop(img, s.polygon, 0.12)) for s in SLOTS]
    ref_labs = [to_lab(slot_crop(ref, s.polygon, 0.12)) for s in SLOTS]
    return labs, ref_labs, pavement_model(ref_labs)[0]


def test_reference_follows_a_colour_cast(monkeypatch):
    """Darker and warmer light (P2.13): the free slots give the pavement's colour, not only
    its lightness."""
    ref = pavement(seed=4)
    img = pavement()
    car(img, 0, (25, 25, 30))
    car(img, 2, (190, 110, 40))
    car(img, 5, (235, 235, 235), dx=30)
    shadow(img, 1)
    moss(img, 3)
    img, expected = cast(img), {"G01", "G03", "G06"}
    results = score_slots_appearance(img, SLOTS, (W, H), reference=ref)
    assert taken(results) == expected
    assert max(r.score for r in results if not r.taken) < 0.1
    labs, ref_labs, colour = crops(img, ref)
    seen = frame_pavement(labs, ref_labs, colour)
    assert 0.75 * colour[0] < seen[0] < 0.9 * colour[0]
    assert 3 < seen[2] - colour[2] < 8  # more yellow than the reference
    # without relighting (P4.9), lightness alone (`illumination`) calls free spaces taken
    monkeypatch.setattr("parking.vision.appearance.CAST_MIN_SLOTS", N + 1)
    plain = AppearanceCfg(match_light=False)
    assert (
        taken(score_slots_appearance(img, SLOTS, (W, H), params=plain, reference=ref)) != expected
    )
    # relit to the reference's light first, it needs neither
    assert taken(score_slots_appearance(img, SLOTS, (W, H), reference=ref)) == expected


def test_frame_pavement_needs_three_agreeing_free_slots():
    ref = pavement(seed=4)
    img = cast(pavement(seed=3))
    for i in range(N - 2):  # six evenly grey cars, two free slots
        cv2.rectangle(
            img, (X0 + i * SLOT_W, Y0), (X0 + (i + 1) * SLOT_W, Y0 + SLOT_H), (150,) * 3, -1
        )
    assert frame_pavement(*crops(img, ref)) is None
    img = cast(pavement(seed=3))
    for i in range(N - 3):  # three free: enough, and the cars don't pull the colour
        cv2.rectangle(
            img, (X0 + i * SLOT_W, Y0), (X0 + (i + 1) * SLOT_W, Y0 + SLOT_H), (150,) * 3, -1
        )
    labs, ref_labs, colour = crops(img, ref)
    seen = frame_pavement(labs, ref_labs, colour)
    assert 3 < seen[2] - colour[2] < 8
    results = score_slots_appearance(img, SLOTS, (W, H), reference=ref)
    assert taken(results) == {s.id for s in SLOTS[: N - 3]}


def test_frame_pavement_lightness_ignores_where_the_free_slots_are():
    """The lot is brighter on one side; only dim-side slots are free. The colour's lightness
    is still the reference's (per-slot ratios), so the bright slot isn't called taken."""
    ramp = np.linspace(1.25, 0.8, W, dtype=np.float32)[None, :, None]
    lit = lambda im: np.clip(im.astype(np.float32) * ramp, 0, 255).astype(np.uint8)  # noqa: E731
    ref = lit(pavement(seed=4))
    img = pavement(seed=3)
    for i in range(1, N - 3):
        car(img, i, (30, 30, 35))
    img = lit(img)
    labs, ref_labs, colour = crops(img, ref)
    assert frame_pavement(labs, ref_labs, colour)[0] == pytest.approx(colour[0], abs=1.5)
    results = score_slots_appearance(img, SLOTS, (W, H), reference=ref)
    assert taken(results) == {s.id for s in SLOTS[1 : N - 3]}


def night(img: np.ndarray) -> np.ndarray:
    """The camera's infrared mode: grey, lit from the camera (bright in the middle)."""
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    x = np.linspace(-1, 1, W, dtype=np.float32)[None, :]
    grey *= 0.35 + 0.45 * np.exp(-((x / 0.7) ** 2))
    return np.repeat(np.clip(grey, 0, 255).astype(np.uint8)[..., None], 3, axis=2)


def test_infrared_night_relit_against_a_grey_reference():
    """P4.9: a grey, unevenly lit frame against the colour reference. Relit to the reference's
    light, and with no sun shadows to excuse a darker car, the cars are found."""
    ref = pavement(seed=4)
    img = pavement()
    car(img, 0, (25, 25, 30))
    car(img, 2, (190, 110, 40))
    car(img, 5, (230, 230, 230), dx=30)  # white: a car of the pavers' own grey would vanish
    img = night(img)
    assert is_grey(img) and not is_grey(ref)
    results = score_slots_appearance(img, SLOTS, (W, H), reference=ref)
    assert taken(results) == {"G01", "G03", "G06"}
    plain = AppearanceCfg(match_light=False)
    assert taken(score_slots_appearance(img, SLOTS, (W, H), params=plain, reference=ref)) != {
        "G01",
        "G03",
        "G06",
    }


def test_dark_warm_dusk_relit():
    """P4.9: dusk before the camera's night mode: a fifth of the light, warmer. Beyond what
    the free slots' lightness ratio may vote for, but the background still gives the gain."""
    ref = pavement(seed=4)
    img, expected = scene()
    img = cast(img, (0.15, 0.2, 0.24))
    results = score_slots_appearance(img, SLOTS, (W, H), reference=ref)
    assert taken(results) == expected
    plain = AppearanceCfg(match_light=False)
    assert (
        taken(score_slots_appearance(img, SLOTS, (W, H), params=plain, reference=ref)) != expected
    )


def test_match_light_needs_background():
    """Slots that cover the whole picture leave nothing to relight by: the frame is kept."""
    ref = pavement(seed=4)
    img = cast(pavement(seed=3))
    relit, _ = match_light(img, ref, [rect(0, 0, W, H)])
    assert relit is img
    relit, same = match_light(img, ref, [s.polygon for s in SLOTS])
    assert same.shape == img.shape
    # outside the slots the relit frame looks like the reference again
    band = (slice(0, Y0 - 5), slice(None))
    assert np.abs(relit[band].mean((0, 1)) - ref[band].mean((0, 1))).max() < 3


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
