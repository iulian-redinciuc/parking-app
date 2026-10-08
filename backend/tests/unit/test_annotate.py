import numpy as np
import pytest

from parking.config import Slot
from parking.vision.annotate import (
    GREEN,
    RED,
    YELLOW,
    annotate_occupancy,
    banner_text,
    text_scale,
)
from parking.vision.detector import Detection
from parking.vision.occupancy import SlotResult

W, H = 640, 480


def slot(sid, x1, y1, x2, y2):
    return Slot(id=sid, zone="ground", polygon=[(x1, y1), (x2, y1), (x2, y2), (x1, y2)])


SLOTS = [slot("G01", 100, 150, 250, 400), slot("G02", 350, 150, 500, 400)]
RESULTS = [SlotResult("G01", 0.82, True), SlotResult("G02", 0.04, False)]


def grey():
    return np.full((H, W, 3), 128, np.uint8)


def test_banner_text():
    assert banner_text({"ground": (12, 40)}, 143.4) == "Ground: 12 free / 40 | 143 ms"
    assert banner_text({"ground": (1, 2), "underground": (0, 5)}) == (
        "Ground: 1 free / 2 | Underground: 0 free / 5"
    )
    assert banner_text({}) == ""


def test_text_scale_grows_with_width():
    assert text_scale(1280) == pytest.approx(1.0)
    assert text_scale(3840) == pytest.approx(3.0)
    assert text_scale(320) == 0.5  # never unreadably small


def test_does_not_modify_input_and_keeps_shape():
    frame = grey()
    out = annotate_occupancy(frame, SLOTS, RESULTS, [], {"ground": (1, 2)}, 143)
    assert out.shape == frame.shape and out.dtype == np.uint8
    assert (frame == 128).all()


def test_taken_slot_is_filled_red_and_free_slot_is_not():
    out = annotate_occupancy(grey(), SLOTS, RESULTS, [], {})
    taken_px = out[180, 130].astype(int)  # inside G01, away from the label
    free_px = out[180, 380].astype(int)  # inside G02
    assert taken_px[2] > taken_px[0] + 40  # reddish (BGR)
    assert (free_px == 128).all()


def test_outline_colours():
    out = annotate_occupancy(grey(), SLOTS, RESULTS, [], {})
    assert tuple(out[275, 500]) == GREEN  # G02's right edge
    assert tuple(out[275, 100]) == RED  # G01's left edge


def near(px, color, tol=40):
    return all(abs(int(a) - b) <= tol for a, b in zip(px, color, strict=True))


def test_detections_drawn_in_yellow_box_or_mask():
    box_only = Detection("car", 0.9, (20, 60, 80, 120))
    masked = Detection(
        "car", 0.9, (520, 60, 620, 140), mask=np.array([[530, 70], [610, 70], [570, 130]], "f4")
    )
    out = annotate_occupancy(grey(), [], [], [box_only, masked], {})
    assert near(out[90, 20], YELLOW)  # left side of the box
    assert near(out[70, 570], YELLOW)  # top edge of the mask triangle
    assert not near(out[60, 570], YELLOW)  # the masked detection's box is not drawn


def test_banner_darkens_top_and_scaled_slots():
    # slot file drawn on a half-size image: polygons must be scaled up 2x
    small = [slot("G01", 50, 75, 125, 200)]
    out = annotate_occupancy(
        grey(), small, [SlotResult("G01", 0.9, True)], [], {"ground": (0, 1)}, image_size=(320, 240)
    )
    assert out[2, W - 3].max() < 128  # banner band
    assert tuple(out[275, 100]) == RED  # left edge at x = 50 * 2


def test_slot_without_result_is_free():
    out = annotate_occupancy(grey(), SLOTS[:1], [], [], {})
    assert tuple(out[275, 100]) == GREEN


@pytest.mark.parametrize("width", [1280, 3840])
def test_long_banner_fits(width):
    frame = np.zeros((width * 9 // 16, width, 3), np.uint8)
    totals = {f"zone{i}": (10, 100) for i in range(6)}
    out = annotate_occupancy(frame, [], [], [], totals, 143)
    assert out[:, -int(width * 0.01) :].max() == 0  # no text past the right margin


def test_label_of_edge_slot_stays_in_frame():
    edge = [slot("G99", W - 20, 150, W + 100, 400)]  # mostly outside the frame
    out = annotate_occupancy(grey(), edge, [SlotResult("G99", 0.5, True)], [], {})
    assert (out[150:400, W - 40 :].min(axis=2) > 200).any()  # white label text pulled inside
