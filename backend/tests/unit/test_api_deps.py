"""P2.9: `Accept-Language` parsing, language resolution and per-language SSE rendering."""

from datetime import UTC, datetime

import pytest

from parking.api.deps import accept_languages, resolve_lang
from parking.api.sse import Broadcaster
from parking.messages import LotStatus


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, []),
        ("", []),
        ("ro-RO,ro;q=0.9,en;q=0.8", ["ro", "ro", "en"]),
        ("en;q=0.2, de", ["de", "en"]),
        ("*, fr;q=0, it;q=bad, es", ["es"]),
        ("pt_BR ; q=0.5", ["pt"]),
    ],
)
def test_accept_languages(header, expected):
    assert accept_languages(header) == expected


def test_resolve_lang():
    available = {"en", "ro"}
    assert resolve_lang(None, None, available) == "en"
    assert resolve_lang("ro", "en", available) == "ro"
    assert resolve_lang("fr", "de, ro;q=0.5", available) == "ro"
    assert resolve_lang(None, "de", available) == "en"
    assert resolve_lang("de", None, {"de"}) == "de"


def status(name):
    total = {
        "capacity": 2,
        "occupied": 0,
        "free": 2,
        "level": "plenty",
        "confidence": 1.0,
        "stale": False,
    }
    zone = {
        "id": "ground",
        "name": name,
        "method": "slots",
        "capacity": 2,
        "occupied": 0,
        "free": 2,
        "level": "plenty",
        "confidence": 1.0,
        "stale": False,
        "trend": "steady",
        "updated_at": None,
        "slots": {},
    }
    return LotStatus(
        lot="main", updated_at=datetime(2026, 10, 9, tzinfo=UTC), total=total, zones=[zone]
    )


def test_render_swaps_names_once_per_event_and_language():
    b = Broadcaster()
    b.publish(status("Ground"))
    event = b.latest
    assert b.render(event, "en", {"ground": "Ground"}) is event.data
    ro = b.render(event, "ro", {"ground": "Parter"})
    assert LotStatus.model_validate_json(ro).zones[0].name == "Parter"
    assert b.render(event, "ro", {"ground": "Parter"}) is ro  # cached
    b.publish(status("Ground"))
    assert b.render(b.latest, "ro", {"ground": "Parter"}) is not ro
