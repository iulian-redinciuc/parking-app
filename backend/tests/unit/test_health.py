"""Frame health checks (vision.md §5) on generated frames."""

from datetime import UTC, datetime, timedelta

import cv2
import numpy as np
import pytest

from parking.config import HealthCfg
from parking.vision.health import WINDOW, FrameHealth, blur_score, small_gray
from parking.vision.sources import Frame

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def texture(seed=0, h=480, w=640):
    """Sharp random texture with mid brightness."""
    rng = np.random.default_rng(seed)
    return rng.integers(40, 220, (h, w, 3), dtype=np.uint8)


def frame(img, s=0):
    return Frame(img, T0 + timedelta(seconds=s))


def test_small_gray_downscales_only():
    g = small_gray(texture(h=480, w=640))
    assert g.shape == (240, 320) and g.dtype == np.float32
    assert small_gray(np.zeros((50, 100), np.uint8)).shape == (50, 100)


def test_healthy_texture():
    h = FrameHealth()
    assert h.check(frame(texture())) is None
    assert h.last_issue is None and h.last_healthy_at == T0 and h.unhealthy_ratio == 0


def test_all_black():
    h = FrameHealth()
    assert h.check(frame(np.zeros((480, 640, 3), np.uint8))) == "black"
    dark = (texture() // 40).astype(np.uint8)  # mean ~3, still noisy
    assert h.check(frame(dark)) == "black"
    assert h.last_healthy_at is None


def test_black_threshold_from_config():
    img = np.full((480, 640, 3), 20, np.uint8)
    assert FrameHealth(HealthCfg(black_mean_max=25)).check(frame(img)) == "black"
    # not black with the default (12), but flat, so blurry
    assert FrameHealth().check(frame(img)) == "blurry"


def test_gaussian_blurred_texture_is_blurry():
    sharp = texture()
    blurred = cv2.GaussianBlur(sharp, (0, 0), sigmaX=6)
    assert blur_score(small_gray(sharp)) > 40 > blur_score(small_gray(blurred))
    assert FrameHealth().check(frame(blurred)) == "blurry"


def test_identical_frames_become_frozen_after_n_in_a_row():
    h = FrameHealth(HealthCfg(frozen_frames=3))
    img = texture()
    # frame 1 has no previous; frames 2-3 are 1-2 still diffs; frame 4 is the 3rd -> frozen
    got = [h.check(frame(img, i)) for i in range(5)]
    assert got == [None, None, None, "frozen", "frozen"]
    # a changed frame resets the run
    assert h.check(frame(texture(seed=1), 5)) is None
    assert h.check(frame(texture(seed=1), 6)) is None


def test_small_noise_still_counts_as_frozen():
    h = FrameHealth(HealthCfg(frozen_frames=2, frozen_diff_max=0.5))
    img = texture().astype(np.int16)
    rng = np.random.default_rng(3)
    issues = []
    for i in range(4):
        noisy = np.clip(img + rng.integers(-1, 2, img.shape), 0, 255).astype(np.uint8)
        issues.append(h.check(frame(noisy, i)))
    assert issues[-1] == "frozen"


def test_frozen_off_for_replay_sources():
    h = FrameHealth(HealthCfg(frozen_frames=1), check_frozen=False)
    img = texture()
    assert [h.check(frame(img, i)) for i in range(10)] == [None] * 10


def test_black_wins_over_frozen():
    h = FrameHealth(HealthCfg(frozen_frames=1))
    black = np.zeros((48, 64, 3), np.uint8)
    assert [h.check(frame(black, i)) for i in range(3)] == ["black"] * 3


def test_size_change_resets_frozen():
    h = FrameHealth(HealthCfg(frozen_frames=1))
    h.check(frame(texture()))
    assert h.check(frame(texture(h=240, w=640))) is None


def test_connect_failed():
    h = FrameHealth()
    assert h.check(None) == "connect_failed"
    assert h.check(frame(np.zeros((0, 0, 3), np.uint8))) == "connect_failed"
    assert h.unhealthy_ratio == 1.0


def test_unhealthy_ratio_rolls_over_last_20():
    h = FrameHealth(check_frozen=False)
    good, bad = texture(), np.zeros((48, 64, 3), np.uint8)
    assert h.unhealthy_ratio == 0.0 and not h.degraded
    for _ in range(10):
        h.check(frame(good))
    for _ in range(10):
        h.check(frame(bad))
    assert h.unhealthy_ratio == pytest.approx(0.5) and not h.degraded  # "more than 50%"
    h.check(None)  # pushes the oldest (good) result out
    assert h.unhealthy_ratio == pytest.approx(11 / 20) and h.degraded
    for _ in range(WINDOW):
        h.check(frame(bad))
    assert h.unhealthy_ratio == 1.0
    for _ in range(WINDOW):
        h.check(frame(good))
    assert h.unhealthy_ratio == 0.0


def test_is_down_after_stale_without_healthy_frame():
    h = FrameHealth()
    h.check(frame(texture(), 0))
    assert not h.is_down(60, now=T0 + timedelta(seconds=60))
    h.check(None)
    assert h.is_down(60, now=T0 + timedelta(seconds=61))


def test_is_down_counts_from_start_if_never_healthy():
    h = FrameHealth()
    h.check(None)
    now = datetime.now(UTC)
    assert not h.is_down(60, now=now)
    assert h.is_down(60, now=now + timedelta(seconds=61))
