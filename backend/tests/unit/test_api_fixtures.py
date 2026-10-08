"""The JSON examples in `tests/fixtures/api/` are shared with the frontend
(`frontend/src/api/__fixtures__/`, P3.2): each must be exactly what the API would send."""

import json
from pathlib import Path

import pytest

from parking.messages import LotStatus

FIXTURES = Path(__file__).parents[1] / "fixtures" / "api"
ERROR_CODES = {
    "bad_request",
    "unauthorized",
    "forbidden",
    "not_found",
    "method_not_allowed",
    "conflict",
    "rate_limited",
    "unavailable",
    "internal",
}


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize("name", ["lot-status.json", "lot-status-stale.json"])
def test_lot_status_fixture_round_trips(name):
    data = load(name)
    status = LotStatus.model_validate(data)
    assert status.model_dump(mode="json") == data
    zones = status.zones
    assert status.total.capacity == sum(z.capacity for z in zones)
    assert status.total.free == sum(z.free for z in zones)
    assert status.total.confidence == min(z.confidence for z in zones)
    assert status.total.stale == any(z.stale for z in zones)
    for z in [status.total, *zones]:
        assert z.occupied + z.free == z.capacity


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("error-*.json")))
def test_error_fixtures_use_the_error_format(name):
    data = load(name)
    assert list(data) == ["error"]
    err = data["error"]
    assert err["code"] in ERROR_CODES and isinstance(err["message"], str)
    assert set(err) <= {"code", "message", "details"}
    for d in err.get("details", []):
        assert set(d) == {"type", "loc", "msg"}
