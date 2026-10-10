"""scripts/resilience/drill.py: the P8.11 drills' verdicts and the watch loop (testing.md §9)."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "resilience_drill", Path(__file__).parents[3] / "scripts" / "resilience" / "drill.py"
)
drill = importlib.util.module_from_spec(_SPEC)
sys.modules["resilience_drill"] = drill  # dataclasses look their module up while it is executed
_SPEC.loader.exec_module(drill)

T0 = 1_800_000_000.0
REAL_CLIENT = httpx.Client
ZONES = frozenset({"ground", "underground"})
DOWN_ALERT = frozenset({("camera_down", "cam-ground")})


def _run(*phases, step=2.0):
    """Samples from `(seconds, state, extra)` phases; state: live | down | nodata | zone names."""
    samples, t = [], T0
    for seconds, state, *extra in phases:
        fields = extra[0] if extra else {}
        for _ in range(int(seconds / step)):
            if state == "down":
                samples.append(drill.Sample(t, False))
            elif state == "nodata":
                samples.append(drill.Sample(t, True, flow_events=fields.get("flow_events")))
            else:
                stale = frozenset() if state == "live" else frozenset(state.split(","))
                defaults = {"flow_events": 0, "alerts": frozenset()}
                samples.append(drill.Sample(t, True, stale, ZONES, **(defaults | fields)))
            t += step
    return samples


def test_power_server_unreachable_then_stale_then_live_passes():
    samples = _run((20, "live"), (90, "down"), (10, "ground,underground"), (70, "live"))
    result = drill.judge("power-server", samples)
    assert result["passed"], result["reasons"]
    assert result["outage_s"] == 100
    assert result["stale_seen"] is True
    assert [step["state"] for step in result["timeline"]] == [
        "live",
        "unreachable",
        "stale: ground, underground",
        "live",
    ]


def test_not_live_at_the_start_is_not_a_drill():
    result = drill.judge("power-server", _run((10, "ground"), (90, "down"), (70, "live")))
    assert not result["passed"]
    assert "not live at the start" in result["reasons"][0]


def test_fault_never_seen():
    result = drill.judge("power-server", _run((300, "live")))
    assert result["reasons"] == [
        "the fault was never seen (nothing was cut, or the cut was too short)"
    ]


def test_two_failed_reads_are_a_blip_not_an_outage():
    samples = _run((20, "live"), (4, "down"), (100, "live"))
    assert drill.fault_index("power-server", samples) is None
    samples = _run((20, "live"), (6, "down"), (100, "live"))
    assert drill.fault_index("power-server", samples) == 10


def test_still_down_or_stale_at_the_end_fails():
    result = drill.judge("power-server", _run((20, "live"), (400, "down")))
    assert result["reasons"] == ["did not come back by itself: unreachable at the end"]
    result = drill.judge("power-server", _run((20, "live"), (60, "down"), (300, "nodata")))
    assert result["reasons"] == ["did not come back by itself: no data at the end"]


def test_live_has_to_hold_for_the_settle_time():
    samples = _run((20, "live"), (60, "down"), (30, "live"))
    assert not drill.judge("power-server", samples)["passed"]
    assert drill.judge("power-server", samples, settle=20)["passed"]
    # a relapse restarts the clock: the recovery is the last time it became live
    samples = _run((20, "live"), (60, "down"), (30, "live"), (10, "ground"), (70, "live"))
    assert drill.judge("power-server", samples)["outage_s"] == 100


def test_back_later_than_allowed_fails():
    samples = _run((20, "live"), (320, "down"), (70, "live"))
    result = drill.judge("power-server", samples)
    assert result["reasons"] == ["back after 320 s, more than the 300 s allowed"]
    assert drill.judge("internet-t1", samples)["passed"]  # a 10 min cut: 780 s


def test_power_site_api_stays_up_zones_go_stale():
    samples = _run((20, "live"), (120, "ground,underground"), (70, "live"))
    result = drill.judge("power-site", samples)
    assert result["passed"], result["reasons"]
    assert result["outage_s"] == 120


def test_power_site_fails_when_the_api_went_away():
    samples = _run((20, "live"), (60, "ground"), (10, "down"), (70, "live"))
    result = drill.judge("power-site", samples)
    assert result["reasons"] == [
        "the API was unreachable during the drill (it has to keep answering)"
    ]


def test_internet_needs_flow_events_after_the_reconnect():
    cut = (600, "ground,underground", {"flow_events": 41})
    samples = _run((20, "live", {"flow_events": 41}), cut, (70, "live", {"flow_events": 44}))
    result = drill.judge("internet", samples)
    assert result["passed"], result["reasons"]
    assert result["flow_events"] == 3

    samples = _run((20, "live", {"flow_events": 41}), cut, (70, "live", {"flow_events": 41}))
    result = drill.judge("internet", samples)
    assert not result["passed"]
    assert "0 flow events arrived after the reconnect" in result["reasons"][0]
    assert drill.judge("internet", samples, min_flow=0)["passed"]  # a lot without a flow camera


def test_camera_only_its_zone_alert_fires_and_clears():
    samples = _run(
        (20, "live"),
        (120, "ground"),
        (480, "ground", {"alerts": DOWN_ALERT}),
        (70, "live"),
    )
    result = drill.judge("camera", samples, zone="ground")
    assert result["passed"], result["reasons"]
    assert result["outage_s"] == 600
    assert result["alert_after_s"] == 120


def test_camera_failures():
    out = (600, "ground", {"alerts": DOWN_ALERT})
    both = (600, "ground,underground", {"alerts": DOWN_ALERT})
    result = drill.judge("camera", _run((20, "live"), both, (70, "live")), zone="ground")
    assert result["reasons"] == ["other zones went stale too: underground"]

    result = drill.judge("camera", _run((20, "live"), (600, "ground"), (70, "live")), zone="ground")
    assert result["reasons"] == ["no admin alert (camera_down / stale) while the camera was out"]

    unread = (600, "ground", {"alerts": None})
    result = drill.judge("camera", _run((20, "live"), unread, (70, "live")), zone="ground")
    assert result["reasons"] == ["admin alerts not read: set ADMIN_TOKEN in the environment"]

    stuck = (70, "live", {"alerts": DOWN_ALERT})
    result = drill.judge("camera", _run((20, "live"), out, stuck), zone="ground")
    assert result["reasons"] == ["the alert is still open at the end: camera_down cam-ground"]

    # the other zone going stale is not this camera's fault being seen
    result = drill.judge("camera", _run((20, "live"), (600, "underground")), zone="ground")
    assert "the fault was never seen" in result["reasons"][0]


def test_parse_args_defaults_and_camera_zone():
    assert drill.parse_args(["power-site", "http://x"]).max_outage == 300
    assert drill.parse_args(["internet", "http://x"]).max_outage == 780
    assert drill.parse_args(["camera", "http://x", "--zone", "ground"]).zone == "ground"
    with pytest.raises(SystemExit):
        drill.parse_args(["camera", "http://x"])


# ---------------------------------------------------------------------------- watching


class FakeApi:
    """An API that goes through `script` (one state per look): live | down | nodata | zones."""

    def __init__(self, script, alerts=None):
        self.script = list(script)
        self.alerts = alerts or {}
        self.look = -1
        self.admin_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            self.look += 1
        state = self.script[min(self.look, len(self.script) - 1)]
        if state == "down":
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "ingest": {"flow_events": self.look}})
        if request.url.path == "/api/admin/alerts":
            self.admin_calls += 1
            assert request.headers["Authorization"] == "Bearer secret"
            issues = [
                {"kind": kind, "subject": subject, "active": active}
                for kind, subject, active in self.alerts.get(state, [])
            ]
            return httpx.Response(200, json={"enabled": False, "available": True, "issues": issues})
        if state == "nodata":
            return httpx.Response(503, json={"error": {"code": "unavailable"}})
        stale = [] if state == "live" else state.split(",")
        zones = [{"id": z, "stale": z in stale} for z in sorted(ZONES)]
        return httpx.Response(200, json={"zones": zones})


def _watch(api, *argv, headers=None):
    args = drill.parse_args([*argv, "http://api.test", "--interval", "0", "--settle", "0"])
    said = []
    with httpx.Client(
        base_url="http://api.test", transport=httpx.MockTransport(api), headers=headers
    ) as client:
        samples = drill.watch(args, client, say=said.append)
    return args, samples, said


def test_watch_follows_a_power_cut_and_stops_once_live_again():
    api = FakeApi(["live", "live", "down", "down", "down", "nodata", "ground", "live", "live"])
    args, samples, said = _watch(api, "power-server")
    assert [s.state for s in samples] == [
        "live",
        "live",
        "unreachable",
        "unreachable",
        "unreachable",
        "no data",
        "stale: ground",
        "live",
    ]
    assert said[0].endswith("live: pull the plug now (power-server)")
    assert [line.split("  ")[1] for line in said[1:]] == [
        "unreachable",
        "no data",
        "stale: ground",
        "live",
    ]
    assert samples[0].zones == ZONES
    assert samples[1].flow_events == 1
    assert samples[0].alerts is None  # no ADMIN_TOKEN: the admin route isn't asked
    assert api.admin_calls == 0
    assert drill.judge("power-server", samples, settle=args.settle)["passed"]


def test_watch_does_not_start_when_the_app_is_not_live(capsys):
    _, samples, said = _watch(FakeApi(["ground", "live"]), "power-server")
    assert len(samples) == 1 and said == []


def test_watch_gives_up_when_nothing_is_cut(monkeypatch):
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(drill.time, "time", lambda: float(next(clock)))
    _, samples, _ = _watch(FakeApi(["live"]), "power-site")
    assert all(s.live for s in samples)
    assert samples[-1].t == 400  # the first look past --fault-wait (300 s from the start at 0)
    assert "never seen" in drill.judge("power-site", samples)["reasons"][0]


def test_watch_gives_up_when_it_does_not_come_back(monkeypatch):
    clock = iter(range(0, 100_000, 100))
    monkeypatch.setattr(drill.time, "time", lambda: float(next(clock)))
    args, samples, _ = _watch(FakeApi(["live", "ground"]), "power-site")
    assert samples[-1].t - samples[1].t > args.max_outage
    assert not drill.judge("power-site", samples, settle=0)["passed"]


def test_watch_reads_active_admin_alerts_with_the_token():
    alerts = {"ground": [("camera_down", "cam-ground", True), ("disk", "api", False)]}
    api = FakeApi(["live", "ground", "ground", "live"], alerts)
    args, samples, _ = _watch(
        api, "camera", "--zone", "ground", headers={"Authorization": "Bearer secret"}
    )
    assert samples[1].alerts == DOWN_ALERT  # the issue still in its grace time isn't an alert
    assert samples[-1].alerts == frozenset()
    result = drill.judge("camera", samples, zone="ground", settle=args.settle)
    assert result["passed"], result["reasons"]


def test_main_exit_codes_and_out_file(monkeypatch, tmp_path, capsys):
    def fake_client(script):
        api = FakeApi(script)

        def make(**kwargs):
            return REAL_CLIENT(transport=httpx.MockTransport(api), **kwargs)

        return make

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    base = ["http://api.test/", "--interval", "0", "--settle", "0"]
    out = tmp_path / "drill" / "run.json"

    monkeypatch.setattr(drill.httpx, "Client", fake_client(["live", "ground", "live"]))
    assert drill.main(["power-site", *base, "--out", str(out)]) == 0
    assert capsys.readouterr().out.rstrip().endswith("PASSED")
    saved = json.loads(out.read_text())
    assert saved["passed"] is True and saved["scenario"] == "power-site"

    monkeypatch.setattr(drill.httpx, "Client", fake_client(["live", "ground", "live"]))
    assert drill.main(["camera", *base, "--zone", "ground"]) == 1  # no ADMIN_TOKEN: no alert read
    assert "NOT PASSED\n  - admin alerts not read" in capsys.readouterr().out

    monkeypatch.setattr(drill.httpx, "Client", fake_client(["down"]))
    assert drill.main(["power-server", *base]) == 2
    assert "not live (unreachable)" in capsys.readouterr().err

    monkeypatch.setattr(drill.httpx, "Client", fake_client(["live"]))
    assert drill.main(["camera", *base, "--zone", "roof"]) == 2
    assert "no zone 'roof'" in capsys.readouterr().err
