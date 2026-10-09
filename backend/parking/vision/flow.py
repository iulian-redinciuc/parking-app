"""Two-line entry/exit counter for the flow camera (vision.md §7.3, P5.5).

`TwoLineCounter.update(tracks, ts)` follows each track's **anchor** (bottom-centre of its
box) and records every time it changes side of line A or line B. A track is counted once,
when its last two crossings are `a`, `b` (or `b`, `a`) and it has been seen for at least
`min_track_frames` frames: A then B = IN when the line file's `in_direction` is `a_to_b`.

- Touching one line and reversing gives `a`, `a`: nothing.
- Jitter on a line gives `a`, `a`, `a`, …: nothing.
- A side change only counts as a crossing where the anchor passes **between the line's
  endpoints** (the crossing point of its last move lies on the segment); passing beyond
  an end just updates the side.
- Lines are in the line file's `image_size` pixels and are scaled to the frame size given to
  `update` (default: the line file's own size).
- Tracks not seen for `TRACK_TTL_S` (5 s) are forgotten. Track ids are never reused while
  the process runs (`VehicleTracker`), so a forgotten id can't come back as another car.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from parking.config import LineFile
from parking.messages import Direction
from parking.vision.detector import Detection
from parking.vision.tracking import anchor

TRACK_TTL_S = 5.0

Point = tuple[float, float]


@dataclass(frozen=True)
class FlowEvent:
    """One counted crossing; the flow worker turns it into a `FlowEventMsg` (P5.6)."""

    direction: Direction
    track_id: int
    ts: float
    conf: float
    cls: str


@dataclass
class TrackState:
    first_ts: float
    last_ts: float
    frames: int = 0
    # per line: last non-zero side (+1/-1) and the anchor where it was seen
    last_side: dict[str, int | None] = field(default_factory=lambda: {"a": None, "b": None})
    last_point: dict[str, Point | None] = field(default_factory=lambda: {"a": None, "b": None})
    crossings: list[str] = field(default_factory=list)
    counted: bool = False


def _cross(line: tuple[Point, Point], p: Point) -> float:
    (x1, y1), (x2, y2) = line
    return (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1)


def _sign(v: float) -> int:
    return (v > 0) - (v < 0)


def _within_segment(line: tuple[Point, Point], prev: Point, p: Point) -> bool:
    """Does the move prev → p (which changes side of `line`) cross it between its ends?"""
    c0, c1 = _cross(line, prev), _cross(line, p)
    s = c0 / (c0 - c1)
    qx, qy = prev[0] + s * (p[0] - prev[0]), prev[1] + s * (p[1] - prev[1])
    (x1, y1), (x2, y2) = line
    dx, dy = x2 - x1, y2 - y1
    u = ((qx - x1) * dx + (qy - y1) * dy) / (dx * dx + dy * dy)
    return 0.0 <= u <= 1.0


class TwoLineCounter:
    def __init__(self, lines: LineFile, min_track_frames: int = 5):
        self.lines = lines
        self.min_track_frames = min_track_frames
        self.state: dict[int, TrackState] = {}
        self._size: tuple[int, int] | None = None
        self._lines: dict[str, tuple[Point, Point]] = {}
        self._scale(tuple(lines.image_size))

    def _scale(self, frame_size: tuple[int, int]) -> None:
        if frame_size == self._size:
            return
        sw, sh = self.lines.image_size
        fx, fy = frame_size[0] / sw, frame_size[1] / sh
        self._lines = {
            name: tuple((x * fx, y * fy) for x, y in line)  # type: ignore[misc]
            for name, line in (("a", self.lines.line_a), ("b", self.lines.line_b))
        }
        self._size = frame_size

    def update(
        self, tracks: list[Detection], ts: float, frame_size: tuple[int, int] | None = None
    ) -> list[FlowEvent]:
        """Feed one frame's tracks (`frame_size` = (w, h) of that frame); returns new events."""
        if frame_size is not None:
            self._scale(tuple(frame_size))
        events: list[FlowEvent] = []
        for t in tracks:
            if t.track_id is None:
                continue
            st = self.state.setdefault(t.track_id, TrackState(first_ts=ts, last_ts=ts))
            st.frames += 1
            st.last_ts = ts
            p = anchor(t.box)
            for name, line in self._lines.items():
                side = _sign(_cross(line, p))
                if side == 0:
                    continue  # on the line: keep the last side
                last, prev = st.last_side[name], st.last_point[name]
                if last is not None and side != last and _within_segment(line, prev, p):
                    st.crossings.append(name)
                st.last_side[name], st.last_point[name] = side, p
            seq = "".join(st.crossings[-2:])
            if not st.counted and st.frames >= self.min_track_frames and seq in ("ab", "ba"):
                inward = (seq == "ab") == (self.lines.in_direction == "a_to_b")
                events.append(FlowEvent("in" if inward else "out", t.track_id, ts, t.conf, t.cls))
                st.counted = True
        for tid in [tid for tid, st in self.state.items() if ts - st.last_ts > TRACK_TTL_S]:
            del self.state[tid]
        return events
