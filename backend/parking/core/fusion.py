"""State store and fusion (vision.md §8): worker readings -> smoothed zone counts -> `LotStatus`.

`StateStore` holds the smoothers, the flow counters, the latest camera health and a trend
buffer per zone. Every mutating call returns the `Change`s it caused, for the DB (`zone_state`
rows when a count changed, `slot_state` rows when a slot flipped) and the SSE broadcaster:

- `apply_observation(obs)`: smooths the slots/counts, recomputes that camera's zones.
- `apply_flow_event(msg)`: one line crossing -> the flow zone's `FlowCounter`.
- `apply_health(msg)`: camera state -> confidence (`degraded`) and staleness (`down`).
- `tick()`: once a second; staleness (`stale_after_s` without data) and trend flips.
- `restore(...)`: seeds the state from the DB at start-up; restored zones stay stale until
  fresh data arrives (data-model.md §2).

Freshness uses the store's clock at receipt (not the worker's `ts`), so worker clock skew
can't make a zone look fresh or stale.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from parking.config import LotConfig, SlotFile
from parking.core.clock import Clock
from parking.core.flow_counter import FlowCounter
from parking.core.smoothing import CountSmoother, SlotSmoother
from parking.messages import (
    CameraHealthMsg,
    CameraState,
    FlowEventMsg,
    Level,
    LotStatus,
    Observation,
    Totals,
    Trend,
    ZoneStatus,
)

log = logging.getLogger(__name__)

TREND_BUFFER = timedelta(minutes=20)
# (confidence when the camera is ok, when it's degraded) per zone method (vision.md §8)
CAMERA_CONFIDENCE = {"slots": (1.0, 0.6), "count": (0.9, 0.5)}

ChangeSource = Literal["observation", "flow", "health", "tick", "startup"]


class UnknownCameraError(ValueError):
    """A payload names a camera that isn't in lot.yaml (or has the wrong role)."""


@dataclass(frozen=True)
class SlotChange:
    """A slot's smoothed state flipped (or was seen for the first time)."""

    ts: datetime
    camera_id: str
    slot_id: str
    taken: bool


@dataclass(frozen=True)
class ZoneChange:
    """Something a client sees about a zone changed. `count_changed` = occupied/free moved."""

    ts: datetime
    zone_id: str
    occupied: int
    free: int
    level: Level
    confidence: float
    stale: bool
    trend: Trend
    count_changed: bool
    source: ChangeSource


Change = SlotChange | ZoneChange


def level_for(free: int, capacity: int, plenty: float = 0.20, filling: float = 0.05) -> Level:
    """api.md Levels on `free / capacity`: ≥ plenty, ≥ filling, > 0 → almost_full, else full."""
    if free <= 0 or capacity <= 0:
        return "full"
    ratio = free / capacity
    if ratio >= plenty:
        return "plenty"
    if ratio >= filling:
        return "filling"
    return "almost_full"


def trend_for(delta_free: int, capacity: int) -> Trend:
    """Δfree over the trend window ≤ −max(2, 5% of capacity) → filling; ≥ +that → emptying."""
    threshold = max(2.0, 0.05 * capacity)
    if delta_free <= -threshold:
        return "filling"
    if delta_free >= threshold:
        return "emptying"
    return "steady"


class TrendBuffer:
    """(ts, free) samples of one zone, appended when `free` changes, kept for 20 minutes.

    `free` is a step function: its value at time t is the last sample at or before t. One
    sample older than the horizon is kept so the value at the window start is always known.
    """

    def __init__(self, keep: timedelta = TREND_BUFFER):
        self.keep = keep
        self.samples: deque[tuple[datetime, int]] = deque()

    def add(self, ts: datetime, free: int) -> None:
        if self.samples and self.samples[-1][1] == free:
            return
        self.samples.append((ts, free))
        self._prune(ts)

    def _prune(self, now: datetime) -> None:
        horizon = now - self.keep
        while len(self.samples) > 1 and self.samples[1][0] <= horizon:
            self.samples.popleft()

    def delta(self, now: datetime, window: timedelta) -> int:
        """free(now) − free(now − window); history shorter than the window uses the oldest."""
        if not self.samples:
            return 0
        self._prune(now)
        start = now - window
        then = self.samples[0][1]
        for ts, free in self.samples:
            if ts > start:
                break
            then = free
        return self.samples[-1][1] - then


@dataclass
class _ZoneView:
    occupied: int
    free: int
    level: Level
    confidence: float
    stale: bool
    trend: Trend

    def key(self) -> tuple:
        return (self.occupied, self.free, self.level, self.confidence, self.stale, self.trend)


class StateStore:
    """Current lot state, built from worker payloads (one per API process)."""

    def __init__(
        self,
        config: LotConfig,
        clock: Clock,
        slot_files: Mapping[str, SlotFile] | None = None,
    ):
        self.config = config
        self.clock = clock
        self.stale_after = timedelta(seconds=config.api.stale_after_s)
        self.trend_window = timedelta(minutes=config.api.trend_window_min)
        slot_files = {
            cam_id: f
            for cam_id, f in (slot_files or {}).items()
            if any(c.id == cam_id and c.role == "occupancy" for c in config.cameras)
        }
        # camera id -> slot id -> zone id (only slots of zones the camera reports on)
        self._slot_zone: dict[str, dict[str, str]] = {
            cam_id: {s.id: s.zone for s in f.slots if s.zone in config.camera(cam_id).zones}
            for cam_id, f in slot_files.items()
        }
        self.capacity = {
            z.id: config.zone_capacity(z.id, list(slot_files.values())) for z in config.zones
        }
        occupancy_cams = [c for c in config.cameras if c.role == "occupancy"]
        k = {c.id: c.smoothing.consistent_readings for c in occupancy_cams}
        self._slots = {c.id: SlotSmoother(k[c.id]) for c in occupancy_cams}
        self._counts = {c.id: CountSmoother(k[c.id]) for c in occupancy_cams}
        self.flow: dict[str, FlowCounter] = {
            z.id: FlowCounter(z.id, self.capacity[z.id], clock)
            for z in config.zones
            if z.method == "flow"
        }
        self.health: dict[str, CameraHealthMsg] = {}
        # camera id -> clock time of its last fresh data (observation; health for flow cameras)
        self._last_data: dict[str, datetime] = {}
        # last non-`down` state per camera; `down` keeps the confidence and sets stale
        self._quality: dict[str, CameraState] = {}
        self._updated_at: dict[str, datetime | None] = {z.id: None for z in config.zones}
        self._trend = {z.id: TrendBuffer() for z in config.zones}
        self.updated_at = clock.now()
        self._views: dict[str, _ZoneView] = self._compute_views()

    # --- inputs ---

    def apply_observation(self, obs: Observation) -> list[Change]:
        camera = self._camera(obs.camera_id, "occupancy")
        now = self.clock.now()
        changes: list[Change] = []
        slot_zone = self._slot_zone.get(camera.id, {})
        smoother = self._slots[camera.id]
        unknown = []
        for reading in obs.slots:
            if reading.id not in slot_zone:
                unknown.append(reading.id)
                continue
            before = smoother.state(reading.id)
            after = smoother.update(reading.id, reading.taken)
            if after != before:
                changes.append(SlotChange(now, camera.id, reading.id, after))
        if unknown:
            log.warning("camera %s: ignoring unknown slot(s) %s", camera.id, ", ".join(unknown))
        for zone_id, count in obs.zone_counts.items():
            if zone_id in camera.zones and self.config.zone(zone_id).method == "count":
                self._counts[camera.id].update(zone_id, count)
        self._last_data[camera.id] = now
        for zone_id in camera.zones:
            if self.config.zone(zone_id).method in ("slots", "count"):
                self._updated_at[zone_id] = now
        return changes + self._diff("observation")

    def flow_zone(self, camera_id: str) -> str:
        """The `flow` zone a flow camera counts for (lot.yaml allows exactly one per zone)."""
        camera = self._camera(camera_id, "flow")
        zone_id = next((z for z in camera.zones if z in self.flow), None)
        if zone_id is None:
            raise UnknownCameraError(f"flow camera '{camera_id}' has no 'flow' zone")
        return zone_id

    def apply_flow_event(self, msg: FlowEventMsg) -> tuple[bool | None, list[Change]]:
        """Apply one crossing: (None = duplicate, False = clamped, True = applied; changes)."""
        zone_id = self.flow_zone(msg.camera_id)
        result = self.flow[zone_id].apply(msg.event_id, msg.direction)
        if result is None:
            return None, []
        now = self.clock.now()
        self._last_data[msg.camera_id] = now
        self._updated_at[zone_id] = now
        return result, self._diff("flow")

    def apply_health(self, msg: CameraHealthMsg) -> list[Change]:
        camera = self._camera(msg.camera_id)
        self.health[camera.id] = msg
        if msg.state != "down":
            self._quality[camera.id] = msg.state
            if camera.role == "flow":
                now = self.clock.now()
                self._last_data[camera.id] = now
                for zone_id in camera.zones:
                    if self.config.zone(zone_id).method == "flow":
                        self._updated_at[zone_id] = self._updated_at[zone_id] or now
        return self._diff("health")

    def tick(self) -> list[Change]:
        """Call every second: zones going stale (or trends shifting) produce changes."""
        return self._diff("tick")

    def restore(
        self,
        slot_states: Mapping[str, Mapping[str, bool]] | None = None,
        zone_counts: Mapping[str, int] | None = None,
        updated_at: Mapping[str, datetime] | None = None,
        trend: Mapping[str, Iterable[tuple[datetime, int]]] | None = None,
    ) -> list[Change]:
        """Seed from the DB: slot states per camera, counts of `count`/`flow` zones, each zone's
        last update time and its recent (ts, free) samples. Zones stay stale until fresh data."""
        for cam_id, states in (slot_states or {}).items():
            if cam_id in self._slots:
                known = self._slot_zone.get(cam_id, {})
                self._slots[cam_id].restore({s: t for s, t in states.items() if s in known})
        for zone_id, count in (zone_counts or {}).items():
            zone = self.config.zone(zone_id)
            if zone.method == "flow":
                self.flow[zone_id].restore(count)
            elif zone.method == "count":
                # one camera's smoother carries the whole restored count
                cam = next(
                    c for c in self.config.cameras if zone_id in c.zones and c.role == "occupancy"
                )
                self._counts[cam.id].restore({zone_id: count})
        for zone_id, ts in (updated_at or {}).items():
            self._updated_at[zone_id] = ts
        for zone_id, samples in (trend or {}).items():
            for ts, free in sorted(samples):
                self._trend[zone_id].add(ts, free)
        return self._diff("startup")

    # --- output ---

    @property
    def has_data(self) -> bool:
        """False until the first observation (or restored state): `/api/status` answers 503."""
        return any(ts is not None for ts in self._updated_at.values())

    def status(self, lang: str = "en") -> LotStatus:
        zones = []
        for zone in self.config.zones:
            view = self._views[zone.id]
            slots = None
            if zone.method == "slots":
                slots = {
                    slot_id: taken
                    for cam_id, slot_zone in self._slot_zone.items()
                    for slot_id, taken in self._slots[cam_id].states.items()
                    if slot_zone.get(slot_id) == zone.id
                }
            zones.append(
                ZoneStatus(
                    id=zone.id,
                    name=zone.name.get(lang)
                    or zone.name.get("en")
                    or next(iter(zone.name.values())),
                    method=zone.method,
                    capacity=self.capacity[zone.id],
                    occupied=view.occupied,
                    free=view.free,
                    level=view.level,
                    confidence=view.confidence,
                    stale=view.stale,
                    trend=view.trend,
                    updated_at=self._updated_at[zone.id],
                    slots=slots,
                )
            )
        capacity = sum(z.capacity for z in zones)
        occupied = sum(z.occupied for z in zones)
        free = sum(z.free for z in zones)
        levels = self.config.api.levels
        total = Totals(
            capacity=capacity,
            occupied=occupied,
            free=free,
            level=level_for(free, capacity, levels.plenty, levels.filling),
            confidence=min(z.confidence for z in zones),
            stale=any(z.stale for z in zones),
        )
        return LotStatus(
            lot=self.config.lot.id, updated_at=self.updated_at, total=total, zones=zones
        )

    # --- internals ---

    def _camera(self, camera_id: str, role: str | None = None):
        camera = next((c for c in self.config.cameras if c.id == camera_id), None)
        if camera is None or (role is not None and camera.role != role):
            kind = f"{role} camera" if role else "camera"
            raise UnknownCameraError(f"unknown {kind} '{camera_id}'")
        return camera

    def _zone_cameras(self, zone_id: str):
        method = self.config.zone(zone_id).method
        role = "flow" if method == "flow" else "occupancy"
        return [c for c in self.config.cameras if zone_id in c.zones and c.role == role]

    def _occupied(self, zone_id: str) -> int:
        method = self.config.zone(zone_id).method
        if method == "flow":
            return self.flow[zone_id].occupied
        if method == "count":
            values = (self._counts[c.id].value(zone_id) for c in self._zone_cameras(zone_id))
            return sum(v for v in values if v is not None)
        return sum(
            1
            for c in self._zone_cameras(zone_id)
            for slot_id, taken in self._slots[c.id].states.items()
            if taken and self._slot_zone.get(c.id, {}).get(slot_id) == zone_id
        )

    def _stale(self, zone_id: str, now: datetime) -> bool:
        if self._updated_at[zone_id] is None:
            return True
        for camera in self._zone_cameras(zone_id):
            health = self.health.get(camera.id)
            if health is not None and health.state == "down":
                return True
            seen = self._last_data.get(camera.id)
            if seen is None or now - seen > self.stale_after:
                return True
        return False

    def _confidence(self, zone_id: str) -> float:
        method = self.config.zone(zone_id).method
        if method == "flow":
            return round(self.flow[zone_id].confidence(), 2)
        ok, degraded = CAMERA_CONFIDENCE[method]
        states = [self._quality.get(c.id, "ok") for c in self._zone_cameras(zone_id)]
        return degraded if "degraded" in states else ok

    def _compute_views(self) -> dict[str, _ZoneView]:
        now = self.clock.now()
        levels = self.config.api.levels
        views = {}
        for zone in self.config.zones:
            capacity = self.capacity[zone.id]
            occupied = min(self._occupied(zone.id), capacity)
            free = capacity - occupied
            views[zone.id] = _ZoneView(
                occupied=occupied,
                free=free,
                level=level_for(free, capacity, levels.plenty, levels.filling),
                confidence=self._confidence(zone.id),
                stale=self._stale(zone.id, now),
                trend=trend_for(self._trend[zone.id].delta(now, self.trend_window), capacity),
            )
        return views

    def _diff(self, source: ChangeSource) -> list[Change]:
        """Recompute every zone; a `ZoneChange` for each one whose visible state changed."""
        now = self.clock.now()
        new = self._compute_views()
        changes: list[Change] = []
        for zone_id, view in new.items():
            if self._updated_at[zone_id] is not None:
                self._trend[zone_id].add(now, view.free)
                view.trend = trend_for(
                    self._trend[zone_id].delta(now, self.trend_window), self.capacity[zone_id]
                )
            old = self._views[zone_id]
            if old.key() == view.key():
                continue
            count_changed = (old.occupied, old.free) != (view.occupied, view.free)
            changes.append(
                ZoneChange(
                    ts=now,
                    zone_id=zone_id,
                    occupied=view.occupied,
                    free=view.free,
                    level=view.level,
                    confidence=view.confidence,
                    stale=view.stale,
                    trend=view.trend,
                    count_changed=count_changed,
                    source=source,
                )
            )
        self._views = new
        if changes:
            self.updated_at = now
        return changes
