import math

import cv2
import numpy as np
import pytest

from parking.config import Camera, SlotFile
from parking.vision import benchmark as bm


def test_cases_cover_every_combination():
    cs = bm.cases(["pytorch", "ncnn"], [640, 1280], ["yolo11n", "yolo11n-seg"])
    assert len(cs) == 8
    assert cs[0] == bm.Case("pytorch", 640, "yolo11n")
    assert cs[-1] == bm.Case("ncnn", 1280, "yolo11n-seg")
    assert cs[-1].name == "yolo11n-seg ncnn @ 1280"
    assert bm.Case("appearance").name == "appearance"


@pytest.mark.parametrize(("q", "expected"), [(50, 10), (95, 19), (100, 20), (0, 1)])
def test_percentile_nearest_rank(q, expected):
    assert bm.percentile(list(range(20, 0, -1)), q) == expected


def test_percentile_needs_values():
    with pytest.raises(ValueError):
        bm.percentile([], 95)


def test_time_runs_warms_up_then_times():
    calls = []
    times, last = bm.time_runs(lambda: calls.append(1) or len(calls), runs=4, warmup=3)
    assert len(calls) == 7
    assert len(times) == 4 and all(t >= 0 for t in times)
    assert last == 7


def test_model_path(tmp_path, monkeypatch):
    assert bm.model_path(bm.Case("pytorch", 640, "yolo11n"), tmp_path) == tmp_path / "yolo11n.pt"
    ready = tmp_path / "bench" / "yolo11n-640" / "yolo11n_ncnn_model"
    ready.mkdir(parents=True)
    assert bm.model_path(bm.Case("ncnn", 640, "yolo11n"), tmp_path) == ready
    with pytest.raises(ValueError):
        bm.model_path(bm.Case("onnx", 640, "yolo11n"), tmp_path)


def test_ncnn_export_reuses_local_weights(tmp_path, monkeypatch):
    (tmp_path / "yolo11n.pt").write_bytes(b"weights")
    seen = {}

    def fake_export(model, imgsz, runtime, out_dir):
        seen.update(model=model, imgsz=imgsz, runtime=runtime, out=out_dir)
        assert (out_dir / "yolo11n.pt").read_bytes() == b"weights"
        return out_dir / f"{model}_ncnn_model"

    monkeypatch.setattr("parking.vision.detector.export_model", fake_export)
    path = bm.ncnn_model(tmp_path, "yolo11n", 1280)
    assert seen == {
        "model": "yolo11n",
        "imgsz": 1280,
        "runtime": "ncnn",
        "out": tmp_path / "bench" / "yolo11n-1280",
    }
    assert path == tmp_path / "bench" / "yolo11n-1280" / "yolo11n_ncnn_model"


def _appearance_setup():
    cam = Camera.model_validate(
        {
            "id": "cam-ground",
            "role": "occupancy",
            "zones": ["ground"],
            "source": "file:x.jpg",
            "slots_file": "s.json",
            "detector": {"model": "models/none"},
            "occupancy": {"method": "appearance"},
        }
    )
    slots = SlotFile.model_validate(
        {
            "version": 1,
            "camera_id": "cam-ground",
            "image_size": [200, 100],
            "slots": [
                {
                    "id": "G01",
                    "zone": "ground",
                    "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
                },
                {
                    "id": "G02",
                    "zone": "ground",
                    "polygon": [[100, 0], [200, 0], [200, 100], [100, 100]],
                },
            ],
        }
    )
    return cam, slots, None


def test_run_case_appearance(tmp_path):
    img = tmp_path / "img.jpg"
    cv2.imwrite(str(img), np.full((100, 200, 3), 90, np.uint8))
    r = bm.run_case(bm.Case("appearance"), str(img), 3, 1, _appearance_setup())
    assert r.runs == 3 and r.error is None and r.detections is None
    assert 0 <= r.median_ms <= r.p95_ms
    assert r.peak_rss_mb > 10


def test_run_isolated_reports_errors(tmp_path):
    r = bm.run_isolated(bm.Case("onnx", 640, "yolo11n"), str(tmp_path / "x.jpg"), 1, 0, "models")
    assert r.runs == 0 and math.isnan(r.median_ms)
    assert "unknown runtime onnx" in r.error


def test_table_and_json():
    ok = bm.CaseResult(bm.Case("ncnn", 640, "yolo11n"), 20, 81.6, 90.2, 541.9, detections=1)
    app = bm.CaseResult(bm.Case("appearance"), 20, 153.0, 160.0, 142.0)
    nan = float("nan")
    bad = bm.CaseResult(bm.Case("pytorch", 1280, "yolo11n"), 0, nan, nan, nan, error="Boom: x")
    rows = bm.table([ok, app, bad])
    assert rows[2] == "| yolo11n | ncnn | 640 | 82 | 90 | 542 | 1 |"
    assert rows[3] == "| appearance scorer | appearance | full frame | 153 | 160 | 142 | - |"
    assert "error: Boom: x" in rows[4]
    data = bm.to_json([ok], machine="pi")
    assert data["machine"] == "pi"
    assert data["results"][0]["name"] == "yolo11n ncnn @ 640"
    assert data["results"][0]["case"]["imgsz"] == 640


def test_cpu_temp_is_a_number_or_none():
    t = bm.cpu_temp()
    assert t is None or 0 < t < 120
