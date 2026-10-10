"""P7.8: the admin alert rules (notifications.md §5.1) without the API."""

import json
from datetime import UTC, datetime, timedelta

from parking.config import LotConfig
from parking.core.system import SystemStats, cpu_temp_c, disk_pct, system_stats
from parking.messages import CameraHealthMsg, LotStatus
from parking.push.admin_alerts import (
    AlertTracker,
    Condition,
    Notice,
    alert_payload,
    backup_condition,
    conditions,
    system_conditions,
)

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


# --- P8.7: the machines and the backup ---


def test_restart_alert_has_no_resolved():
    t = AlertTracker()
    restarted = Condition("api_restarted", "api")
    assert t.update([restarted], at(0)) == [Notice(restarted, at(0))]
    assert t.update([], at(2)) == []
    assert t.issues(at(2)) == []


def test_system_conditions():
    def worker(camera, state="ok", **fields):
        return CameraHealthMsg(camera_id=camera, ts=T0, state=state, **fields)

    health = {
        "cam-ground": worker("cam-ground", disk_pct=85.1, cpu_temp_c=79.9),
        "cam-ramp": worker("cam-ramp", "down", disk_pct=99.0, cpu_temp_c=90.0),  # old numbers
    }
    found = system_conditions(health, SystemStats(disk_pct=85.0, cpu_temp_c=80.0))
    assert [(c.key, c.detail) for c in found] == [
        ("cpu_temp:api", "80 °C"),
        ("disk:cam-ground", "85%"),
    ]
    assert system_conditions({"cam-ground": worker("cam-ground")}, SystemStats()) == []


def test_backup_condition(tmp_path):
    file = tmp_path / "last-run.json"
    assert backup_condition(file, T0) is None  # backups not set up

    def ran(status, message="", ts="2026-10-09T01:30:07Z"):
        file.write_text(json.dumps({"ts": ts, "status": status, "message": message}))
        return backup_condition(file, T0)

    assert ran("ok") is None  # 12.5 h ago
    assert ran("ok", ts="2026-10-08T01:30:07Z").detail == "no backup since the last good one"
    assert ran("failed", "the upload failed") == Condition(
        "backup_failed", "api", "the upload failed"
    )
    assert ran("local_only").detail == "not copied off the machine"
    file.write_text("{half")
    assert backup_condition(file, T0).detail == "the status file can't be read"


def test_system_stats(tmp_path):
    assert 0 <= disk_pct(tmp_path) <= 100
    assert disk_pct(tmp_path / "missing") is None
    assert cpu_temp_c(tmp_path) is None  # a machine without thermal zones
    for zone, value in (("thermal_zone0", "53450\n"), ("thermal_zone1", "61200\n")):
        (tmp_path / zone).mkdir()
        (tmp_path / zone / "temp").write_text(value)
    (tmp_path / "thermal_zone2").mkdir()
    (tmp_path / "thermal_zone2" / "temp").write_text("n/a")
    assert cpu_temp_c(tmp_path) == 61.2
    assert system_stats(tmp_path, tmp_path).cpu_temp_c == 61.2


def test_machine_payloads():
    url = "https://parking.example/#/"
    disk = Condition("disk", "cam-ground", "91%")
    alert = alert_payload(Notice(disk, at(0)), CONFIG, url)
    assert alert["title"] == "Admin: Disk almost full on camera cam-ground's machine"
    assert alert["body"] == "91% used, since 14:05"
    assert alert["tag"] == "admin-disk:cam-ground"
    assert alert["url"] == "https://parking.example/#/admin"
    done = alert_payload(Notice(Condition("backup_failed", "api"), at(0), at(5)), CONFIG, url)
    assert done["title"] == "Admin: The backup works again"
    assert (
        alert_payload(Notice(Condition("backup_failed", "api"), at(0)), CONFIG, url)["body"]
        == "Since 14:05"
    )
