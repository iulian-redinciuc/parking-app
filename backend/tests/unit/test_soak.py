"""backend/scripts/soak.py: the P4.11 soak report (testing.md §7)."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "soak", Path(__file__).parents[2] / "scripts" / "soak.py"
)
soak = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(soak)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _sample(minutes: float, mem: float = 200, cam: str = "ok", api_ok: bool = True, **kw) -> dict:
    s = {
        "ts": (T0 + timedelta(minutes=minutes)).isoformat(),
        "host": {"temp_c": kw.get("temp", 55.0), "throttled": kw.get("throttled", "0x0")},
        "containers": {
            "parking-api": {"status": "running", "restarts": 0, "mem_mb": 150.0},
            "parking-vision-occupancy": {
                "status": kw.get("status", "running"),
                "restarts": kw.get("restarts", 0),
                "started_at": kw.get("started", "a"),
                "mem_mb": mem,
            },
            "parking-tunnel": {"status": "missing"},
        },
        "api": {"ok": True, "cameras": {"cam-ground": cam}} if api_ok else {"ok": False},
        "zones": {"ground": {"stale": kw.get("stale", False), "free": 3}},
        "reconnects": {"parking-vision-occupancy": kw.get("reconnects", 0)},
    }
    return s


def _write(tmp_path: Path, samples: list[dict]) -> Path:
    p = tmp_path / "soak.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in samples) + '\n{"ts": "cut off')
    return p


def _week(step: int = 10, **kw) -> list[dict]:
    return [_sample(m, **kw) for m in range(0, 7 * 24 * 60 + 1, step)]


def test_parse_size_mb():
    assert soak.parse_size_mb("180.3MiB") == 180.3
    assert soak.parse_size_mb("1.5GiB") == 1536.0
    assert soak.parse_size_mb("512KiB") == 0.5
    assert soak.parse_size_mb("n/a") is None


def test_clean_week_passes(tmp_path):
    samples = soak.load(_write(tmp_path, _week()))  # the cut-off last line is skipped
    r = soak.report(samples, interval=600)
    assert r["passed"], r["reasons"]
    assert r["days"] == 7.0
    assert r["outages"] == []
    m = r["memory"]["parking-vision-occupancy"]
    assert m["growth_mb"] == 0 and not m["grew"]
    assert "parking-tunnel" not in r["memory"]
    assert "verdict: PASSED" in soak.format_report(r)


def _parsed(samples):
    return [{**s, "_t": datetime.fromisoformat(s["ts"])} for s in samples]


def test_short_run_is_not_passed():
    r = soak.report(_parsed(_week()[:100]), interval=600)
    assert not r["passed"]
    assert r["reasons"] == ["only 0.69 of 7 days sampled"]


def test_memory_growth_fails():
    leaky = _week()
    for i, s in enumerate(leaky):  # +60 MB over the week, linear
        s["containers"]["parking-vision-occupancy"]["mem_mb"] = 200 + 60 * i / len(leaky)
    r = soak.report(_parsed(leaky), interval=600)
    m = r["memory"]["parking-vision-occupancy"]
    assert m["grew"] and m["growth_mb"] > 40
    assert m["slope_mb_per_day"] == pytest.approx(60 / 7, rel=0.05)
    assert r["reasons"] == ["memory grew: parking-vision-occupancy"]


def test_warmup_and_noise_are_allowed():
    week = _week()
    for i, s in enumerate(week):
        mem = 120 if i < 3 else 200 + (5 if i % 2 else -5)  # start-up below the steady state
        s["containers"]["parking-vision-occupancy"]["mem_mb"] = mem
    r = soak.report(_parsed(week), interval=600)
    assert r["passed"], r["reasons"]


def test_recovered_outage_is_listed_but_passes():
    week = _week()
    for s in week[100:106]:  # 1 h camera down, zone stale, then back
        s["api"]["cameras"]["cam-ground"] = "down"
        s["zones"]["ground"]["stale"] = True
    week[106]["reconnects"]["parking-vision-occupancy"] = 1
    r = soak.report(_parsed(week), interval=600)
    assert r["passed"], r["reasons"]
    whats = sorted(o["what"] for o in r["outages"])
    assert whats == ["camera cam-ground down", "zone ground stale"]
    assert all(o["minutes"] == 60 and o["end"] for o in r["outages"])
    assert r["reconnects"] == {"parking-vision-occupancy": 1}


def test_unrecovered_outage_fails():
    week = _week()
    for s in week[-10:]:
        s["api"] = {"ok": False}
        s["containers"]["parking-vision-occupancy"]["status"] = "exited"
    r = soak.report(_parsed(week), interval=600)
    assert not r["passed"]
    assert r["reasons"] == [
        "still down at the last sample: api unreachable, container parking-vision-occupancy exited"
    ]
    assert "STILL DOWN" in soak.format_report(r)


def test_sampling_gap_is_an_outage():
    week = [s for i, s in enumerate(_week()) if not 200 <= i < 230]  # 5 h without samples
    r = soak.report(_parsed(week), interval=600)
    assert r["passed"]
    (gap,) = r["outages"]
    assert gap["what"].startswith("no samples") and gap["minutes"] == 310


def test_restarts_temperature_and_throttling():
    week = _week()
    for s in week[500:]:
        s["containers"]["parking-vision-occupancy"].update(restarts=2, started_at="b")
    week[10]["host"].update(temp_c=71.5, throttled="0x80000")
    r = soak.report(_parsed(week), interval=600)
    m = r["memory"]["parking-vision-occupancy"]
    assert (m["restarts"], m["starts"]) == (2, 1)
    assert r["temp_c"]["max"] == 71.5 and r["temp_c"]["median"] == 55.0
    assert r["throttled"] == ["0x80000"]


def test_cli_report_exit_code(tmp_path, capsys):
    p = _write(tmp_path, _week())
    assert soak.main(["report", str(p), "--interval", "600"]) == 0
    capsys.readouterr()
    assert soak.main(["report", str(p), "--days", "8", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["reasons"] == ["only 7.00 of 8 days sampled"]


def test_empty_file(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert soak.main(["report", str(p)]) == 1


def test_items_never_ok_are_listed_not_counted():
    week = _week()
    for s in week:  # a camera that isn't installed yet: no data, its zone always stale
        s["api"]["cameras"]["cam-ramp"] = "unknown"
        s["zones"]["underground"] = {"stale": True, "free": 60}
    r = soak.report(_parsed(week), interval=600)
    assert r["passed"], r["reasons"]
    assert r["never_ok"] == ["camera cam-ramp", "container parking-tunnel", "zone underground"]
    assert "never ok, not counted: camera cam-ramp" in soak.format_report(r)


def test_api_never_answering_fails():
    r = soak.report(_parsed(_week(api_ok=False)), interval=600)
    assert r["reasons"] == ["the API never answered"]
