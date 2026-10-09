"""Two-line counter (vision/flow.py) on synthetic tracks, no model."""

from parking.config import LineFile
from parking.vision.detector import Detection
from parking.vision.flow import TRACK_TTL_S, TwoLineCounter

FPS = 10.0


def lines(**changes) -> LineFile:
    # horizontal lines across x 100..540: A at y = 150, B at y = 250; IN = downwards
    data = {
        "version": 1,
        "camera_id": "cam-ramp",
        "image_size": [640, 360],
        "line_a": [[100, 150], [540, 150]],
        "line_b": [[100, 250], [540, 250]],
        "in_direction": "a_to_b",
    }
    data.update(changes)
    return LineFile.model_validate(data)


def car(tid: int, x: float, y: float, conf: float = 0.8) -> Detection:
    """A box whose anchor (bottom-centre) is at (x, y)."""
    return Detection("car", conf, (x - 40, y - 60, x + 40, y), track_id=tid)


def drive(counter, tid, ys, x=320.0, t0=0.0):
    events = []
    for i, y in enumerate(ys):
        events += counter.update([car(tid, x, y)], t0 + i / FPS)
    return events


def path(y0: float, y1: float, n: int = 20) -> list[float]:
    return [y0 + (y1 - y0) * i / (n - 1) for i in range(n)]


def test_a_then_b_is_in():
    ev = drive(TwoLineCounter(lines()), 1, path(100, 300))
    assert [(e.direction, e.track_id, e.cls) for e in ev] == [("in", 1, "car")]
    assert ev[0].conf == 0.8


def test_b_then_a_is_out():
    ev = drive(TwoLineCounter(lines()), 1, path(300, 100))
    assert [e.direction for e in ev] == ["out"]


def test_in_direction_flip():
    c = TwoLineCounter(lines(in_direction="b_to_a"))
    assert [e.direction for e in drive(c, 1, path(100, 300))] == ["out"]
    assert [e.direction for e in drive(c, 2, path(300, 100), t0=10)] == ["in"]


def test_touch_a_then_reverse_is_nothing():
    ev = drive(TwoLineCounter(lines()), 1, path(100, 200) + path(200, 100))
    assert ev == []


def test_jitter_on_a_line_is_nothing():
    ys = [100, 120, 140] + [148, 152, 147, 153, 150, 149, 151] * 5 + [140, 120]
    c = TwoLineCounter(lines())
    assert drive(c, 1, ys) == []
    assert drive(c, 2, [230, 245] + [248, 252, 247, 253] * 5 + [240], t0=10) == []


def test_one_event_per_track():
    c = TwoLineCounter(lines())
    ys = path(100, 300) + path(300, 100) + path(100, 300)  # back and forth
    ev = drive(c, 1, ys)
    assert len(ev) == 1


def test_short_tracks_are_ignored():
    c = TwoLineCounter(lines(), min_track_frames=5)
    assert drive(c, 1, [100, 200, 300]) == []  # crossed in 3 frames
    # a track that crossed early is counted once it is long enough
    ev = drive(TwoLineCounter(lines(), min_track_frames=5), 2, [100, 200, 300, 310, 320])
    assert [(e.direction, e.ts) for e in ev] == [("in", 0.4)]


def test_crossing_beyond_the_line_ends_is_not_counted():
    # passes the lines' extensions at x = 600 (they end at 540)
    assert drive(TwoLineCounter(lines()), 1, path(100, 300), x=600) == []


def test_several_cars_at_once():
    c = TwoLineCounter(lines())
    down, up = path(100, 300), path(300, 100)
    events = []
    for i in range(len(down)):
        events += c.update([car(1, 200, down[i]), car(2, 450, up[i])], i / FPS)
    assert sorted((e.track_id, e.direction) for e in events) == [(1, "in"), (2, "out")]


def test_lines_are_scaled_to_the_frame_size():
    c = TwoLineCounter(lines())
    # a 1280×720 frame: lines at y = 300 and 500
    ev = []
    for i, y in enumerate(path(200, 600)):
        ev += c.update([car(1, 640, y)], i / FPS, frame_size=(1280, 720))
    assert [e.direction for e in ev] == ["in"]
    # y 200..280 crosses both lines at 640×360 but neither at 1280×720
    ev = []
    for i, y in enumerate(path(200, 280)):
        ev += c.update([car(2, 640, y)], 3 + i / FPS, frame_size=(1280, 720))
    assert ev == []


def test_tracks_are_forgotten_after_5_s():
    c = TwoLineCounter(lines())
    drive(c, 1, path(100, 200, 10))  # crossed A only
    assert 1 in c.state
    c.update([car(2, 320, 50)], 0.9 + TRACK_TTL_S + 0.1)
    assert 1 not in c.state and 2 in c.state


def test_detections_without_track_id_are_ignored():
    c = TwoLineCounter(lines())
    assert c.update([Detection("car", 0.9, (0, 0, 10, 10))], 0.0) == []
    assert c.state == {}
