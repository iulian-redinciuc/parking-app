"""P2.11: end-to-end, in Docker. Worker (replay folder + fake detector) → API → `/api/stream`.

Starts `deploy/docker-compose.test.yml` (project `parking-e2e`), waits for the first live
status, swaps the replayed frame for one whose sidecar has different detections and expects
the new free count on the stream within `3 × interval + 5 s` (3 = the fixture's
`consistent_readings`). Skipped unless `PARKING_E2E=1` (needs Docker; CI job `e2e`):

    cd backend && PARKING_E2E=1 uv run pytest tests/e2e -v
"""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("PARKING_E2E") != "1", reason="set PARKING_E2E=1 (Docker)"),
]

REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "deploy" / "docker-compose.test.yml"
FRAMES = REPO / "backend" / "tests" / "fixtures" / "replay" / "frames"
INTERVAL_S = 2  # the fixture camera's `folder:...?interval=2`
READINGS = 3  # its `smoothing.consistent_readings`
FIRST_FREE = 3  # frame-01.json: 1 of 4 spaces taken
NEXT_FREE = 1  # frame-02.json: 3 of 4 spaces taken


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def put_frame(replay: Path, name: str) -> None:
    """Sidecar first, then the image; each written under a temp name and renamed, so the
    worker never sees a half-copied file."""
    for suffix in (".json", ".png"):
        tmp = replay / f".{name}{suffix}.part"
        shutil.copyfile(FRAMES / f"{name}{suffix}", tmp)
        tmp.chmod(0o644)  # the container user (uid 1000) may differ from the host user
        tmp.rename(replay / f"{name}{suffix}")


def remove_frame(replay: Path, name: str) -> None:
    for suffix in (".png", ".json"):
        (replay / f"{name}{suffix}").unlink()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    replay = tmp_path_factory.mktemp("replay")
    replay.chmod(0o755)
    put_frame(replay, "frame-01")
    port = free_port()
    env = {**os.environ, "E2E_REPLAY_DIR": str(replay), "E2E_API_PORT": str(port)}

    def compose(*args: str, check: bool = True, timeout: float = 600):
        cmd = ["docker", "compose", "-f", str(COMPOSE), *args]
        return subprocess.run(cmd, env=env, check=check, timeout=timeout)

    try:
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "120")
        yield f"http://127.0.0.1:{port}", replay
    finally:
        compose("logs", "--no-color", "--tail", "100", check=False, timeout=60)
        compose("down", "-v", "--rmi", "all", "--timeout", "10", check=False, timeout=120)


def ground(status: dict) -> dict:
    return next(z for z in status["zones"] if z["id"] == "ground")


def wait_for_free(lines, free: int, deadline: float) -> dict:
    """Read status events until the ground zone shows `free`, live (not stale)."""
    seen = []
    for line in lines:
        if line.startswith("data:") and line[5:].strip():  # pings have an empty `data:`
            zone = ground(json.loads(line[5:]))
            seen.append((zone["free"], zone["stale"]))
            if zone["free"] == free and not zone["stale"]:
                return zone
        if time.monotonic() > deadline:  # pings every 5 s keep this loop turning
            break
    pytest.fail(f"no live free={free} in time; saw (free, stale): {seen}")


def test_new_frame_reaches_the_stream(stack):
    base, replay = stack
    timeout = httpx.Timeout(10, read=15)  # > sse_ping_s (5) in the fixture lot.yaml
    with httpx.stream("GET", f"{base}/api/stream", timeout=timeout) as stream:
        assert stream.status_code == 200
        lines = stream.iter_lines()
        first = wait_for_free(lines, FIRST_FREE, time.monotonic() + 60)
        assert first["capacity"] == 4

        start = time.monotonic()
        put_frame(replay, "frame-02")
        remove_frame(replay, "frame-01")
        budget = READINGS * INTERVAL_S + 5
        zone = wait_for_free(lines, NEXT_FREE, start + budget)
        elapsed = time.monotonic() - start

    assert zone["occupied"] == 3
    assert elapsed <= budget
    print(f"new count on the stream after {elapsed:.1f} s (budget {budget} s)")
