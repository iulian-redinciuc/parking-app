"""State store + fusion (vision.md §8, api.md Levels, testing.md §2) and the flow counter."""

from datetime import timedelta

import pytest

from parking.config import LotConfig, SlotFile
from parking.core.clock import FakeClock
from parking.core.flow_counter import FlowCounter
from parking.core.fusion import (
    NotCorrectableError,
    SlotChange,
    StateStore,
    TrendBuffer,
    UnknownCameraError,
    ZoneChange,
    level_for,
    trend_for,
)
from parking.messages import CameraHealthMsg, Observation, SlotScore

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]
GROUND_SLOTS = [f"G{i:02d}" for i in range(1, 11)]  # 10 slots


def make_config(**api) -> LotConfig:
    detector = {"model": "models/x"}
    return LotConfig.model_validate(
        {
            "version": 1,
            "lot": {
                "id": "main",
                "name": "Parking",
                "location": {"lat": 1, "lon": 2},
                "timezone": "UTC",
            },
            "zones": [
                {"id": "ground", "name": {"en": "Ground", "ro": "Parter"}, "method": "slots"},
                {"id": "roof", "name": {"en": "Roof"}, "method": "count", "capacity": 20},
                {
                    "id": "underground",
                    "name": {"en": "Underground"},
                    "method": "flow",
                    "capacity": 60,
                },
            ],
            "cameras": [
                {
                    "id": "cam-ground",
                    "role": "occupancy",
                    "zones": ["ground", "roof"],
                    "source": "file:x.jpg",
                    "slots_file": "s.json",
                    "detector": detector,
                },
                {
                    "id": "cam-ramp",
                    "role": "flow",
                    "zones": ["underground"],
                    "source": "file:y.jpg",
                    "lines_file": "l.json",
                    "detector": detector,
                },
            ],
            "api": api,
        }
    )


def make_store(clock=None, **api) -> tuple[StateStore, FakeClock]:
    clock = clock or FakeClock()
    slots = SlotFile.model_validate(
        {
            "version": 1,
            "camera_id": "cam-ground",
            "image_size": [100, 100],
            "slots": [{"id": s, "zone": "ground", "polygon": SQUARE} for s in GROUND_SLOTS],
        }
    )
    return StateStore(make_config(**api), clock, {"cam-ground": slots}), clock


def obs(clock, taken=(), roof=None, camera="cam-ground", slots=GROUND_SLOTS) -> Observation:
    return Observation(
        camera_id=camera,
        ts=clock.now(),
        frame_size=(100, 100),
        inference_ms=1,
        detections=0,
        slots=[SlotScore(id=s, score=0.9 if s in taken else 0.1, taken=s in taken) for s in slots],
        zone_counts={} if roof is None else {"roof": roof},
    )


def health(clock, state="ok", camera="cam-ground") -> CameraHealthMsg:
    return CameraHealthMsg(camera_id=camera, ts=clock.now(), state=state)


def zone(store, zone_id):
    return next(z for z in store.status().zones if z.id == zone_id)


def zone_changes(changes, zone_id=None):
    return [
        c
        for c in changes
        if isinstance(c, ZoneChange) and (zone_id is None or c.zone_id == zone_id)
    ]


# --- levels and trend rules ---


@pytest.mark.parametrize(
    ("free", "capacity", "level"),
    [
        (20, 100, "plenty"),  # exactly 0.20
        (19, 100, "filling"),
        (5, 100, "filling"),  # exactly 0.05
        (4, 100, "almost_full"),
        (1, 100, "almost_full"),
        (0, 100, "full"),
        (0, 0, "full"),
        (1, 10, "filling"),  # 0.10
        (10, 10, "plenty"),
    ],
)
def test_level_boundaries(free, capacity, level):
    assert level_for(free, capacity) == level


def test_level_uses_configured_thresholds():
    assert level_for(30, 100, plenty=0.5, filling=0.3) == "filling"
    assert level_for(29, 100, plenty=0.5, filling=0.3) == "almost_full"


@pytest.mark.parametrize(
    ("delta", "capacity", "trend"),
    [
        (-2, 10, "filling"),  # threshold is at least 2
        (-1, 10, "steady"),
        (2, 10, "emptying"),
        (-4, 100, "steady"),  # 5% of 100 = 5
        (-5, 100, "filling"),
        (5, 100, "emptying"),
        (0, 100, "steady"),
    ],
)
def test_trend_thresholds(delta, capacity, trend):
    assert trend_for(delta, capacity) == trend


def test_trend_buffer_value_at_window_start_and_pruning():
    clock = FakeClock()
    t0 = clock.now()
    buf = TrendBuffer()
    window = timedelta(minutes=15)
    assert buf.delta(t0, window) == 0
    buf.add(t0, 10)
    buf.add(t0 + timedelta(minutes=5), 7)
    buf.add(t0 + timedelta(minutes=6), 7)  # unchanged value: not stored
    assert len(buf.samples) == 2
    # history shorter than the window: compare with the oldest sample
    assert buf.delta(t0 + timedelta(minutes=10), window) == -3
    # window start at minute 6: free was 7 then
    assert buf.delta(t0 + timedelta(minutes=21), window) == 0
    buf.add(t0 + timedelta(minutes=40), 9)
    # minute-5 sample is the last one before the 20-min horizon, so it's kept; minute 0 goes
    assert [f for _, f in buf.samples] == [7, 9]


# --- store: slots zone ---


def test_no_data_before_first_observation():
    store, _ = make_store()
    assert not store.has_data
    status = store.status()
    assert all(z.stale and z.updated_at is None for z in status.zones)
    assert status.total.stale


def test_first_observation_counts_and_changes():
    store, clock = make_store()
    changes = store.apply_observation(obs(clock, taken={"G01", "G02", "G03"}))
    slot_changes = [c for c in changes if isinstance(c, SlotChange)]
    assert len(slot_changes) == 10  # first reading of every slot is recorded
    assert {c.slot_id for c in slot_changes if c.taken} == {"G01", "G02", "G03"}
    (g,) = zone_changes(changes, "ground")
    assert (g.occupied, g.free, g.stale, g.count_changed, g.source) == (
        3,
        7,
        False,
        True,
        "observation",
    )
    assert store.has_data
    z = zone(store, "ground")
    assert (z.capacity, z.occupied, z.free, z.level, z.confidence) == (10, 3, 7, "plenty", 1.0)
    assert z.updated_at == clock.now()
    assert z.slots["G01"] is True and z.slots["G04"] is False


def test_slot_flip_needs_k_readings():
    store, clock = make_store()
    store.apply_observation(obs(clock))
    for _ in range(2):
        clock.advance(5)
        assert store.apply_observation(obs(clock, taken={"G05"})) == []
    clock.advance(5)
    changes = store.apply_observation(obs(clock, taken={"G05"}))
    assert changes[0] == SlotChange(clock.now(), "cam-ground", "G05", True)
    (g,) = zone_changes(changes)
    assert (g.occupied, g.free) == (1, 9)


def test_unknown_slots_are_ignored():
    store, clock = make_store()
    changes = store.apply_observation(obs(clock, taken={"X99"}, slots=["G01", "X99"]))
    assert {c.slot_id for c in changes if isinstance(c, SlotChange)} == {"G01"}
    assert set(zone(store, "ground").slots) == {"G01"}


def test_unknown_camera_or_wrong_role_raises():
    store, clock = make_store()
    with pytest.raises(UnknownCameraError):
        store.apply_observation(obs(clock, camera="cam-nope"))
    with pytest.raises(UnknownCameraError):
        store.apply_observation(obs(clock, camera="cam-ramp"))
    with pytest.raises(UnknownCameraError):
        store.apply_health(health(clock, camera="cam-nope"))


def test_occupied_is_clamped_to_capacity():
    store, clock = make_store()
    store.apply_observation(obs(clock, roof=35))
    z = zone(store, "roof")
    assert (z.occupied, z.free, z.level) == (20, 0, "full")


# --- count zone ---


def test_count_zone_uses_median_and_count_confidence():
    store, clock = make_store()
    store.apply_observation(obs(clock, roof=10))
    assert zone(store, "roof").occupied == 10
    store.apply_observation(obs(clock, roof=18))  # median_low(10, 18) = 10
    store.apply_observation(obs(clock, roof=11))  # median(10, 18, 11) = 11
    z = zone(store, "roof")
    assert (z.occupied, z.free, z.confidence, z.slots) == (11, 9, 0.9, None)


# --- totals ---


def test_totals_sum_zones_and_take_lowest_confidence():
    store, clock = make_store()
    store.apply_health(health(clock, camera="cam-ramp"))
    store.apply_observation(obs(clock, taken={"G01", "G02"}, roof=5))
    status = store.status()
    assert (status.total.capacity, status.total.occupied, status.total.free) == (90, 7, 83)
    assert status.total.level == "plenty"
    assert status.total.confidence == 0.9  # roof (count, ok)
    assert status.total.stale is False
    assert status.lot == "main"
    store.apply_health(health(clock, state="degraded"))
    assert store.status().total.confidence == 0.5


def test_zone_names_follow_language():
    store, _ = make_store()
    assert zone(store, "ground").name == "Ground"
    names = {z.id: z.name for z in store.status(lang="ro").zones}
    assert names == {"ground": "Parter", "roof": "Roof", "underground": "Underground"}


# --- staleness ---


def test_stale_after_timeout_and_back():
    store, clock = make_store(stale_after_s=60)
    store.apply_health(health(clock, camera="cam-ramp"))
    store.apply_observation(obs(clock))
    clock.advance(60)
    assert store.tick() == []
    clock.advance(1)
    changes = zone_changes(store.tick())
    assert {c.zone_id for c in changes} == {"ground", "roof", "underground"}
    assert all(c.stale and not c.count_changed and c.source == "tick" for c in changes)
    assert store.tick() == []  # only the flip is reported
    assert store.status().total.stale
    changes = zone_changes(store.apply_observation(obs(clock)))
    assert {c.zone_id for c in changes} == {"ground", "roof"}
    assert not any(c.stale for c in changes)
    assert zone(store, "underground").stale  # flow camera still silent


def test_camera_down_is_stale_with_confidence_kept():
    store, clock = make_store()
    store.apply_observation(obs(clock))
    store.apply_health(health(clock, state="degraded"))
    changes = zone_changes(store.apply_health(health(clock, state="down")), "ground")
    assert changes[0].stale and changes[0].confidence == 0.6
    z = zone(store, "ground")
    assert z.stale and z.confidence == 0.6
    store.apply_health(health(clock, state="ok"))
    z = zone(store, "ground")
    assert not z.stale and z.confidence == 1.0


def test_degraded_lowers_confidence():
    store, clock = make_store()
    store.apply_observation(obs(clock, roof=3))
    changes = zone_changes(store.apply_health(health(clock, state="degraded")))
    assert {c.zone_id: c.confidence for c in changes} == {"ground": 0.6, "roof": 0.5}
    assert all(c.source == "health" and not c.count_changed for c in changes)
    assert not zone(store, "ground").stale


# --- trend ---


def test_trend_filling_then_steady_after_window():
    store, clock = make_store()  # trend window 15 min, capacity 10 → threshold 2
    store.apply_observation(obs(clock))
    for taken in ({"G01"}, {"G01", "G02"}):
        clock.advance(minutes=2)
        for _ in range(3):
            store.apply_observation(obs(clock, taken=taken))
    assert zone(store, "ground").trend == "filling"
    # keep the zone fresh with unchanged readings until the drop leaves the window
    for _ in range(16):
        clock.advance(minutes=1)
        store.apply_observation(obs(clock, taken={"G01", "G02"}))
    assert zone(store, "ground").trend == "steady"


def test_trend_emptying_and_tick_reports_trend_flip():
    store, clock = make_store(trend_window_min=5, stale_after_s=3600)
    store.apply_observation(obs(clock, taken={"G01", "G02", "G03"}))
    clock.advance(60)
    for _ in range(3):
        changes = store.apply_observation(obs(clock, taken={"G01"}))
    assert zone_changes(changes, "ground")[0].trend == "emptying"
    clock.advance(minutes=5)
    (change,) = zone_changes(store.tick(), "ground")
    assert change.trend == "steady" and not change.count_changed


# --- flow zone ---


def test_flow_zone_fresh_from_health_heartbeats():
    store, clock = make_store()
    assert zone(store, "underground").stale
    changes = zone_changes(store.apply_health(health(clock, camera="cam-ramp")), "underground")
    assert not changes[0].stale
    z = zone(store, "underground")
    assert (z.occupied, z.free, z.confidence, z.updated_at) == (0, 60, 1.0, clock.now())
    clock.advance(61)
    assert zone(store, "underground").stale is False  # views refresh on tick
    store.tick()
    assert zone(store, "underground").stale


def test_flow_confidence_decays():
    store, clock = make_store()
    store.apply_health(health(clock, camera="cam-ramp"))
    counter = store.flow["underground"]
    for i in range(10):
        counter.apply(f"e{i}", "in")
    clock.advance(hours=5)
    store.apply_health(health(clock, camera="cam-ramp"))
    z = zone(store, "underground")
    assert z.occupied == 10
    assert z.confidence == pytest.approx(1 - 0.10 - 0.10)


def test_correct_flow_zone_resets_confidence_and_clamps():
    store, clock = make_store()
    counter = store.flow["underground"]
    for i in range(10):
        counter.apply(f"e{i}", "in")
    clock.advance(hours=5)
    old, new, changes = store.correct("underground", 37)
    assert (old, new) == (10, 37)
    (change,) = zone_changes(changes, "underground")
    assert (change.source, change.occupied, change.count_changed) == ("correction", 37, True)
    z = zone(store, "underground")
    assert (z.occupied, z.confidence, z.updated_at) == (37, 1.0, clock.now())
    capacity = store.capacity["underground"]
    assert store.correct("underground", capacity + 5)[1] == capacity


def test_correct_only_flow_zones():
    store, _ = make_store()
    with pytest.raises(NotCorrectableError):
        store.correct("ground", 3)
    with pytest.raises(KeyError):
        store.correct("nowhere", 3)


# --- restore ---


def test_restore_is_stale_until_fresh_data():
    store, clock = make_store()
    t0 = clock.now()
    clock.advance(30)
    changes = store.restore(
        slot_states={"cam-ground": {"G01": True, "G02": True, "X99": True}},
        zone_counts={"roof": 4, "underground": 33},
        updated_at={"ground": t0, "roof": t0, "underground": t0},
        trend={"ground": [(t0 - timedelta(minutes=10), 10)]},
    )
    assert {c.zone_id for c in zone_changes(changes)} == {"ground", "roof", "underground"}
    assert all(c.source == "startup" and c.stale for c in zone_changes(changes))
    assert store.has_data
    status = store.status()
    assert {z.id: z.occupied for z in status.zones} == {"ground": 2, "roof": 4, "underground": 33}
    assert all(z.stale and z.updated_at == t0 for z in status.zones)
    assert zone(store, "ground").trend == "filling"  # 10 free 10 min ago, 8 now
    # a restored slot needs k contrary readings; the zone turns fresh at once
    changes = store.apply_observation(obs(clock, taken={"G01", "G02"}))
    assert not zone(store, "ground").stale
    assert not any(isinstance(c, SlotChange) and c.slot_id in ("G01", "G02") for c in changes)


# --- flow counter skeleton ---


def test_flow_counter_clamp_duplicates_correct_restore():
    clock = FakeClock()
    counter = FlowCounter("underground", 2, clock)
    assert counter.apply("a", "out") is False  # clamped at 0
    assert counter.apply("a", "in") is None  # duplicate id
    assert counter.apply("b", "in") is True
    assert counter.apply("c", "in") is True
    assert counter.apply("d", "in") is False  # clamped at capacity
    assert counter.occupied == 2
    assert counter.events_since_correction == 4
    clock.advance(hours=1)
    assert counter.confidence() == pytest.approx(1 - 0.04 - 0.02)
    assert counter.correct(5) == 2
    assert counter.events_since_correction == 0
    assert counter.confidence() == 1.0
    clock.advance(hours=100)
    assert counter.confidence() == 0.3
    counter.restore(1, corrected_at=clock.now(), events_since_correction=3)
    assert counter.occupied == 1
    assert counter.confidence() == pytest.approx(0.97)


# --- special spaces (P9.2) ---

SPECIAL = {"G01": "accessible", "G02": "accessible", "G03": "ev"}


def typed_slot_file(types=SPECIAL, ids=GROUND_SLOTS) -> SlotFile:
    return SlotFile.model_validate(
        {
            "version": 1,
            "camera_id": "cam-ground",
            "image_size": [100, 100],
            "slots": [
                {"id": s, "zone": "ground", "polygon": SQUARE, "type": types.get(s, "standard")}
                for s in ids
            ],
        }
    )


def by_type(store, zone_id="ground"):
    counts = zone(store, zone_id).by_type
    return counts and {t: (c.capacity, c.free) for t, c in counts.items()}


def test_by_type_counts_the_special_spaces_of_a_slots_zone():
    clock = FakeClock()
    store = StateStore(make_config(), clock, {"cam-ground": typed_slot_file()})
    # before any reading a space counts as free, like in the zone's own count
    assert by_type(store) == {"accessible": (2, 2), "ev": (1, 1)}
    store.apply_observation(obs(clock, taken={"G01", "G03", "G07"}))
    assert by_type(store) == {"accessible": (2, 1), "ev": (1, 0)}
    assert zone(store, "ground").free == 7  # special spaces are part of the zone's count
    # only `slots` zones have it
    assert by_type(store, "roof") is None and by_type(store, "underground") is None


def test_by_type_is_empty_without_special_spaces():
    store, clock = make_store()
    store.apply_observation(obs(clock, taken={"G01"}))
    assert by_type(store) == {}


def test_by_type_follows_the_smoothed_state():
    clock = FakeClock()
    store = StateStore(make_config(), clock, {"cam-ground": typed_slot_file()})
    store.apply_observation(obs(clock))
    k = store.config.cameras[0].smoothing.consistent_readings
    for _ in range(k - 1):
        store.apply_observation(obs(clock, taken={"G03"}))
        assert by_type(store)["ev"] == (1, 1)
    store.apply_observation(obs(clock, taken={"G03"}))
    assert by_type(store)["ev"] == (1, 0)


def test_a_swap_between_types_is_a_change_even_when_the_count_stays():
    clock = FakeClock()
    store = StateStore(make_config(), clock, {"cam-ground": typed_slot_file()})
    k = store.config.cameras[0].smoothing.consistent_readings
    for _ in range(k):
        store.apply_observation(obs(clock, taken={"G01"}))
    assert by_type(store)["accessible"] == (2, 1)
    before = store.updated_at
    clock.advance(seconds=5)
    # the accessible space frees up while a standard one is taken: still 9 free
    changes = []
    for _ in range(k):
        changes += store.apply_observation(obs(clock, taken={"G07"}))
    [change] = zone_changes(changes, "ground")
    assert (change.free, change.count_changed) == (9, False)
    assert store.updated_at > before
    assert by_type(store)["accessible"] == (2, 2)


def test_retyping_a_slot_in_the_editor_is_published():
    clock = FakeClock()
    store = StateStore(make_config(), clock, {"cam-ground": typed_slot_file()})
    store.apply_observation(obs(clock))
    changes = store.replace_slot_file("cam-ground", typed_slot_file({"G05": "reserved"}))
    assert [c.source for c in zone_changes(changes, "ground")] == ["config"]
    assert by_type(store) == {"reserved": (1, 1)}
    assert store.replace_slot_file("cam-ground", typed_slot_file({"G05": "reserved"})) == []
