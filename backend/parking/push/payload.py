"""The Web Push payload (api.md §6) built from the current `LotStatus`."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from parking.messages import SPACE_TYPES, LotStatus, ZoneStatus

TAG = "parking-status"
# English, like the rest of the push text
SPACE_TYPE_NAMES = {
    "accessible": "Accessible",
    "ev": "EV charging",
    "motorcycle": "Motorcycle",
    "reserved": "Reserved",
}


def app_url(public_app_url: str | None) -> str:
    """`<PUBLIC_APP_URL>#/`; just `#/` (the service worker resolves it against its scope)
    when the app URL isn't set."""
    return (public_app_url or "").rstrip("/") + ("/#/" if public_app_url else "#/")


def status_payload(
    status: LotStatus | None,
    kind: str,
    url: str,
    tz: str = "UTC",
    zones: list[str] | None = None,
    space_types: list[str] | None = None,
) -> dict[str, Any]:
    """`Parking: 23 free` / `Ground 12 · Underground ≈11 · 17:05` (flow zones are estimates,
    hence `≈`; the time is the status time in the subscription's `tz`). `zones` limits the body
    to the user's preferred zones; the title is always the whole lot. `space_types` adds the
    free special spaces of those zones before the time (`Accessible 1 · EV charging 0`), for
    the types they have."""
    if status is None:
        return {
            "title": "Parking",
            "body": "No data yet",
            "tag": TAG,
            "url": url,
            "level": None,
            "kind": kind,
        }
    shown = [z for z in status.zones if not zones or z.id in zones] or status.zones
    parts = [f"{z.name} {'≈' if z.method == 'flow' else ''}{z.free}" for z in shown]
    special = type_free(shown)
    parts += [f"{SPACE_TYPE_NAMES[t]} {special[t]}" for t in space_types or () if t in special]
    parts.append(local_time(status.updated_at, tz))
    return {
        "title": f"Parking: {status.total.free} free",
        "body": " · ".join(parts),
        "tag": TAG,
        "url": url,
        "level": status.total.level,
        "kind": kind,
    }


def type_free(zones: list[ZoneStatus]) -> dict[str, int]:
    """Free special spaces per type over `zones`, only for the types they have."""
    free: dict[str, int] = {}
    for zone in zones:
        for kind, count in (zone.by_type or {}).items():
            free[kind] = free.get(kind, 0) + count.free
    return {t: free[t] for t in SPACE_TYPES if t in free}


def local_time(ts: datetime, tz: str) -> str:
    try:
        return ts.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    except (ValueError, KeyError):  # a tz that went away from the tz database
        return ts.strftime("%H:%M")
