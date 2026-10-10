"""Flow evaluation (vision/evaluate.py flow part, `parking evaluate-flow`, P5.9)."""

import json

import pytest
from typer.testing import CliRunner

from parking.cli import app
from parking.vision import tracking
from parking.vision.evaluate import (
    FlowLabel,
    flow_labels_csv,
    flow_lines,
    match_flow,
    parse_flow_labels,
)
from tests.unit.test_flow_worker import BlobBackend, repo  # noqa: F401 (fixture)


def ev(t, d="in", note=""):
    return FlowLabel(t, d, note)


# --- labels CSV (config.md §4) ---


def test_parse_flow_labels():
    text = "\ufeffvideo_time_s,direction,note\n57.9,OUT,van\n12.4,in,\n\n3,in\n"
    assert parse_flow_labels(text) == [ev(3.0), ev(12.4), ev(57.9, "out", "van")]
    assert parse_flow_labels("video_time_s,direction\n1.5,out\n") == [ev(1.5, "out")]
    assert parse_flow_labels("video_time_s,direction,note\n") == []


@pytest.mark.parametrize(
    ("text", "msg"),
    [
        ("", "empty"),
        ("time,direction\n1,in\n", "header"),
        ("video_time_s,direction,note\n1,sideways,\n", "line 2: direction"),
        ("video_time_s,direction,note\nabc,in,\n", "not a number"),
        ("video_time_s,direction,note\n-1,in,\n", ">= 0"),
        ("video_time_s,direction,note\nnan,in,\n", ">= 0"),
        ("video_time_s,direction,note\n1,in,a,b\n", "columns"),
        ("video_time_s,direction\n1,in,van\n", "columns"),
        ("video_time_s,direction,note\n1\n", "columns"),
    ],
)
def test_parse_flow_labels_errors(text, msg):
    with pytest.raises(ValueError, match=msg):
        parse_flow_labels(text, "x.csv")


def test_labels_csv_round_trip():
    events = [ev(57.94, "out", "van, white"), ev(12.4)]
    text = flow_labels_csv(events)
    assert text.splitlines()[:2] == ["video_time_s,direction,note", "12.4,in,"]
    assert parse_flow_labels(text) == [ev(12.4), ev(57.9, "out", "van, white")]


# --- matching and metrics (vision.md §10) ---


def test_match_within_2_s_same_direction():
    truth = [ev(10), ev(20, "out"), ev(30), ev(40)]
    pred = [ev(11.5), ev(20.5, "in"), ev(32.5), ev(40)]
    r = match_flow(pred, truth)
    assert r.tp == 2 and r.fp == 2 and r.fn == 2
    assert [(t.t, p.t) for t, p in r.matches] == [(10, 11.5), (40, 40)]
    assert [e.t for e in r.false_pos] == [20.5, 32.5]
    assert [e.t for e in r.missed] == [20, 30]
    assert r.accuracy == pytest.approx(2 / 6)
    assert (r.true_in, r.true_out, r.pred_in, r.pred_out) == (3, 1, 4, 0)
    assert r.net_error == (4 - 0) - (3 - 1)
    assert r.mean_offset == pytest.approx(0.75)
    assert r.meets_target is False


def test_closest_pair_wins():
    # the prediction at 10.0 belongs to the truth at 10.5, not the earlier one at 9.0
    r = match_flow([ev(10.0), ev(11.9)], [ev(9.0), ev(10.5)])
    assert [(t.t, p.t) for t, p in r.matches] == [(10.5, 10.0)]
    assert [e.t for e in r.missed] == [9.0] and [e.t for e in r.false_pos] == [11.9]
    # each event is used once: two predictions for one car = one FP
    r = match_flow([ev(5.0), ev(5.2)], [ev(5.1)])
    assert (r.tp, r.fp, r.fn) == (1, 1, 0)


def test_tolerance_bounds_and_targets():
    assert match_flow([ev(12.0)], [ev(10.0)]).tp == 1
    assert match_flow([ev(12.01)], [ev(10.0)]).tp == 0
    assert match_flow([ev(11.0)], [ev(10.0)], tolerance=0.5).tp == 0
    empty = match_flow([], [])
    assert empty.accuracy is None and empty.meets_target is None and empty.net_error == 0
    perfect = match_flow([ev(t) for t in range(50)], [ev(t) for t in range(50)])
    assert perfect.accuracy == 1.0 and perfect.meets_target is True
    one_miss = match_flow([ev(t) for t in range(49)], [ev(t) for t in range(50)])
    assert one_miss.accuracy == pytest.approx(0.98) and one_miss.meets_target is True
    assert one_miss.net_error == -1


def test_report_lines_and_dict():
    r = match_flow([ev(10.4), ev(30, "out")], [ev(10, "in", "van"), ev(50, "out")])
    lines = flow_lines(r)
    assert "TP 1  FP 1  FN 1  event accuracy 33.3%  net error 0" in lines[2]
    assert lines[3].endswith(": missed")
    assert lines[4] == "extra counts (FP): 30.0 s out"
    assert lines[5] == "missed (FN): 50.0 s out"
    d = r.to_dict()
    assert d["tp"] == 1 and d["event_accuracy"] == pytest.approx(1 / 3)
    assert d["matches"] == [{"truth_t": 10.0, "pred_t": 10.4, "direction": "in"}]
    assert d["missed"] == [{"t": 50.0, "direction": "out", "note": ""}]
    json.dumps(d)
    assert "n/a" in flow_lines(match_flow([], []))[3]


# --- the CLI on a generated ramp clip (one car in, one out) ---


def test_cli_evaluate_flow(repo, monkeypatch):  # noqa: F811
    cfg = str(repo / "config/lot.yaml")
    args = ["evaluate-flow", "--video", "ramp.avi", "--camera", "cam-ramp", "--config", cfg]
    runner = CliRunner()
    result = runner.invoke(app, args)
    assert result.exit_code == 1 and "ramp.csv" in result.output  # no tally yet

    labels = repo / "data" / "labels"
    labels.mkdir(parents=True)
    # the car is counted at 7.2 s (in) and 25.7 s (out); a third car was missed
    (labels / "ramp.csv").write_text("video_time_s,direction,note\n7,in,\n25,out,\n30,in,van\n")
    result = runner.invoke(app, args)
    assert result.exit_code == 1 and "model" in result.output

    (repo / "models" / "none").mkdir(parents=True)
    monkeypatch.setattr(tracking, "UltralyticsTracker", lambda *a, **k: BlobBackend())
    video = repo / "out" / "eval-debug.mp4"
    result = runner.invoke(app, [*args, "--debug-video", str(video), "--out", "out/eval"])
    assert result.exit_code == 0, result.output
    assert "TP 2  FP 0  FN 1  event accuracy 66.7%  net error -1" in result.output
    assert "missed (FN): 30.0 s in (van)" in result.output
    assert video.stat().st_size > 0
    pred = parse_flow_labels((repo / "out/eval/flow-ramp-pred.csv").read_text())
    assert [e.direction for e in pred] == ["in", "out"]
    report = json.loads(next((repo / "out/eval").glob("flow-ramp-*.json")).read_text())
    assert report["run"]["frames"] == 400 and report["settings"]["min_track_frames"] == 5

    result = runner.invoke(
        app, [*args, "--json", "--min-track-frames", "500", "--conf", "0.5", "--tolerance", "1"]
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output.strip().splitlines()[-1])
    assert out["tp"] == 0 and out["fn"] == 3  # tracks too short to count
    assert out["settings"]["conf"] == 0.5 and out["settings"]["tolerance_s"] == 1

    result = runner.invoke(app, [*args, "--tolerance", "0"])
    assert result.exit_code != 0
