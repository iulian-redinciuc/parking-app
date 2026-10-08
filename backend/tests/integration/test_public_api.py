"""P2.9: public REST endpoints, languages, CORS, gzip, error format, rate limits and `/docs`,
driven with httpx `AsyncClient`: in-process (ASGI transport + lifespan) for REST, and a real
uvicorn server for the SSE stream (the ASGI transport buffers whole responses)."""

import contextlib
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from parking.api.app import create_app
from parking.config import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLOTS = ["G01", "G02", "G03"]
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]

LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 1.5, lon: 2.5}, timezone: Europe/Bucharest}
zones:
  - {id: ground, name: {en: Ground, ro: Parter}, method: slots}
  - {id: underground, name: {en: Underground, ro: Subsol}, method: flow, capacity: 60}
cameras:
  - id: cam-ground
    role: occupancy
    zones: [ground]
    source: "file:x.jpg"
    slots_file: config/slots/cam-ground.json
    detector: {model: models/x}
    smoothing: {consistent_readings: 1}
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "file:y.jpg"
    lines_file: config/lines/cam-ramp.json
    detector: {model: models/x}
api: {sse_ping_s: 1}
"""


def obs(taken):
    return {
        "v": 1,
        "camera_id": "cam-ground",
        "ts": "2026-10-09T08:00:00.000Z",
        "frame_size": [10, 10],
        "inference_ms": 5,
        "detections": 0,
        "slots": [{"id": s, "score": 0.5, "taken": s in taken} for s in SLOTS],
    }


@pytest.fixture
def lot(tmp_path):
    (tmp_path / "config" / "slots").mkdir(parents=True)
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    slots = [{"id": s, "zone": "ground", "polygon": SQUARE} for s in SLOTS]
    slot_file = {"version": 1, "camera_id": "cam-ground", "image_size": [10, 10], "slots": slots}
    (tmp_path / "config" / "slots" / "cam-ground.json").write_text(json.dumps(slot_file))
    return tmp_path


def make_app(lot, **settings):
    settings = Settings(_env_file=None, worker_token=TOKEN, **settings)
    return create_app(
        lot / "config" / "lot.yaml",
        settings,
        db_url=f"sqlite:///{lot}/parking.sqlite",
        tick=False,
    )


@contextlib.asynccontextmanager
async def client_for(app, **kwargs):
    transport = httpx.ASGITransport(app=app, **kwargs)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


async def test_status_503_until_first_observation_then_lot_status(lot):
    async with client_for(make_app(lot)) as client:
        r = await client.get("/api/status")
        assert r.status_code == 503
        assert r.json() == {
            "error": {"code": "unavailable", "message": "no data received yet since start-up"}
        }

        r = await client.post("/internal/observations", json=obs({"G01"}), headers=AUTH)
        assert r.status_code == 204
        r = await client.get("/api/status")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache"
        status = r.json()
        assert status["v"] == 1 and status["lot"] == "main"
        ground, under = status["zones"]
        assert (ground["name"], ground["occupied"], ground["free"]) == ("Ground", 1, 2)
        assert ground["slots"] == {"G01": True, "G02": False, "G03": False}
        assert ground["stale"] is False and ground["updated_at"].endswith("Z")
        assert under["slots"] is None and under["updated_at"] is None and under["stale"] is True
        assert status["total"]["capacity"] == 63 and status["total"]["stale"] is True


async def test_status_restored_from_db_is_200_and_stale(lot):
    async with client_for(make_app(lot)) as client:
        await client.post("/internal/observations", json=obs({"G01", "G02"}), headers=AUTH)
    async with client_for(make_app(lot)) as client:  # restart, no new data
        r = await client.get("/api/status")
        assert r.status_code == 200
        ground = r.json()["zones"][0]
        assert (ground["occupied"], ground["stale"]) == (2, True)


@pytest.mark.parametrize(
    ("query", "header", "expected"),
    [
        ("", None, "Ground"),
        ("?lang=ro", None, "Parter"),
        ("?lang=RO-ro", "en", "Parter"),
        ("", "ro-RO,ro;q=0.9,en;q=0.8", "Parter"),
        ("", "de-DE,en;q=0.5,ro;q=0.4", "Ground"),
        ("", "de, ro;q=0.3", "Parter"),
        ("", "ro;q=0, en", "Ground"),
        ("?lang=fr", "ro", "Parter"),  # not available: next candidate
        ("?lang=fr", None, "Ground"),
    ],
)
async def test_zone_names_follow_lang_then_accept_language(lot, query, header, expected):
    headers = {"Accept-Language": header} if header else {}
    async with client_for(make_app(lot)) as client:
        await client.post("/internal/observations", json=obs(set()), headers=AUTH)
        r = await client.get(f"/api/status{query}", headers=headers)
        assert r.json()["zones"][0]["name"] == expected
        assert r.headers["vary"] == "Accept-Language"
        r = await client.get(f"/api/lot{query}", headers=headers)
        assert r.json()["zones"][0]["name"] == expected


async def test_lot_info(lot):
    async with client_for(make_app(lot)) as client:
        assert (await client.get("/api/lot")).json() == {
            "v": 1,
            "id": "main",
            "name": "Parking",
            "location": {"lat": 1.5, "lon": 2.5},
            "notify_radius_m": 500,
            "timezone": "Europe/Bucharest",
            "zones": [
                {"id": "ground", "name": "Ground", "method": "slots", "capacity": 3},
                {"id": "underground", "name": "Underground", "method": "flow", "capacity": 60},
            ],
            "levels": {"plenty": 0.2, "filling": 0.05},
        }
    # LOT_LAT / LOT_LON from the settings win over lot.yaml
    async with client_for(make_app(lot, lot_lat=51.5, lot_lon=-0.12)) as client:
        assert (await client.get("/api/lot")).json()["location"] == {"lat": 51.5, "lon": -0.12}


async def test_errors_use_the_error_format(lot):
    async with client_for(make_app(lot)) as client:
        r = await client.get("/nope")
        assert r.status_code == 404
        assert r.json() == {"error": {"code": "not_found", "message": "Not Found"}}
        r = await client.post("/api/status")
        assert r.status_code == 405
        assert r.json()["error"]["code"] == "method_not_allowed"
        r = await client.get("/api/status", params={"lang": "x" * 36})
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "bad_request" and err["message"].startswith("query.lang:")
        assert err["details"][0]["loc"] == ["query", "lang"]
        r = await client.post("/internal/health", json={})
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


async def test_unexpected_error_is_500_internal(lot):
    app = make_app(lot)
    async with client_for(app, raise_app_exceptions=False) as client:
        await client.post("/internal/observations", json=obs(set()), headers=AUTH)

        def boom(lang="en"):
            raise RuntimeError("boom")

        app.state.runtime.store.status = boom
        r = await client.get("/api/status")
        assert r.status_code == 500
        assert r.json() == {"error": {"code": "internal", "message": "internal server error"}}


async def test_public_rate_limit_per_ip(lot):
    async with client_for(make_app(lot)) as client:
        for _ in range(120):
            assert (await client.get("/api/lot")).status_code == 200
        r = await client.get("/healthz")  # the limit is shared by all public GETs
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "rate_limited"
        assert 1 <= int(r.headers["retry-after"]) <= 60
        # the workers' internal routes have no rate limit
        r = await client.post("/internal/observations", json=obs(set()), headers=AUTH)
        assert r.status_code == 204


async def test_cors_only_for_configured_origins(lot):
    app = make_app(lot, cors_origins="https://a.example, https://b.example")
    async with client_for(app) as client:
        r = await client.get("/api/lot", headers={"Origin": "https://b.example"})
        assert r.headers["access-control-allow-origin"] == "https://b.example"
        r = await client.get("/api/lot", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers
        preflight = {
            "Origin": "https://a.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        }
        r = await client.options("/api/status", headers=preflight)
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://a.example"
        assert "PATCH" in r.headers["access-control-allow-methods"]
        allowed = r.headers["access-control-allow-headers"].lower()
        assert "authorization" in allowed and "content-type" in allowed
    async with client_for(make_app(lot)) as client:  # no CORS_ORIGINS: no CORS at all
        r = await client.get("/api/lot", headers={"Origin": "https://a.example"})
        assert "access-control-allow-origin" not in r.headers


async def test_docs_only_in_debug_and_gzip_over_1kb(lot):
    async with client_for(make_app(lot)) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
        r = await client.get("/api/lot", headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in r.headers  # under 1 KB
    async with client_for(make_app(lot, log_level="DEBUG")) as client:
        assert (await client.get("/docs")).status_code == 200
        r = await client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
        assert r.headers["content-encoding"] == "gzip"
        paths = r.json()["paths"]
        for path in ("/healthz", "/api/lot", "/api/status", "/api/stream", "/internal/health"):
            assert path in paths


@pytest.fixture
def server(lot):
    app = make_app(lot, log_level="DEBUG")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "uvicorn didn't start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(10)


async def next_status(lines):
    """The `data` of the next `status` event from an `aiter_lines()` iterator."""
    event = None
    async for line in lines:
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event == "status":
            return json.loads(line[6:])
    raise AssertionError("stream ended")


async def test_ingest_then_status_and_stream_in_two_languages(server):
    async with httpx.AsyncClient(base_url=server, timeout=5) as client:
        r = await client.post("/internal/observations", json=obs({"G01"}), headers=AUTH)
        assert r.status_code == 204
        status = (await client.get("/api/status")).json()
        async with (
            client.stream("GET", "/api/stream", headers={"Accept-Encoding": "gzip"}) as en,
            client.stream("GET", "/api/stream?lang=ro") as ro,
        ):
            assert "content-encoding" not in en.headers  # SSE is never compressed
            en_lines, ro_lines = en.aiter_lines(), ro.aiter_lines()
            first_en, first_ro = await next_status(en_lines), await next_status(ro_lines)
            assert first_en == status
            assert [z["name"] for z in first_ro["zones"]] == ["Parter", "Subsol"]
            assert first_ro["zones"][0]["free"] == first_en["zones"][0]["free"] == 2

            await client.post("/internal/observations", json=obs({"G01", "G02"}), headers=AUTH)
            new_en, new_ro = await next_status(en_lines), await next_status(ro_lines)
            assert new_en["zones"][0]["free"] == new_ro["zones"][0]["free"] == 1
            assert new_en["zones"][0]["name"] == "Ground" and new_ro["zones"][0]["name"] == "Parter"
            assert new_en == (await client.get("/api/status")).json()
