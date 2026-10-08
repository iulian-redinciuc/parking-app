"""Worker API client (api.md §5.1): one-shot sends, the flow-event outbox, backoff, print mode."""

import io
import json
import threading
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from parking.config import Settings
from parking.messages import CameraHealthMsg, FlowEventMsg, Observation
from parking.workers.api_client import ApiClient

TS = datetime(2026, 10, 7, 17, 5, 12, tzinfo=UTC)
BASE = "http://api.test"


def obs():
    slots = [{"id": "G01", "score": 0.9, "taken": True}]
    return Observation(
        camera_id="cam-ground", ts=TS, frame_size=(64, 48), inference_ms=12, detections=0,
        slots=slots,
    )  # fmt: skip


def health():
    return CameraHealthMsg(camera_id="cam-ground", ts=TS, state="ok")


def flow(i):
    return FlowEventMsg(event_id=f"e{i:04d}", camera_id="cam-ramp", ts=TS, direction="in",
                        track_id=i, cls="car", confidence=0.8)  # fmt: skip


class FakeApi:
    """httpx.MockTransport handler that records requests; `fail` = how many calls fail first."""

    def __init__(self, fail=0, status=503):
        self.fail = fail
        self.status = status
        self.requests: list[httpx.Request] = []
        self.flow_ids: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            if self.fail:
                self.fail -= 1
                if self.status is None:
                    raise httpx.ConnectError("connection refused", request=request)
                return httpx.Response(self.status)
            if request.url.path == "/internal/flow-events":
                events = json.loads(request.content)["events"]
                self.flow_ids += [e["event_id"] for e in events]
                return httpx.Response(200, json={"accepted": len(events), "duplicates": 0})
            return httpx.Response(204)

    @property
    def transport(self):
        return httpx.MockTransport(self)


class RecordingSleep:
    """Records the backoff delays instead of waiting."""

    def __init__(self):
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> bool:
        self.delays.append(seconds)
        return False


def make(api, tmp_path, **kw):
    return ApiClient(BASE, "s3cret", "cam-ramp", outbox_dir=tmp_path, transport=api.transport,
                     **kw)  # fmt: skip


def outbox_ids(path):
    if not path.exists():
        return []
    return [json.loads(line)["event_id"] for line in path.read_text().splitlines()]


# --- observations and health ---


def test_send_observation_posts_json_with_bearer_token(tmp_path):
    api = FakeApi()
    with make(api, tmp_path) as c:
        assert c.send_observation(obs()) is True
    (r,) = api.requests
    assert r.method == "POST"
    assert str(r.url) == f"{BASE}/internal/observations"
    assert r.headers["authorization"] == "Bearer s3cret"
    assert r.headers["content-type"] == "application/json"
    assert json.loads(r.content)["slots"] == [{"id": "G01", "score": 0.9, "taken": True}]


@pytest.mark.parametrize("status", [503, 401, None])
def test_observation_failure_is_dropped_after_one_attempt(tmp_path, status, caplog):
    api = FakeApi(fail=5, status=status)
    with make(api, tmp_path) as c:
        assert c.send_observation(obs()) is False
        assert c.send_health(health()) is False
    assert len(api.requests) == 2  # no retries
    assert "failed, dropped" in caplog.text
    assert "s3cret" not in caplog.text


def test_send_health_posts_to_health(tmp_path):
    api = FakeApi()
    with make(api, tmp_path) as c:
        assert c.send_health(health()) is True
    assert api.requests[0].url.path == "/internal/health"
    assert json.loads(api.requests[0].content)["state"] == "ok"


def test_timeout_is_five_seconds(tmp_path):
    with make(FakeApi(), tmp_path) as c:
        assert c._http.timeout == httpx.Timeout(5.0)


def test_from_settings_reads_url_and_token(tmp_path):
    s = Settings(_env_file=None, api_internal_url=BASE, worker_token=SecretStr("tok"))
    api = FakeApi()
    with ApiClient.from_settings(s, "cam-ground", outbox_dir=tmp_path,
                                 transport=api.transport) as c:  # fmt: skip
        c.send_health(health())
    assert api.requests[0].headers["authorization"] == "Bearer tok"
    assert str(api.requests[0].url).startswith(BASE)


def test_missing_url_needs_print_mode(tmp_path):
    with pytest.raises(ValueError, match="API_INTERNAL_URL"):
        ApiClient(None, "t", "cam-ground", outbox_dir=tmp_path)


# --- outbox ---


def test_flow_events_are_sent_in_batches_of_100_and_removed(tmp_path):
    api = FakeApi()
    with make(api, tmp_path) as c:
        c.queue_flow_events([flow(i) for i in range(250)])
        assert c.flush(5)
        assert c.pending == 0
    sizes = [len(json.loads(r.content)["events"]) for r in api.requests]
    assert sum(sizes) == 250 and max(sizes) <= 100
    assert api.flow_ids == [f"e{i:04d}" for i in range(250)]
    assert not (tmp_path / "cam-ramp.jsonl").exists()


def test_outbox_survives_a_restart(tmp_path):
    down = FakeApi(fail=10**6, status=None)
    c1 = make(down, tmp_path, sleep=lambda s: True)  # gives up after the first failure
    c1.queue_flow_events([flow(1), flow(2)])
    c1.queue_flow_events([flow(3)])
    c1.close()
    assert outbox_ids(tmp_path / "cam-ramp.jsonl") == ["e0001", "e0002", "e0003"]

    up = FakeApi()
    with make(up, tmp_path) as c2:  # nothing queued: it resends what's on disk by itself
        assert c2.flush(5)
    assert up.flow_ids == ["e0001", "e0002", "e0003"]
    assert not (tmp_path / "cam-ramp.jsonl").exists()


def test_outbox_skips_a_torn_line_and_duplicates(tmp_path):
    path = tmp_path / "cam-ramp.jsonl"
    path.write_text(
        flow(1).model_dump_json() + "\n" + flow(1).model_dump_json() + "\n"
        + flow(2).model_dump_json() + "\n" + '{"v": 1, "event_id": "e00'
    )  # fmt: skip
    api = FakeApi()
    with make(api, tmp_path) as c:
        assert c.flush(5)
    assert api.flow_ids == ["e0001", "e0002"]


def test_backoff_doubles_from_1_to_30_s_then_resets(tmp_path):
    api = FakeApi(fail=7)
    sleep = RecordingSleep()
    with make(api, tmp_path, sleep=sleep) as c:
        c.queue_flow_events([flow(1)])
        assert c.flush(5)
        assert sleep.delays == [1, 2, 4, 8, 16, 30, 30]
        api.fail = 2
        c.queue_flow_events([flow(2)])
        assert c.flush(5)
    assert sleep.delays[7:] == [1, 2]  # reset after a success
    assert api.flow_ids == ["e0001", "e0002"]
    assert len(api.requests) == 7 + 1 + 2 + 1


def test_unsent_events_stay_on_disk_while_the_api_is_down(tmp_path):
    api = FakeApi(fail=10**6, status=503)
    gate = threading.Event()

    def sleep(_):
        gate.set()
        return True  # stop retrying, like a shutdown

    c = make(api, tmp_path, sleep=sleep)
    c.queue_flow_events([flow(1)])
    assert gate.wait(5)
    assert c.flush(0.05) is False
    c.close()
    assert outbox_ids(tmp_path / "cam-ramp.jsonl") == ["e0001"]


def test_events_queued_while_sending_are_kept(tmp_path):
    release = threading.Event()
    api = FakeApi()
    inner = api.__call__

    def slow(request):
        release.wait(5)
        return inner(request)

    c = ApiClient(BASE, "t", "cam-ramp", outbox_dir=tmp_path,
                  transport=httpx.MockTransport(slow))  # fmt: skip
    c.queue_flow_events([flow(1)])
    c.queue_flow_events([flow(2)])  # may arrive while batch [1] or [1, 2] is in flight
    release.set()
    assert c.flush(5)
    c.close()
    assert sorted(api.flow_ids) == ["e0001", "e0002"]
    assert len(set(api.flow_ids)) == 2


# --- print mode ---


def test_print_mode_writes_payloads_to_stdout_and_sends_nothing(tmp_path):
    out = io.StringIO()
    with ApiClient(None, None, "cam-ramp", print_mode=True, outbox_dir=tmp_path, out=out) as c:
        assert c.send_observation(obs())
        assert c.send_health(health())
        c.queue_flow_events([flow(i) for i in range(150)])
        assert c.pending == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0]["camera_id"] == "cam-ground" and lines[0]["ts"] == "2026-10-07T17:05:12.000Z"
    assert lines[1]["state"] == "ok"
    assert [len(x["events"]) for x in lines[2:]] == [100, 50]
    assert not list(tmp_path.iterdir())
