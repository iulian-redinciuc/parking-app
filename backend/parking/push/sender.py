"""pywebpush wrapper with expiry cleanup (notifications.md §2).

Every attempt writes a `notification_log` row. 404/410 from the push service means the browser
dropped the subscription, so it's deleted at once; any other error counts as a failure and the
subscription is deleted after `MAX_FAILURES` in a row. A success resets the count.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

import pywebpush
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from sqlalchemy import Engine

from parking.core.clock import Clock, SystemClock
from parking.db.engine import session_scope
from parking.db.models import NotificationLog, PushSubscription

log = logging.getLogger(__name__)

TTL_S = 600  # a "12 free" message is worthless an hour later
MAX_FAILURES = 5
MAX_WORKERS = 10  # pywebpush is blocking
TIMEOUT_S = 10
GONE = (404, 410)
ERROR_MAX_CHARS = 500

Urgency = Literal["very-low", "low", "normal", "high"]


@dataclass(frozen=True)
class SendResult:
    subscription_id: str
    ok: bool
    status_code: int | None = None  # the push service's HTTP status, if it answered
    deleted: bool = False  # the subscription was removed (404/410 or too many failures)
    error: str | None = None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_vapid_keys() -> tuple[str, str]:
    """A new P-256 pair as `(public, private)`: the public key is the URL-safe base64
    uncompressed point the browser's `applicationServerKey` wants, the private key the raw
    32-byte scalar in URL-safe base64 (what `Vapid.from_string` reads)."""
    vapid = Vapid()
    vapid.generate_keys()
    public = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(public), _b64url(private)


class PushSender:
    """Sends Web Push messages and keeps `push_subscription` / `notification_log` up to date.

    `webpush` is injectable so tests (and a dry run) never reach a real push service.
    """

    def __init__(
        self,
        engine: Engine,
        vapid_private_key: str,
        vapid_subject: str,
        *,
        clock: Clock | None = None,
        webpush: Callable[..., Any] = pywebpush.webpush,
        ttl: int = TTL_S,
        max_workers: int = MAX_WORKERS,
    ):
        if not vapid_private_key:
            raise ValueError("VAPID_PRIVATE_KEY is not set (run `parking push vapid-keys`)")
        if not vapid_subject:
            raise ValueError("VAPID_SUBJECT is not set (e.g. mailto:you@example.com)")
        self._engine = engine
        self._vapid = Vapid.from_string(vapid_private_key)  # parsed once, fails early if bad
        self._subject = vapid_subject
        self._clock = clock or SystemClock()
        self._webpush = webpush
        self._ttl = ttl
        self._max_workers = max_workers

    @classmethod
    def from_settings(cls, engine: Engine, settings, **kw) -> PushSender:
        key = settings.vapid_private_key
        return cls(
            engine,
            key.get_secret_value() if key else "",
            settings.vapid_subject or "",
            **kw,
        )

    def send(
        self, sub: PushSubscription, payload: dict[str, Any], urgency: Urgency = "normal"
    ) -> SendResult:
        """Push `payload` (api.md §6) to one subscription and record the outcome."""
        sub_id, info = sub.id, sub.subscription_info()
        try:
            response = self._webpush(
                subscription_info=info,
                data=json.dumps(payload),
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self._subject},  # fresh dict: pywebpush adds aud/exp to it
                ttl=self._ttl,
                headers={"Urgency": urgency},
                timeout=TIMEOUT_S,
            )
        except Exception as e:  # WebPushException, network errors, bad subscription keys
            status = e.status_code if isinstance(e, pywebpush.WebPushException) else None
            error = (f"{status}: " if status else "") + str(e)
            return self._record_failure(sub_id, payload, status, error[:ERROR_MAX_CHARS])
        return self._record_success(sub_id, payload, getattr(response, "status_code", None))

    def send_many(
        self,
        subs: Iterable[PushSubscription],
        payload: dict[str, Any],
        urgency: Urgency = "normal",
    ) -> list[SendResult]:
        """`send` to each subscription, at most `max_workers` at a time; results in input
        order."""
        subs = list(subs)
        if not subs:
            return []
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(subs))) as pool:
            return list(pool.map(lambda s: self.send(s, payload, urgency), subs))

    def _record_success(
        self, sub_id: str, payload: dict[str, Any], status: int | None
    ) -> SendResult:
        now = self._clock.now()
        with session_scope(self._engine) as session:
            row = session.get(PushSubscription, sub_id)
            if row is not None:
                row.failures = 0
                row.last_sent_at = now
            session.add(self._log(sub_id, payload, "sent"))
        return SendResult(sub_id, ok=True, status_code=status)

    def _record_failure(
        self, sub_id: str, payload: dict[str, Any], status: int | None, error: str
    ) -> SendResult:
        deleted = False
        with session_scope(self._engine) as session:
            row = session.get(PushSubscription, sub_id)
            if row is not None:
                row.failures += 1
                if status in GONE or row.failures >= MAX_FAILURES:
                    session.delete(row)
                    deleted = True
            session.add(self._log(sub_id, payload, "failed", error))
        log.warning(
            "push to %s failed%s: %s", sub_id, " (subscription deleted)" if deleted else "", error
        )
        return SendResult(sub_id, ok=False, status_code=status, deleted=deleted, error=error)

    def _log(
        self, sub_id: str, payload: dict[str, Any], status: str, error: str | None = None
    ) -> NotificationLog:
        return NotificationLog(
            ts=self._clock.now(),
            subscription_id=sub_id,
            kind=str(payload.get("kind", "")),
            payload=payload,
            status=status,
            error=error,
        )
