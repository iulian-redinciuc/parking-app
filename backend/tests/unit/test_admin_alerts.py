"""P7.8: the admin alert rules (notifications.md §5.1) without the API."""

from datetime import UTC, datetime, timedelta

from parking.config import LotConfig
from parking.messages import CameraHealthMsg, LotStatus
from parking.push.admin_alerts import AlertTracker, Condition, Notice, alert_payload, conditions

T0 = datetime(2026, 10, 9, 14, 5, tzinfo=UTC)
DOWN = Condition("camera_down", "cam-ground", "connect_failed")
SHIFT = Condition("camera_shifted", "cam-ground")

CONFIG = LotConfig.model_validate(
    {
        "version": 1,
        "lot": {"id": "main", "name": "P", "location": {"lat": 1, "lon": 2}, "timezone": "UTC"},
        "zones": [
            {"id": "ground", "name": {"en": "Ground", "ro": "Parter"}, "method": "slots"},
            {"id": "underground", "name": {"en": "Underground"}, "method": "flow", "capacity": 9},
        ],
        "cameras": [
            {
                "id": "cam-ground",
                "role": "occupancy",
                "zones": ["ground"],
                "source": "file:x.jpg",
                "slots_file": "s.json",
                "detector": {"model": "m"},
            },
            {
                "id": "cam-ramp",
                "role": "flow",
                "zones": ["underground"],
                "source": "file:y.jpg",
                "lines_file": "l.json",
                "detector": {"model": "m"},
            },
        ],
    }
)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_grace_then_one_alert_per_hour_then_resolved():
    t = AlertTracker()
    assert t.update([DOWN], at(0)) == []
    assert t.update([DOWN], at(1.9)) == []
    assert t.update([DOWN], at(2)) == [Notice(DOWN, at(0))]
    assert t.update([DOWN], at(30)) == []
    assert t.update([DOWN], at(62)) == [Notice(DOWN, at(0))]  # the hourly repeat
    assert t.update([], at(70)) == [Notice(DOWN, at(0), at(70))]
    assert t.update([], at(71)) == []


def test_no_resolved_without_an_alert():
    t = AlertTracker()
    t.update([DOWN], at(0))
    assert t.update([], at(1)) == []  # cleared inside the grace period


def test_flapping_issue_waits_for_the_hour():
    t = AlertTracker()
    assert len(t.update([SHIFT], at(0))) == 1
    assert t.update([], at(5))[0].resolved
    assert t.update([SHIFT], at(10)) == []
    assert t.update([SHIFT], at(59)) == []
    assert t.update([SHIFT], at(60)) == [Notice(SHIFT, at(10))]


def test_issues_list():
    t = AlertTracker()
    t.update([DOWN, SHIFT], at(0))
    issues = t.issues(at(1))
    assert [(i["key"], i["active"]) for i in issues] == [
        ("camera_down:cam-ground", False),
        ("camera_shifted:cam-ground", True),
    ]
    assert issues[1]["last_alert_at"] == at(0)


def health(camera, state, issue=None):
    return CameraHealthMsg(camera_id=camera, ts=T0, state=state, issue=issue)


def status(stale_ground: bool, updated: bool = True) -> LotStatus:
    zone = {"capacity": 9, "occupied": 0, "free": 9, "level": "plenty", "confidence": 1.0}
    zone["trend"] = "steady"
    zones = [
        {**zone, "id": "ground", "name": "Ground", "method": "slots", "stale": stale_ground},
        {**zone, "id": "underground", "name": "Underground", "method": "flow", "stale": True},
    ]
    for z in zones:
        z["updated_at"] = T0 if updated and z["id"] == "ground" else None
    total = {k: v for k, v in zone.items() if k != "trend"} | {"stale": stale_ground}
    return LotStatus.model_validate(
        {"lot": "main", "updated_at": T0, "total": total, "zones": zones}
    )


def test_conditions():
    found = conditions(
        CONFIG,
        {"cam-ground": health("cam-ground", "degraded", "shifted")},
        status(True),
        {"underground": 4},
    )
    assert [c.key for c in found] == [
        "camera_shifted:cam-ground",
        "stale:ground",
        "clamps:underground",
    ]
    assert found[2].detail == "4"
    # a down camera's zone isn't stale on top; zones never updated and 3 clamps don't count
    found = conditions(
        CONFIG, {"cam-ground": health("cam-ground", "down")}, status(True), {"underground": 3}
    )
    assert [c.key for c in found] == ["camera_down:cam-ground"]
    assert conditions(CONFIG, {}, status(True, updated=False), {}) == []
    assert conditions(CONFIG, {"cam-ramp": health("cam-ramp", "ok")}, status(False), {}) == []


def test_payloads():
    url = "https://parking.example/#/"
    alert = alert_payload(Notice(DOWN, at(0)), CONFIG, url, "Europe/Bucharest")
    assert alert == {
        "title": "Admin: Camera cam-ground is down",
        "body": "Since 17:05 (connect failed)",
        "tag": "admin-camera_down:cam-ground",
        "url": "https://parking.example/#/admin/cameras/cam-ground",
        "level": None,
        "kind": "admin_alert",
        "issue": "camera_down:cam-ground",
        "resolved": False,
    }
    stale = Condition("stale", "ground")
    done = alert_payload(Notice(stale, at(0), at(12)), CONFIG, url, "UTC", "ro")
    assert done["title"] == "Admin: Parter: live data again"
    assert done["body"] == "Resolved: 14:05–14:17"
    assert done["url"] == "https://parking.example/#/admin"
    assert done["resolved"] is True
