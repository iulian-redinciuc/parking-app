"""P6.1: VAPID keys and the push sender with `webpush` mocked (notifications.md §2)."""

import base64
import json
import os
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException
from sqlmodel import Session, select
from typer.testing import CliRunner

from parking.cli import app
from parking.core.clock import FakeClock
from parking.db.engine import make_engine, session_scope, upgrade
from parking.db.models import NotificationLog, PushSubscription
from parking.push.sender import MAX_FAILURES, PushSender, generate_vapid_keys

T0 = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)
PAYLOAD = {
    "title": "Parking: 23 free",
    "body": "Ground 12 · Underground ≈11 · 08:30",
    "tag": "parking-status",
    "url": "https://example.test/#/",
    "level": "plenty",
    "kind": "on_my_way",
}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def browser_keys() -> tuple[str, str]:
    """A subscription's `p256dh` and `auth`, as a browser would create them."""
    point = ec.generate_private_key(ec.SECP256R1()).public_key()
    return b64url(point.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)), b64url(
        os.urandom(16)
    )


@pytest.fixture
def engine(tmp_path):
    url = f"sqlite:///{tmp_path}/db/parking.sqlite"
    upgrade(url)
    engine = make_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def keys():
    return generate_vapid_keys()


def add_subs(engine, n=1, failures=0) -> list[PushSubscription]:
    p256dh, auth = browser_keys()
    with session_scope(engine) as session:
        for i in range(n):
            session.add(
                PushSubscription(
                    id=f"sub-{i:02d}",
                    endpoint=f"https://push.example.test/send/{i}",
                    p256dh=p256dh,
                    auth=auth,
                    prefs={"zones": ["ground"]},
                    tz="Europe/Bucharest",
                    created_at=T0,
                    last_seen_at=T0,
                    failures=failures,
                )
            )
    with Session(engine) as session:  # closed without a commit, so the rows stay loaded
        return list(session.exec(select(PushSubscription).order_by(PushSubscription.id)))


def sub_row(engine, sub_id) -> PushSubscription | None:
    with Session(engine) as session:
        return session.get(PushSubscription, sub_id)


def logs(engine) -> list[NotificationLog]:
    with Session(engine) as session:
        return list(session.exec(select(NotificationLog).order_by(NotificationLog.id)))


def failing(status):
    def webpush(**_kw):
        raise WebPushException("Push failed", response=SimpleNamespace(status_code=status))

    return webpush


def make_sender(engine, keys, webpush, **kw):
    return PushSender(
        engine, keys[1], "mailto:test@example.test", clock=FakeClock(T0), webpush=webpush, **kw
    )


def test_vapid_keys_are_a_p256_pair(keys):
    public, private = keys
    raw_public = b64url_decode(public)
    assert len(raw_public) == 65 and raw_public[0] == 4  # uncompressed point
    assert len(b64url_decode(private)) == 32
    assert "=" not in public + private
    vapid = Vapid.from_string(private)
    derived = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    assert derived == raw_public


def test_cli_prints_env_lines():
    result = CliRunner().invoke(app, ["push", "vapid-keys"])
    assert result.exit_code == 0
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert set(lines) == {"VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"}
    assert len(b64url_decode(lines["VAPID_PUBLIC_KEY"])) == 65
    assert "back them up" in result.stderr


def test_success_sends_and_logs(engine, keys):
    calls = []

    def webpush(**kw):
        calls.append(kw)
        return SimpleNamespace(status_code=201)

    [sub] = add_subs(engine, failures=2)
    result = make_sender(engine, keys, webpush).send(sub, PAYLOAD, urgency="high")

    assert result.ok and result.status_code == 201 and not result.deleted
    [call] = calls
    assert call["subscription_info"] == {
        "endpoint": sub.endpoint,
        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
    }
    assert json.loads(call["data"]) == PAYLOAD
    assert call["ttl"] == 600
    assert call["headers"] == {"Urgency": "high"}
    assert call["vapid_claims"] == {"sub": "mailto:test@example.test"}
    assert isinstance(call["vapid_private_key"], Vapid)
    row = sub_row(engine, sub.id)
    assert row.failures == 0 and row.last_sent_at == T0
    [entry] = logs(engine)
    assert (entry.subscription_id, entry.kind, entry.status, entry.error) == (
        sub.id,
        "on_my_way",
        "sent",
        None,
    )
    assert entry.payload == PAYLOAD and entry.ts == T0


@pytest.mark.parametrize("status", [404, 410])
def test_gone_deletes_the_subscription(engine, keys, status):
    [sub] = add_subs(engine)
    result = make_sender(engine, keys, failing(status)).send(sub, PAYLOAD)

    assert not result.ok and result.deleted and result.status_code == status
    assert sub_row(engine, sub.id) is None
    [entry] = logs(engine)
    assert entry.status == "failed" and entry.error.startswith(f"{status}: ")


def test_server_error_increments_failures(engine, keys):
    [sub] = add_subs(engine)
    result = make_sender(engine, keys, failing(500)).send(sub, PAYLOAD)

    assert not result.ok and not result.deleted and result.status_code == 500
    assert sub_row(engine, sub.id).failures == 1
    assert [e.status for e in logs(engine)] == ["failed"]


def test_fifth_failure_in_a_row_deletes(engine, keys):
    [sub] = add_subs(engine, failures=MAX_FAILURES - 2)
    sender = make_sender(engine, keys, failing(500))

    assert not sender.send(sub, PAYLOAD).deleted
    assert sub_row(engine, sub.id).failures == MAX_FAILURES - 1
    assert sender.send(sub, PAYLOAD).deleted
    assert sub_row(engine, sub.id) is None
    assert len(logs(engine)) == 2


def test_network_error_counts_as_a_failure(engine, keys):
    def webpush(**_kw):
        raise ConnectionError("no route to host")

    [sub] = add_subs(engine)
    result = make_sender(engine, keys, webpush).send(sub, PAYLOAD)

    assert not result.ok and result.status_code is None and "no route" in result.error
    assert sub_row(engine, sub.id).failures == 1


def test_send_many_uses_at_most_ten_threads(engine, keys):
    lock, active, peak = threading.Lock(), [0], [0]

    def webpush(subscription_info, **_kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        if subscription_info["endpoint"].endswith("/3"):
            raise WebPushException("gone", response=SimpleNamespace(status_code=410))
        return SimpleNamespace(status_code=201)

    subs = add_subs(engine, n=25)
    results = make_sender(engine, keys, webpush).send_many(subs, PAYLOAD)

    assert [r.subscription_id for r in results] == [s.id for s in subs]
    assert [r.ok for r in results].count(False) == 1 and results[3].deleted
    assert 1 < peak[0] <= 10
    assert len(logs(engine)) == 25
    assert make_sender(engine, keys, webpush).send_many([], PAYLOAD) == []


def test_real_webpush_encrypts_and_signs(engine, keys, monkeypatch):
    """pywebpush itself, with only the HTTP POST replaced: checks our key format and headers
    are what it (and a push service) expects."""
    posted = {}

    def fake_post(url, data, headers, timeout):
        posted.update(url=url, data=data, headers=headers)
        response = requests.Response()
        response.status_code = 201
        return response

    monkeypatch.setattr("requests.post", fake_post)
    [sub] = add_subs(engine)
    sender = PushSender(engine, keys[1], "mailto:test@example.test", clock=FakeClock(T0))

    assert sender.send(sub, PAYLOAD, urgency="high").ok
    assert posted["url"] == sub.endpoint
    headers = {k.lower(): v for k, v in posted["headers"].items()}
    assert headers["ttl"] == "600" and headers["urgency"] == "high"
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["authorization"].startswith("vapid t=")
    assert f"k={keys[0]}" in headers["authorization"]
    assert PAYLOAD["title"].encode() not in posted["data"]  # encrypted


def test_missing_keys_fail_early(engine):
    with pytest.raises(ValueError, match="VAPID_PRIVATE_KEY"):
        PushSender(engine, "", "mailto:test@example.test")
    with pytest.raises(ValueError, match="VAPID_SUBJECT"):
        PushSender(engine, generate_vapid_keys()[1], "")
