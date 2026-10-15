import json
from collections import Counter

from typer.testing import CliRunner

from parking.cli import app
from parking.config import LabelFile
from parking.vision import validation as va

from .test_cli import _write_lot

runner = CliRunner()


def _capture(root, day, hhmmss, taken, total=10, reasons="periodic", meta=True):
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    jpg = folder / f"{hhmmss}_000-{reasons}.jpg"
    jpg.write_bytes(b"jpeg")
    if meta:
        slots = [{"id": f"G{i:02d}", "score": 0.5, "taken": i < taken} for i in range(total)]
        body = {"camera_id": "cam-ground", "observation": {"slots": slots}}
        jpg.with_suffix(".json").write_text(json.dumps(body))
    return jpg


def test_period_and_level():
    assert [va.period(h) for h in (0, 5, 6, 10, 11, 14, 15, 20, 21, 23)] == [
        "night", "night", "morning", "morning", "noon", "noon",
        "evening", "evening", "night", "night",
    ]  # fmt: skip
    assert [va.level(t, 10) for t in (0, 2, 3, 8, 9, 10)] == [
        "empty", "empty", "busy", "busy", "full", "full",
    ]  # fmt: skip
    assert va.level(0, 0) == "unknown"


def test_scan_reads_time_and_level_and_skips_bad_files(tmp_path):
    _capture(tmp_path, "2026-10-01", "071500", taken=10, reasons="periodic+flip")
    _capture(tmp_path, "2026-10-01", "230000", taken=0)
    _capture(tmp_path, "2026-10-01", "120000", taken=5, meta=False)  # no JSON
    (tmp_path / "2026-10-01" / "notes.jpg").write_bytes(b"x")  # not a capture name
    bad = _capture(tmp_path, "2026-10-02", "120000", taken=5)
    bad.with_suffix(".json").write_text("{")
    (tmp_path / "other").mkdir()
    caps = va.scan(tmp_path)
    assert [(c.out_name, c.period, c.level) for c in caps] == [
        ("2026-10-01_071500_000.jpg", "morning", "full"),
        ("2026-10-01_230000_000.jpg", "night", "empty"),
    ]


def test_pick_balances_strata_and_spreads_in_time(tmp_path):
    # 7 days: every 10 min at noon (busy) = lots, a few at night (empty), one evening full
    for d in range(1, 8):
        day = f"2026-10-0{d}"
        for m in range(0, 240, 10):
            _capture(tmp_path, day, f"{11 + m // 60:02d}{m % 60:02d}00", taken=5)
        _capture(tmp_path, day, "020000", taken=0)
    _capture(tmp_path, "2026-10-03", "180000", taken=10)
    caps = va.scan(tmp_path)
    chosen = va.pick(caps, 20)
    strata = Counter(c.stratum for c in chosen)
    assert strata == {("noon", "busy"): 12, ("night", "empty"): 7, ("evening", "full"): 1}
    noon_days = {c.local.day for c in chosen if c.period == "noon"}
    assert len(noon_days) >= 6  # spread over the week, not the first 12 captures
    assert chosen == sorted(chosen, key=lambda c: c.local)
    assert len(va.pick(caps, 10_000)) == len(caps)


def _labels(images):
    return LabelFile.model_validate({"version": 1, "camera_id": "cam-ground", "images": images})


def test_coverage():
    images = {f"{i:03d}.jpg": {"conditions": ["noon", "dry", "noon"]} for i in range(200)}
    for i in range(15):
        images[f"{i:03d}.jpg"]["conditions"] = ["night", "rain", "snow"]
    cov = va.coverage(_labels(images), ["noon", "night", "rain", "full"])
    assert cov.images == 200
    assert cov.tags == {"noon": 185, "night": 15, "rain": 15, "full": 0, "dry": 185, "snow": 15}
    assert cov.missing == {"full": 0}
    assert not cov.ok
    assert va.coverage(_labels(images), ["noon", "night"]).ok
    assert not va.coverage(_labels(images), ["noon"], min_images=201).ok


def test_cli_pick_and_check(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    debug = tmp_path / "data" / "debug" / "cam-ground"
    for h in range(24):
        _capture(debug, "2026-10-01", f"{h:02d}0000", taken=h % 3 * 5)
    args = ["validation", "pick", "--camera", "cam-ground", "--count", "6"]
    result = runner.invoke(app, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "6 of 24 capture(s) picked, would copy 6, 0 already there" in result.output
    assert not (tmp_path / "data" / "validation").exists()

    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    out = sorted(p.name for p in (tmp_path / "data" / "validation" / "cam-ground").iterdir())
    assert len(out) == 6 and all(n.startswith("2026-10-01_") for n in out)
    result = runner.invoke(app, args)
    assert "copied 0, 6 already there" in result.output

    labels = tmp_path / "data" / "labels" / "cam-ground-validation.json"
    labels.parent.mkdir(parents=True)
    images = {n: {"conditions": ["noon"], "taken": ["G01"]} for n in out}
    labels.write_text(json.dumps({"version": 1, "camera_id": "cam-ground", "images": images}))
    check = ["validation", "check", "--camera", "cam-ground"]
    result = runner.invoke(app, check)
    assert result.exit_code == 1
    assert "images      6 / 200" in result.output and "night       0  <- short" in result.output
    result = runner.invoke(
        app, [*check, "--tags", "noon", "--min-images", "6", "--min-per-tag", "6"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.rstrip().endswith("ok")


def test_cli_pick_without_captures(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    result = runner.invoke(app, ["validation", "pick", "--camera", "cam-ground"])
    assert result.exit_code == 1
    assert "no captures" in result.output


def _slot(i, taken):
    from types import SimpleNamespace

    return SimpleNamespace(id=f"G{i:02d}", score=0.9 if taken else 0.1, taken=taken)


def test_timelapse_makes_worker_captures_with_truth(tmp_path):
    from datetime import datetime
    from types import SimpleNamespace

    import cv2
    import numpy as np

    from parking.vision.health import FrameHealth
    from parking.workers import timelapse as tl

    feed = tmp_path / "feed"
    feed.mkdir()
    rng = np.random.default_rng(0)
    for n in ("a.jpg", "b.jpg"):  # textured, so night IR frames aren't "blurry"
        cv2.imwrite(str(feed / n), rng.integers(60, 200, (60, 80, 3), np.uint8))
    truth_in = _labels({"a.jpg": {"taken": ["G00"]}, "b.jpg": {"taken": ["G00", "G01"]}})

    def analyse(image):  # always guesses "G00 taken": right on a.jpg, one slot off on b.jpg
        slots = [_slot(0, True), _slot(1, False)]
        return SimpleNamespace(
            camera_id="cam-ground", frame_size=(80, 60), inference_ms=5.0, detections=[],
            slots=slots, zone_counts={},
        )  # fmt: skip

    files = sorted(feed.iterdir())
    res = tl.run(
        files, truth_in, analyse, FrameHealth(check_frozen=False), root=tmp_path,
        camera_id="cam-ground", lot_id="main", tz="Europe/Bucharest",
        start=datetime(2026, 10, 1), days=2, every_min=30, capture_every_min=60,
    )  # fmt: skip
    assert res.frames == 96 and not res.unhealthy
    assert res.saved == 48  # every hour (= a.jpg's turn), no flips: the guess never changes
    assert res.slots == 96 and res.slot_errors == 0
    caps = va.scan(tmp_path / "data" / "debug" / "cam-ground")
    assert len(caps) == 48 and caps[0].out_name == "2026-10-01_000000_000.jpg"
    assert {c.out_name for c in caps} == set(res.truth)
    tags = res.truth["2026-10-01_000000_000.jpg"].conditions
    assert tags[0] == "night" and tags[-1] == "simulated" and tags[2] == "busy"
    res = tl.run(
        files[1:], truth_in, analyse, FrameHealth(check_frozen=False), root=tmp_path / "b",
        camera_id="cam-ground", lot_id="main", tz="UTC", start=datetime(2026, 10, 1),
        days=1, every_min=60,
    )  # fmt: skip
    assert res.saved == 24 and res.slot_errors == 24  # G01 missed on every b.jpg frame
    assert res.truth["2026-10-01_120000_000.jpg"].conditions[:3] == ["noon", "dry", "full"]
    assert tl.rain_spells(14, 1) == tl.rain_spells(14, 1) != tl.rain_spells(14, 2)
    assert all(0 <= a < b <= 14 * 24 + 5 for a, b in tl.rain_spells(14, 1))


def test_cli_pick_labels_from_truth_keeps_existing_labels(tmp_path, monkeypatch):
    _write_lot(tmp_path, monkeypatch)
    debug = tmp_path / "data" / "debug" / "cam-ground"
    for h in range(4):
        _capture(debug, "2026-10-01", f"{h * 6:02d}0000", taken=h)
    names = [f"2026-10-01_{h * 6:02d}0000_000.jpg" for h in range(4)]
    truth = tmp_path / "truth.json"
    truth.write_text(
        _labels({n: {"conditions": ["night"], "taken": ["G01"]} for n in names}).model_dump_json()
    )
    labels = tmp_path / "data" / "labels" / "cam-ground-validation.json"
    labels.parent.mkdir(parents=True)
    labels.write_text(_labels({names[0]: {"conditions": ["fixed"]}}).model_dump_json())
    args = ["validation", "pick", "--camera", "cam-ground", "--labels-from", str(truth)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "3 label(s) added" in result.output and "(4 in all)" in result.output
    images = LabelFile.model_validate_json(labels.read_text()).images
    assert images[names[0]].conditions == ["fixed"]  # a hand-made label wins
    assert images[names[1]].taken == ["G01"]

    truth.write_text(_labels({}).model_dump_json())
    result = runner.invoke(app, args)
    assert result.exit_code == 1 and "not in" in result.output


def test_blur_boxes_and_cli_manual_boxes_once(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    from parking.vision import blur as bl

    assert bl.grow((10, 10, 30, 50), (40, 100, 3)) == (7, 4, 33, 40)
    _write_lot(tmp_path, monkeypatch)
    folder = tmp_path / "data" / "validation" / "cam-ground"
    folder.mkdir(parents=True)
    img = np.random.default_rng(1).integers(0, 255, (100, 200, 3), np.uint8)
    cv2.imwrite(str(folder / "a.png"), img)
    cv2.imwrite(str(folder / "b.png"), img)
    boxes = tmp_path / "plates.json"
    boxes.write_text('{"a.png": [[20, 20, 60, 40]]}')
    args = ["validation", "blur", "--camera", "cam-ground", "--no-detect", "--boxes", str(boxes)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "1 hand-drawn box(es); blurred 1" in result.output
    out = cv2.imread(str(folder / "a.png"))
    assert out[22:38, 22:58].std() < img[22:38, 22:58].std() / 3  # blurred inside
    assert (out[80:, 150:] == img[80:, 150:]).all()  # untouched outside
    assert (cv2.imread(str(folder / "b.png")) == img).all()
    log = bl.load_log(folder)
    assert log == {"a.png": [{"by": "manual", "box": [20, 20, 60, 40]}]}
    result = runner.invoke(app, args)  # nothing new: not blurred twice
    assert "blurred 0" in result.output and (cv2.imread(str(folder / "a.png")) == out).all()
    boxes.write_text('{"a.png": [[1, 2, 3]]}')
    assert runner.invoke(app, args).exit_code == 1
