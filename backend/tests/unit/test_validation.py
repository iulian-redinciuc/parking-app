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
