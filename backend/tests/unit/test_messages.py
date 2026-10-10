"""Message models (api.md §1, §5): JSON round trips, timestamps, forward compatibility."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from parking.messages import (
    CameraHealthMsg,
    FlowEventAck,
    FlowEventBatch,
    FlowEventMsg,
    LotStatus,
    Observation,
    SlotScore,
    Totals,
    ZoneStatus,
)

TS = datetime(2026, 10, 7, 17, 5, 12, 120000, tzinfo=UTC)

# The examples from api.md, as written there.
OBSERVATION = {
    "v": 1, "camera_id": "cam-ground", "ts": "2026-10-07T17:05:12.120Z",
    "frame_size": [2560, 1440], "inference_ms": 143, "detections": 31,
    "slots": [{"id": "G01", "score": 0.82, "taken": True},
              {"id": "G02", "score": 0.04, "taken": False}],
    "zone_counts": {},
}  # fmt: skip
FLOW = {
    "v": 1, "event_id": "6f1c2a1e-0000", "camera_id": "cam-ramp",
    "ts": "2026-10-07T17:06:01.480Z", "direction": "in", "track_id": 4412,
    "cls": "car", "confidence": 0.77,
}  # fmt: skip
HEALTH = {
    "v": 1, "camera_id": "cam-ground", "ts": "2026-10-07T17:05:12.000Z",
    "state": "ok", "issue": None, "fps": 0.2, "last_frame_age_s": 3.1,
    "inference_ms_avg": 151, "unhealthy_ratio": 0.0, "gate_active_ratio": None,
    "started_at": "2026-10-07T09:00:00.000Z", "disk_pct": 41.5, "cpu_temp_c": 58.4,
}  # fmt: skip
LOT_STATUS = {
    "v": 1, "lot": "main", "updated_at": "2026-10-07T17:05:12.000Z",
    "total": {"capacity": 100, "occupied": 77, "free": 23, "level": "plenty",
              "confidence": 0.86, "stale": False},
    "zones": [
        {"id": "ground", "name": "Ground", "method": "slots", "capacity": 40,
         "occupied": 28, "free": 12, "level": "filling", "confidence": 1.0, "stale": False,
         "trend": "filling", "updated_at": "2026-10-07T17:05:12.000Z",
         "slots": {"G01": True, "G02": False},
         "by_type": {"accessible": {"capacity": 2, "free": 1}}},
        {"id": "underground", "name": "Underground", "method": "flow", "capacity": 60,
         "occupied": 49, "free": 11, "level": "filling", "confidence": 0.86, "stale": False,
         "trend": "steady", "updated_at": "2026-10-07T17:04:58.000Z", "slots": None,
         "by_type": None},
    ],
}  # fmt: skip

CASES = [
    (Observation, OBSERVATION),
    (FlowEventMsg, FLOW),
    (FlowEventBatch, {"events": [FLOW]}),
    (FlowEventAck, {"accepted": 3, "duplicates": 1}),
    (CameraHealthMsg, HEALTH),
    (LotStatus, LOT_STATUS),
    (Totals, LOT_STATUS["total"]),
    (ZoneStatus, LOT_STATUS["zones"][0]),
    (SlotScore, OBSERVATION["slots"][0]),
]


@pytest.mark.parametrize(("model", "payload"), CASES, ids=[m.__name__ for m, _ in CASES])
def test_round_trip_through_json(model, payload):
    msg = model.model_validate_json(json.dumps(payload))
    dumped = msg.model_dump_json()
    assert model.model_validate_json(dumped) == msg
    # The spec examples come back unchanged (numbers compare equal, 143 == 143.0).
    assert json.loads(dumped) == payload


@pytest.mark.parametrize(("model", "payload"), CASES, ids=[m.__name__ for m, _ in CASES])
def test_unknown_fields_are_ignored(model, payload):
    msg = model.model_validate({**payload, "added_in_v2": {"x": 1}})
    assert "added_in_v2" not in msg.model_dump()


def test_timestamps_serialise_as_utc_z_with_milliseconds():
    plus2 = timezone(timedelta(hours=2))
    obs = Observation(
        camera_id="c", ts=datetime(2026, 10, 7, 19, 5, 12, 120456, tzinfo=plus2),
        frame_size=(10, 10), inference_ms=1, detections=0,
    )  # fmt: skip
    assert obs.ts == TS.replace(microsecond=120456)
    assert json.loads(obs.model_dump_json())["ts"] == "2026-10-07T17:05:12.120Z"


def test_naive_timestamp_is_taken_as_utc():
    msg = CameraHealthMsg(camera_id="c", ts=datetime(2026, 10, 7, 17, 5, 12), state="ok")
    assert msg.ts.tzinfo is UTC
    assert json.loads(msg.model_dump_json())["ts"] == "2026-10-07T17:05:12.000Z"


def test_pipeline_observation_is_accepted():
    """`AnalysisResult.to_observation()` (P1.7) adds `totals`/`timings`; they're ignored."""
    payload = {**OBSERVATION, "totals": {"ground": {"free": 1}}, "timings": {"score": 3.1}}
    obs = Observation.model_validate(payload)
    assert [s.id for s in obs.slots] == ["G01", "G02"]
    assert "totals" not in json.loads(obs.model_dump_json())


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (FlowEventMsg, {**FLOW, "direction": "sideways"}),
        (FlowEventMsg, {**FLOW, "confidence": 1.5}),
        (FlowEventMsg, {**FLOW, "event_id": ""}),
        (FlowEventBatch, {"events": []}),
        (FlowEventBatch, {"events": [FLOW] * 101}),
        (CameraHealthMsg, {**HEALTH, "state": "broken"}),
        (CameraHealthMsg, {**HEALTH, "issue": "foggy"}),
        (Observation, {**OBSERVATION, "v": 2}),
        (Observation, {**OBSERVATION, "frame_size": [1, 2, 3]}),
        (Totals, {**LOT_STATUS["total"], "level": "busy"}),
        (ZoneStatus, {**LOT_STATUS["zones"][0], "trend": "up"}),
    ],
)
def test_invalid_payloads_are_rejected(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_batch_of_100_is_allowed():
    assert len(FlowEventBatch.model_validate({"events": [FLOW] * 100}).events) == 100
