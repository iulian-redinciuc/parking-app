"""scripts/load/sse.py: the P8.10 load test's parsing, statistics and verdict (testing.md §8)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "load_sse", Path(__file__).parents[3] / "scripts" / "load" / "sse.py"
)
load = importlib.util.module_from_spec(_SPEC)
sys.modules["load_sse"] = load  # dataclasses look their module up while it is being executed
_SPEC.loader.exec_module(load)

T0 = 1_800_000_000.0


def _args(*extra):
    return load.parse_args(["https://example.test/", *extra])


def _status(updated_at: str) -> str:
    return json.dumps({"v": 1, "updated_at": updated_at})


def _recorder(delays, mem=None, clients=500, dropped=(0, 0), connected=None):
    """A finished 600 s run: `delays` per event id, one memory sample every 10 s."""
    rec = load.Recorder(connected=connected or clients, window=(T0, T0 + 600))
    rec.delays = delays
    mem = mem if mem is not None else [120.0] * 60
    for i, value in enumerate(mem):
        rec.samples.append(
            {"t": T0 + 10 * i, "clients": clients, "dropped": dropped[0], "mem_mb": value}
            | {"cpu_pct": 4.0}
        )
    rec.samples[-1]["dropped"] = dropped[1]
    return rec


def _good_delays(events=12, clients=500, delay=0.2):
    return {i: [delay] * clients for i in range(events)}


def test_parser_reads_the_stream_of_api_md():
    parser = load.SseParser()
    text = (
        "retry: 3000\n\n"
        'event: status\nid: 7\ndata: {"v":1}\n\n'
        ": a comment\n"
        "event: ping\ndata:\n\n"
        "\n"
        "data: a\ndata: b\n\n"
    )
    events = [e for line in text.split("\n") if (e := parser.feed(line)) is not None]
    assert [(e.event, e.id, e.data) for e in events] == [
        ("status", "7", '{"v":1}'),
        ("ping", None, ""),
        ("message", None, "a\nb"),
    ]


def test_parse_ts_and_percentile():
    assert load.parse_ts("2026-10-07T17:05:12.120Z") == pytest.approx(1791392712.12)
    values = [float(i) for i in range(1, 101)]
    assert load.percentile(values, 95) == 95.0
    assert load.percentile(values, 50) == 50.0
    assert load.percentile([3.0], 95) == 3.0
    assert load.percentile([], 95) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("115.2MiB / 512MiB|1.53%", (115.2, 1.53)),
        ("1.5GiB / 4GiB|101.20%", (1536.0, 101.2)),
        ("", None),
        ("-- / --|--", None),
    ],
)
def test_parse_docker_stats(text, expected):
    assert load.parse_docker_stats(text) == expected


def test_memory_verdict_compares_first_and_last_quarter():
    steady = load.memory_verdict([100.0] * 20 + [110.0] * 40, 16, 10)
    assert steady["stable"] and steady["growth_mb"] == 10.0 and steady["allowed_mb"] == 16.0
    growing = load.memory_verdict([100.0 + i for i in range(60)], 16, 10)
    assert not growing["stable"] and growing["growth_mb"] == 45.0
    # the percentage wins on a large container
    assert load.memory_verdict([400.0] * 30 + [430.0] * 30, 16, 10)["stable"]
    assert load.memory_verdict([100.0] * 7, 16, 10) is None


def test_recorder_counts_only_deliveries_inside_the_measurement():
    rec = load.Recorder()
    data = _status("2027-01-15T08:00:10.000Z")
    sent = load.parse_ts("2027-01-15T08:00:10.000Z")
    rec.delivered(5, data, sent + 0.1)  # still connecting: no window yet
    rec.window = (sent - 1, sent + 600)
    rec.delivered(5, data, sent + 0.25)
    rec.delivered(5, data, sent + 0.5)
    rec.delivered(4, _status("2027-01-15T08:00:08.000Z"), sent + 0.1)  # changed before the start
    rec.delivered(6, _status("2027-01-15T08:10:09.900Z"), sent + 600.4)  # late, but it counts
    rec.delivered(7, _status("2027-01-15T08:10:10.100Z"), sent + 600.2)  # changed after the end
    assert list(rec.delays) == [5, 6]
    assert rec.delays[5] == pytest.approx([0.25, 0.5])
    assert rec.delays[6] == pytest.approx([0.5])


def test_report_passes_a_clean_run():
    result = load.report(_recorder(_good_delays()), _args(), 0.1)
    assert result["passed"] and result["reasons"] == []
    assert result["url"] == "https://example.test"
    assert result["events"] == 12 and result["deliveries"] == 6000
    assert result["delay_s"]["p95"] == 0.2
    assert result["clients"] == {
        "wanted": 500,
        "connected": 500,
        "server_min": 500,
        "server_max": 500,
    }
    assert result["cpu_pct"] == {"mean": 4.0, "max": 4.0}
    assert "PASSED" in load.render(result) and "NOT PASSED" not in load.render(result)


def test_report_fails_on_a_slow_tail():
    delays = _good_delays()
    delays[0] = [2.5] * 500  # one event in twelve: more than 5% of the deliveries
    result = load.report(_recorder(delays), _args(), 0.0)
    assert not result["passed"]
    assert result["reasons"] == ["p95 delay 2.500 s, limit 2 s"]


def test_report_fails_on_errors_missing_clients_and_dropped_events():
    rec = _recorder(_good_delays(clients=496), dropped=(3, 9), connected=498)
    rec.error("disconnected", "the server closed the stream")
    rec.error("disconnected", "again")
    result = load.report(rec, _args(), 0.0)
    assert result["errors"] == {"disconnected": 2, "undelivered": 24, "dropped_by_server": 6}
    assert result["first_error"] == {"disconnected": "the server closed the stream"}
    assert result["reasons"] == [
        "only 498 of 500 clients connected",
        "errors: disconnected 2, dropped_by_server 6, undelivered 24",
    ]
    assert "NOT PASSED" in load.render(result)


def test_report_needs_enough_status_changes():
    result = load.report(_recorder(_good_delays(events=3)), _args(), 0.0)
    assert result["reasons"] == [
        "3 status change(s) during the measurement, 10 needed: run it while the counts change"
    ]


def test_report_needs_stable_memory_and_a_measurement_of_it():
    growing = load.report(_recorder(_good_delays(), mem=[100.0 + i for i in range(60)]), _args(), 0)
    assert growing["reasons"] == ["API memory grew 45.0 MB (allowed 16.0 MB)"]
    rec = _recorder(_good_delays())
    for sample in rec.samples:
        del sample["mem_mb"], sample["cpu_pct"]
    unmeasured = load.report(rec, _args(), 0)
    assert not unmeasured["passed"] and unmeasured["memory"] is None
    assert "not measured" in unmeasured["reasons"][0]
    assert unmeasured["cpu_pct"] is None


def test_args_ssh_implies_the_api_container():
    assert _args("--ssh", "server").docker == "parking-api"
    assert _args("--ssh", "server", "--docker", "other").docker == "other"
    assert _args().docker is None
    with pytest.raises(SystemExit):
        _args("--clients", "0")
