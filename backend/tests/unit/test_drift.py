"""Drift test notes and report (vision.md §10, P5.11)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.core.drift import (
    DriftNote,
    append_drift_note,
    load_drift_notes,
    measure_drift,
    parse_drift_notes,
    report,
)

T0 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
runner = CliRunner()


def _notes(errors, true=30, step=timedelta(days=1)):
    return [DriftNote(T0 + i * step, true, true + e) for i, e in enumerate(errors)]


def test_drift_per_day_is_the_error_change_over_the_days():
    result = measure_drift(_notes([0, 1, 1, 3, 2, 4, 6, 7]))
    assert result.days == 7 and result.drift_per_day == 1.0
    assert result.verdict == "PASSED"
    assert [s.step for s in result.steps] == [0, 1, 0, 2, -1, 2, 2, 1]
    assert result.steps[0].step_per_day is None and result.steps[3].step_per_day == 2.0


def test_drift_counts_from_the_first_error_and_ignores_the_sign():
    # starts 2 off (not corrected on day 0) and the app loses 3 cars a day
    result = measure_drift(_notes([2, -1, -4, -7, -10, -13, -16, -19]))
    assert result.drift_per_day == 3.0 and result.verdict == "FAILED"
    assert "first error is +2" in report(result, "underground")


def test_exactly_the_target_passes():
    assert measure_drift(_notes([0] * 7 + [14])).verdict == "PASSED"
    assert measure_drift(_notes([0] * 7 + [15])).verdict == "FAILED"


def test_short_tests_are_incomplete():
    assert measure_drift([]).verdict == "INCOMPLETE"
    one = measure_drift(_notes([0]))
    assert one.drift_per_day is None and one.verdict == "INCOMPLETE"
    assert measure_drift(_notes([0, 0, 0])).verdict == "INCOMPLETE"
    # the last count taken a few hours early still makes a week
    week = _notes([0] * 7) + [DriftNote(T0 + timedelta(days=6, hours=20), 30, 31)]
    assert measure_drift(week).verdict == "PASSED"
    assert measure_drift(_notes([0, 40]), min_days=1).verdict == "FAILED"


def test_notes_are_sorted_and_same_time_notes_have_no_rate():
    notes = _notes([0, 2])[::-1] + [DriftNote(T0 + timedelta(days=1), 30, 33)]
    result = measure_drift(notes, min_days=1)
    assert [s.note.error for s in result.steps] == [0, 2, 3]
    assert result.steps[2].step_per_day is None
    assert result.drift_per_day == 3.0


def test_parse_drift_notes():
    text = (
        "﻿ts,true_occupied,app_occupied,note\n"
        '2026-11-03T08:05:00+02:00,31,33,"rain, night"\n'
        "\n"
        "2026-11-02T08:00:00,30,30\n"
    )
    notes = parse_drift_notes(text)
    assert [n.error for n in notes] == [0, 2]
    assert notes[0].ts == T0 and notes[1].note == "rain, night"
    assert len(parse_drift_notes("ts,true_occupied,app_occupied\n2026-11-02,1,2\n")) == 1


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty file"),
        ("ts,true,app\n", "header must be"),
        ("ts,true_occupied,app_occupied\n2026-11-02,1\n", "line 2: expected 3 columns"),
        ("ts,true_occupied,app_occupied\nmonday,1,2\n", "not an ISO time"),
        ("ts,true_occupied,app_occupied\n2026-11-02,1.5,2\n", "true_occupied '1.5'"),
        ("ts,true_occupied,app_occupied\n2026-11-02,1,-2\n", "app_occupied must be >= 0"),
    ],
)
def test_parse_drift_notes_errors(text, message):
    with pytest.raises(ValueError, match=message):
        parse_drift_notes(text, "n.csv")


def test_append_then_load(tmp_path):
    path = tmp_path / "labels" / "drift-underground.csv"
    for n in _notes([0, 1]):
        append_drift_note(path, DriftNote(n.ts, n.true_occupied, n.app_occupied, "a, b"))
    assert path.read_text().startswith("ts,true_occupied,app_occupied,note\n2026-11-02T08:00:00")
    assert [(n.error, n.note) for n in load_drift_notes(path)] == [(0, "a, b"), (1, "a, b")]


def _write_lot(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lot.yaml").write_text(
        """
version: 1
lot: {id: main, name: P, location: {lat: 45, lon: 0}, timezone: UTC}
zones:
  - {id: ground, name: {en: Ground}, method: slots}
  - {id: underground, name: {en: Underground}, method: flow, capacity: 60}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: file:img.jpg
    slots_file: config/slots/cam-ground.json
    detector: {model: models/none, conf: 0.5}
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: video:x.mp4
    lines_file: config/lines/cam-ramp.json
    detector: {model: models/none, conf: 0.5}
"""
    )
    monkeypatch.chdir(tmp_path)


def test_cli_note_and_report(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    for day, (true, shown) in enumerate([(30, 30), (41, 42), (28, 31), (35, 39)]):
        at = (T0 + timedelta(days=day * 7 / 3)).isoformat()
        args = ["drift-note", "--zone", "underground", "--true", str(true), "--app", str(shown)]
        result = runner.invoke(app, [*args, "--at", at, "--note", f"day {day}"])
        assert result.exit_code == 0, result.output
    assert "true 35, app 39, error +4" in result.output
    assert (tmp_path / "data" / "labels" / "drift-underground.csv").is_file()

    result = runner.invoke(app, ["drift-report", "--zone", "underground"])
    assert result.exit_code == 0, result.output
    assert "drift: 0.57 cars/day" in result.output and "verdict: PASSED" in result.output

    args = ["drift-report", "--zone", "underground"]
    result = runner.invoke(app, [*args, "--target", "0.5", "--json"])
    assert result.exit_code == 1
    out = json.loads(result.output)
    assert out["verdict"] == "FAILED" and out["zone"] == "underground"
    assert [n["error"] for n in out["notes"]] == [0, 1, 3, 4]

    result = runner.invoke(app, ["drift-report", "--zone", "underground", "--days", "14"])
    assert result.exit_code == 1 and "verdict: INCOMPLETE" in result.output


def test_cli_note_reads_the_app_value_from_the_api(tmp_path, monkeypatch):
    import httpx

    _write_lot(tmp_path, monkeypatch)
    seen = []

    def fake_get(url, timeout):
        seen.append(url)
        if "down" in url:
            raise httpx.ConnectError("refused")
        zones = [{"id": "ground", "occupied": 3}, {"id": "underground", "occupied": 44}]
        return httpx.Response(200, json={"zones": zones}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    args = ["drift-note", "--zone", "underground", "--true", "42"]
    result = runner.invoke(app, [*args, "--api", "http://api:8000/", "--file", "n.csv"])
    assert result.exit_code == 0, result.output
    assert seen == ["http://api:8000/api/status"] and "app 44, error +2" in result.output
    assert load_drift_notes(tmp_path / "n.csv")[0].app_occupied == 44

    result = runner.invoke(app, [*args, "--api", "http://down:1", "--file", "n.csv"])
    assert result.exit_code == 1 and "--app" in result.output
    assert len(load_drift_notes(tmp_path / "n.csv")) == 1


def test_cli_errors(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    result = runner.invoke(app, ["drift-report", "--zone", "underground"])
    assert result.exit_code == 1 and "drift-note" in result.output
    result = runner.invoke(app, ["drift-report", "--zone", "ground"])
    assert result.exit_code == 1 and "slots zone" in result.output
    result = runner.invoke(app, ["drift-note", "--zone", "nope", "--true", "1", "--app", "1"])
    assert result.exit_code == 1 and "zones: ground, underground" in result.output
    args = ["drift-note", "--zone", "underground", "--true", "1", "--app", "1"]
    assert runner.invoke(app, [*args, "--at", "nope"]).exit_code == 2
    (tmp_path / "bad.csv").write_text("x\n")
    result = runner.invoke(app, ["drift-report", "--zone", "underground", "--file", "bad.csv"])
    assert result.exit_code == 1 and "header must be" in result.output
