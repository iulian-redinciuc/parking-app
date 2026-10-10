"""A simulated camera feed made from one photo (docs/design/vision.md §10, Simulated feed).

Cars are cut from the taken slots of a base image and pavement from the free ones, then
pasted into other slots so that many different frames (cars arriving and leaving) come out
of the one photo. Each frame's true occupancy is known, so the frames can be replayed as
the dev camera and scored with `parking evaluate`.

It moves the same few cars around: it tests the pipeline and the scorer's margins, it is
not evidence of real-world accuracy.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from parking.config import AppearanceCfg, SlotFile
from parking.vision.appearance import _corners, non_pavement, pavement_model, slot_crop, to_lab

MARGIN = 0.15  # sprite margin per side, as a fraction of the slot's width / height
EDGE_FRAC = 0.005  # a corner this close to the image border (fraction of the size) is cut off
ASPECT_TOL = 1.3  # donors whose width/height is within this factor count as the same shape
MIN_CAR_FRAC = 0.15  # a car blob smaller than this share of the slot isn't trusted as a mask
EVENTS_P = (0.45, 0.40, 0.15)  # chance of 0, 1 or 2 cars arriving or leaving per frame
PULL = 3.0  # how hard the walk is pulled towards the target occupancy
DAY_LOW, DAY_HIGH = 0.06, 0.94  # the day curve's nearly empty and nearly full shares

SIDES = ("left", "top", "right", "bottom")


@dataclass(frozen=True)
class Look:
    """How the car in a slot was made: whose sprite, mirrored how, shifted how."""

    donor: str
    flip_x: bool = False
    flip_y: bool = False
    gain: float = 1.0  # brightness
    hue: int = 0  # OpenCV hue units (2 degrees each)


@dataclass(frozen=True)
class _Patch:
    """A slot as an upright patch: the slot plus a margin on every side."""

    corners: np.ndarray  # 4x2 image points: top-left first, then clockwise
    size: tuple[int, int]  # patch width, height (margin included)
    to_image: np.ndarray  # 3x3 homography, patch pixels -> image pixels
    cut: frozenset[str]  # patch sides lying on the image border
    margin: tuple[int, int]  # px added left/right and top/bottom

    @property
    def aspect(self) -> float:
        return self.size[0] / self.size[1]


def _ordered(points: Sequence[Sequence[float]]) -> np.ndarray:
    """4 corners, clockwise on screen, starting at the one nearest the top-left."""
    c = _corners(points).astype(np.float64)
    x, y = c[:, 0], c[:, 1]
    if np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y) < 0:  # anticlockwise with y down
        c = c[::-1]
    return np.roll(c, -int(np.argmin(c.sum(axis=1))), axis=0)


def _patch(points: Sequence[Sequence[float]], image_size: tuple[int, int], margin: float):
    c = _ordered(points)
    w = (np.linalg.norm(c[1] - c[0]) + np.linalg.norm(c[2] - c[3])) / 2
    h = (np.linalg.norm(c[2] - c[1]) + np.linalg.norm(c[3] - c[0])) / 2
    w, h = max(int(round(w)), 2), max(int(round(h)), 2)
    mx, my = int(round(w * margin)), int(round(h * margin))
    inner = np.array(
        [[mx, my], [mx + w - 1, my], [mx + w - 1, my + h - 1], [mx, my + h - 1]], np.float32
    )
    to_image = cv2.getPerspectiveTransform(inner, c.astype(np.float32))

    iw, ih = image_size
    ex, ey = max(iw * EDGE_FRAC, 2), max(ih * EDGE_FRAC, 2)
    on_border = [(x <= ex, y <= ey, x >= iw - 1 - ex, y >= ih - 1 - ey) for x, y in c]
    cut = set()
    for side, (a, b) in zip(SIDES, ((0, 3), (0, 1), (1, 2), (2, 3)), strict=True):
        if any(p and q for p, q in zip(on_border[a], on_border[b], strict=True)):
            cut.add(side)
    return _Patch(c, (w + 2 * mx, h + 2 * my), to_image, frozenset(cut), (mx, my))


def _feather(mask: np.ndarray, grow: int, blur: int) -> np.ndarray:
    """A 0..1 float alpha: the mask grown by `grow` px with a soft edge of about `blur` px."""
    m = mask.astype(np.uint8)
    if grow > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1))
        m = cv2.dilate(m, k)
    alpha = m.astype(np.float32)
    if blur > 0:
        alpha = cv2.GaussianBlur(alpha, (2 * blur + 1, 2 * blur + 1), 0)
    return alpha


class Simulator:
    """Renders the base image with any set of slots taken.

    `taken` are the slots holding a car in the base image; `unsure` slots are never touched
    and never used as donors. Slot polygons are rescaled from the slot file's `image_size`.
    """

    def __init__(
        self,
        base: np.ndarray,
        slot_file: SlotFile,
        taken: Iterable[str],
        unsure: Iterable[str] = (),
        margin: float = MARGIN,
        seed: int = 0,
    ) -> None:
        self.base = base
        h, w = base.shape[:2]
        slots = slot_file.scaled(w, h)
        self.slot_ids = [s.id for s in slots]
        self.unsure = frozenset(unsure)
        self.base_taken = frozenset(taken) - self.unsure
        unknown = sorted((set(taken) | self.unsure) - set(self.slot_ids))
        if unknown:
            raise ValueError(f"slot(s) not in the slot file: {', '.join(unknown)}")
        self.movable = [s for s in self.slot_ids if s not in self.unsure]
        self.patches = {s.id: _patch(s.polygon, (w, h), margin) for s in slots}

        free = [s for s in self.movable if s not in self.base_taken]
        if not self.base_taken or not free:
            raise ValueError("the base image needs at least one taken and one free slot")
        params = AppearanceCfg()
        polys = {s.id: s.polygon for s in slots}
        self._pavement = pavement_model(
            [to_lab(slot_crop(base, polys[s], params.inset)) for s in free]
        )
        self._params = params

        self._sprites = {s: self._cut(s) for s in self.movable}
        self._own = {s.id: self._own_mask(s.id, slots) for s in slots}
        # the car in each taken slot of the base: sprite alpha and the area to cover to remove it
        self._car_alpha: dict[str, np.ndarray] = {}
        self._clear_alpha: dict[str, np.ndarray] = {}
        for s in sorted(self.base_taken):
            self._car_alpha[s], self._clear_alpha[s] = self._car_masks(s)

        rng = np.random.default_rng(seed)
        self._car_donors = {s: self._donors(s, sorted(self.base_taken)) for s in self.movable}
        # the pavement that shows when a base car leaves: one donor per slot, so it never flickers
        self._pavement_donor = {
            s: str(rng.choice(self._donors(s, free))) for s in sorted(self.base_taken)
        }
        self._clear_sprite = {s: self._matched_pavement(s) for s in sorted(self.base_taken)}
        self._default = {s: self.new_look(s, rng) for s in self.movable}

    # --- sprites ---

    def _cut(self, slot: str) -> np.ndarray:
        p = self.patches[slot]
        return cv2.warpPerspective(
            self.base,
            p.to_image,
            p.size,
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REFLECT,
        )

    def _own_mask(self, slot: str, slots) -> np.ndarray:
        """1 where the patch may be changed, 0 inside the other slots' polygons (soft edge)."""
        p = self.patches[slot]
        to_patch = np.linalg.inv(p.to_image)
        others = np.zeros((p.size[1], p.size[0]), np.uint8)
        for other in slots:
            if other.id != slot:
                poly = np.array([other.polygon], np.float64)
                pts = cv2.perspectiveTransform(poly, to_patch)[0]
                cv2.fillPoly(others, [np.round(pts).astype(np.int32)], 1)
        return cv2.GaussianBlur(1.0 - others.astype(np.float32), (5, 5), 0)

    def _matched_pavement(self, slot: str) -> np.ndarray:
        """The pavement donor's sprite at `slot`'s patch size, shifted towards the colour of
        the pavement around the car it replaces (the edge of the covered area)."""
        p = self.patches[slot]
        donor = cv2.resize(self._sprites[self._pavement_donor[slot]], p.size)
        own = self._sprites[slot]
        colour, spread = self._pavement
        alpha = self._clear_alpha[slot]
        ring = (alpha > 0.02) & (alpha < 0.5)
        ring &= ~non_pavement(to_lab(own), colour, spread, self._params)
        if np.count_nonzero(ring) < 50:
            return donor
        # half way: that edge is partly the car's own shadow, which leaves with the car
        offset = 0.5 * (np.median(own[ring], axis=0) - np.median(donor[ring], axis=0))
        return np.clip(donor.astype(np.float32) + offset, 0, 255).astype(np.uint8)

    def _car_masks(self, slot: str) -> tuple[np.ndarray, np.ndarray]:
        """Alpha of the car in `slot` (to paste it elsewhere) and of the area that hides it.

        The car is the largest non-pavement blob touching the slot, as its convex hull (so
        windows reflecting the pavement colour belong to it). Cars poke out of their slot,
        which is why the patch has a margin and the mask isn't just the slot's rectangle.
        """
        sprite = self._sprites[slot]
        ph, pw = sprite.shape[:2]
        short = min(pw, ph)
        colour, spread = self._pavement
        mask = non_pavement(to_lab(sprite), colour, spread, self._params)
        mask = (mask & (self._own[slot] > 0.99)).astype(np.uint8)  # not the neighbours' cars
        k = max(int(short * 0.05), 3)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)  # drops the painted lines
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

        mx, my = self.patches[slot].margin
        inner = np.zeros((ph, pw), bool)
        inner[my : ph - my, mx : pw - mx] = True
        best, best_area = 0, 0
        for i in range(1, n):
            area = int(np.count_nonzero((labels == i) & inner))
            if area > best_area:
                best, best_area = i, area
        hull = np.zeros((ph, pw), np.uint8)
        if best_area >= MIN_CAR_FRAC * inner.sum():
            pts = cv2.findNonZero((labels == best).astype(np.uint8))
            cv2.fillConvexPoly(hull, cv2.convexHull(pts), 1)
        else:  # no clear car: fall back to the whole slot
            hull[inner] = 1
        car = _feather(hull, max(int(short * 0.03), 1), max(int(short * 0.02), 1))
        clear = _feather(hull, max(int(short * 0.10), 2), max(int(short * 0.06), 1))
        return car * self._own[slot], clear * self._own[slot]

    def _donors(self, slot: str, candidates: Sequence[str]) -> list[str]:
        """Candidates of a similar shape: cut off by the image edge the same way, and with a
        similar width/height (the closest one if none is within `ASPECT_TOL`)."""
        me = self.patches[slot]
        same_cut = [c for c in candidates if self.patches[c].cut == me.cut] or list(candidates)

        def off(c: str) -> float:
            return abs(math.log(self.patches[c].aspect / me.aspect))

        near = [c for c in same_cut if off(c) <= math.log(ASPECT_TOL)]
        return near or [min(same_cut, key=off)]

    # --- looks ---

    def base_state(self) -> dict[str, Look]:
        """The base image's own cars (rendering this returns the base image unchanged)."""
        return {s: Look(donor=s) for s in sorted(self.base_taken)}

    def new_look(self, slot: str, rng: np.random.Generator) -> Look:
        """A randomly chosen car for `slot`: a donor of similar shape, maybe mirrored, with a
        small brightness and colour shift. A cut-off slot is never mirrored across its cut."""
        cut = self.patches[slot].cut
        can_x = ("left" in cut) == ("right" in cut)
        can_y = ("top" in cut) == ("bottom" in cut)
        return Look(
            donor=str(rng.choice(self._car_donors[slot])),
            flip_x=bool(can_x and rng.random() < 0.5),
            flip_y=bool(can_y and rng.random() < 0.5),
            gain=round(float(rng.uniform(0.9, 1.1)), 3),
            hue=int(rng.integers(-8, 9)),
        )

    def _state(self, occupancy: Mapping[str, Look] | Iterable[str]) -> dict[str, Look]:
        if not isinstance(occupancy, Mapping):
            occupancy = dict.fromkeys(occupancy)
        bad = sorted(set(occupancy) - set(self.movable))
        if bad:
            raise ValueError(f"slot(s) that can't be changed: {', '.join(bad)}")
        return {
            s: look or (Look(donor=s) if s in self.base_taken else self._default[s])
            for s, look in occupancy.items()
        }

    # --- rendering ---

    def _paste(self, img: np.ndarray, slot: str, sprite: np.ndarray, alpha: np.ndarray) -> None:
        """Blend `sprite` (any patch size) into `slot`, in place; other slots stay as they are."""
        p = self.patches[slot]
        alpha = cv2.resize(alpha, p.size) * self._own[slot]
        sprite = cv2.resize(sprite, p.size)
        pw, ph = p.size
        sh, sw = sprite.shape[:2]
        scale = np.diag([pw / sw, ph / sh, 1.0])
        corners = cv2.perspectiveTransform(
            np.array([[[0, 0], [pw, 0], [pw, ph], [0, ph]]], np.float64), p.to_image
        )[0]
        h, w = img.shape[:2]
        x1, y1 = np.floor(corners.min(axis=0)).astype(int)
        x2, y2 = np.ceil(corners.max(axis=0)).astype(int) + 1
        x1, y1, x2, y2 = max(x1, 0), max(y1, 0), min(x2, w), min(y2, h)
        if x2 <= x1 or y2 <= y1:
            return
        m = np.array([[1, 0, -x1], [0, 1, -y1], [0, 0, 1.0]]) @ p.to_image @ scale
        size = (int(x2 - x1), int(y2 - y1))
        warped = cv2.warpPerspective(sprite, m, size, flags=cv2.INTER_LINEAR)
        a = cv2.warpPerspective(alpha, m, size, flags=cv2.INTER_LINEAR)[..., None]
        roi = img[y1:y2, x1:x2]
        roi[:] = np.clip(a * warped + (1 - a) * roi, 0, 255).astype(np.uint8)

    def _car_sprite(self, look: Look) -> tuple[np.ndarray, np.ndarray]:
        sprite, alpha = self._sprites[look.donor], self._car_alpha[look.donor]
        if look.flip_x:
            sprite, alpha = sprite[:, ::-1], alpha[:, ::-1]
        if look.flip_y:
            sprite, alpha = sprite[::-1], alpha[::-1]
        if look.gain != 1.0 or look.hue:
            hsv = cv2.cvtColor(np.ascontiguousarray(sprite), cv2.COLOR_BGR2HSV).astype(np.int32)
            hsv[..., 0] = (hsv[..., 0] + look.hue) % 180
            hsv[..., 2] = np.clip(hsv[..., 2] * look.gain, 0, 255)
            sprite = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        return np.ascontiguousarray(sprite), np.ascontiguousarray(alpha)

    def render(self, occupancy: Mapping[str, Look] | Iterable[str]) -> np.ndarray:
        """The base image with exactly the slots in `occupancy` taken.

        `occupancy` maps slot -> `Look`, or is just the taken slot ids (base cars stay where
        they are, other slots get a fixed default car). A slot whose wanted state is what
        the base shows isn't touched.
        """
        state = self._state(occupancy)
        img = self.base.copy()
        cars = []
        for slot in self.movable:
            look = state.get(slot)
            if slot in self.base_taken and look != Look(donor=slot):
                self._paste(img, slot, self._clear_sprite[slot], self._clear_alpha[slot])
            if look is not None and look != Look(donor=slot):
                cars.append((slot, look))
        for slot, look in cars:  # after all the pavement, whose wider edge could dim a neighbour
            self._paste(img, slot, *self._car_sprite(look))
        return img


# --- sequence ---


def day_curve(t: float) -> float:
    """Share of the lot taken at `t` (0..1 of the day): empty, fills up, busy, empties."""
    ramp = [(0.0, DAY_LOW), (0.1, DAY_LOW), (0.4, DAY_HIGH), (0.6, DAY_HIGH), (0.9, DAY_LOW)]
    return float(np.interp(t, [x for x, _ in ramp], [y for _, y in ramp], right=DAY_LOW))


def walk(
    slots: Sequence[str],
    frames: int,
    rng: np.random.Generator,
    start: Iterable[str],
    day: bool = False,
    min_dwell: int = 4,
) -> Iterator[list[tuple[str, bool]]]:
    """Per frame, the changes since the previous one as `(slot, arrived)`; frame 0 has none.

    A random walk: each frame at most two cars arrive or leave, pulled towards half full, or
    towards `day_curve` with `day`. A slot that changed stays as it is for `min_dwell` frames
    (the API's smoothing needs a few consistent readings before it believes a change).
    A generator, so that a longer run starts with the same frames as a shorter one.
    """
    taken = set(start)
    changed_at: dict[str, int] = {}
    n = len(slots)
    for i in range(frames):
        events: list[tuple[str, bool]] = []
        if i > 0:
            target = (day_curve(i / max(frames - 1, 1)) if day else 0.5) * n
            for _ in range(int(rng.choice(3, p=EVENTS_P))):
                p_arrive = min(max(0.5 + PULL * (target - len(taken)) / n, 0.03), 0.97)
                arrive = bool(rng.random() < p_arrive)
                pool = [
                    s
                    for s in slots
                    if (s in taken) != arrive and i - changed_at.get(s, -min_dwell) >= min_dwell
                ]
                if not pool:
                    continue
                slot = str(rng.choice(pool))
                (taken.add if arrive else taken.discard)(slot)
                changed_at[slot] = i
                events.append((slot, arrive))
        yield events


def sequence(
    sim: Simulator, frames: int, seed: int, day: bool = False, min_dwell: int = 4
) -> list[dict[str, Look]]:
    """`frames` states (slot -> `Look` of the car in it), the same for the same seed.

    Starts from the base image's cars (with `day`, from only as many of them as the curve's
    nearly empty start). A car keeps its look for as long as it stays.
    """
    rng = np.random.default_rng(seed)
    state = sim.base_state()
    if day:
        keep = max(round(DAY_LOW * len(sim.movable)), 0)
        for slot in list(rng.permutation(sorted(state)))[keep:]:
            del state[str(slot)]
    out = []
    for events in walk(sim.movable, frames, rng, state, day, min_dwell):
        for slot, arrived in events:
            if arrived:
                state[slot] = sim.new_look(slot, rng)
            else:
                del state[slot]
        out.append(dict(state))
    return out


# --- frame-wide variation ---


def vary(
    image: np.ndarray,
    index: int,
    seed: int,
    drift: float = 0.08,
    noise: float = 2.0,
    shift: int = 0,
) -> np.ndarray:
    """Frame `index` as the camera would see it: brightness and contrast drifting slowly
    (up to ±`drift` and ±`drift`/2), sensor noise (`noise` = sigma in grey levels) and an
    optional whole-frame shift of up to `shift` px. The same for the same `seed` and `index`."""
    phases = np.random.default_rng(seed).uniform(0, 2 * math.pi, 2)
    rng = np.random.default_rng([seed, index])
    gain = 1 + drift * math.sin(index / 37 + phases[0])
    contrast = 1 + drift / 2 * math.sin(index / 23 + phases[1])
    out = image.astype(np.float32)
    mean = float(out.mean())
    out = (out - mean) * contrast + mean * gain
    if noise > 0:
        out += rng.normal(0, noise, out.shape).astype(np.float32)
    out = np.clip(out, 0, 255).astype(np.uint8)
    if shift > 0:
        dx, dy = (int(v) for v in rng.integers(-shift, shift + 1, 2))
        m = np.array([[1, 0, dx], [0, 1, dy]], np.float32)
        out = cv2.warpAffine(out, m, (out.shape[1], out.shape[0]), borderMode=cv2.BORDER_REPLICATE)
    return out


def frame_name(index: int) -> str:
    """`frame-0001.jpg` for index 0."""
    return f"frame-{index + 1:04}.jpg"
