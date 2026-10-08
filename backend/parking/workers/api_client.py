"""HTTP client the workers use to talk to the API (api.md §5.1).

- Observations and health: one attempt each; on failure log and move on (only the latest matters).
- Flow events: must not be lost. They go to an **outbox** (in memory + `data/outbox/<camera>.jsonl`,
  so they survive a restart); a background thread sends them in batches of ≤ 100 with backoff
  1 → 30 s and removes them once the API accepts them. `event_id` makes resends safe.
- `print_mode`: write each payload as one JSON line to stdout instead of sending it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TextIO

import httpx
from pydantic import BaseModel, ValidationError

from parking.config import Settings
from parking.messages import CameraHealthMsg, FlowEventBatch, FlowEventMsg, Observation

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0
BATCH_MAX = 100
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0


class ApiClient:
    """Sync client (httpx) used by the workers. Call `close()` when done (or use `with`)."""

    def __init__(
        self,
        base_url: str | None,
        token: str | None,
        camera_id: str,
        *,
        outbox_dir: str | Path = "data/outbox",
        print_mode: bool = False,
        timeout: float = TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        backoff_min: float = BACKOFF_MIN_S,
        backoff_max: float = BACKOFF_MAX_S,
        sleep: Callable[[float], bool] | None = None,
        out: TextIO | None = None,
    ) -> None:
        """`sleep(seconds)` waits between retries and returns True if the client is closing
        (default: an interruptible wait). Tests pass a fake to record the backoff delays."""
        if not print_mode and not base_url:
            raise ValueError("API_INTERNAL_URL is not set (or use print mode)")
        self.camera_id = camera_id
        self.print_mode = print_mode
        self._out = out or sys.stdout
        self._print_lock = threading.Lock()  # the loop and the heartbeat both print
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._http: httpx.Client | None = None
        if not print_mode:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            self._http = httpx.Client(
                base_url=base_url or "", headers=headers, timeout=timeout, transport=transport
            )

        self.outbox_path = Path(outbox_dir) / f"{camera_id}.jsonl"
        self._cond = threading.Condition()
        self._outbox: list[FlowEventMsg] = [] if print_mode else self._load_outbox()
        self._thread: threading.Thread | None = None
        if self._outbox:
            log.info("outbox: %d unsent flow event(s) from %s", len(self._outbox), self.outbox_path)
            self._start_sender()

    @classmethod
    def from_settings(
        cls, settings: Settings, camera_id: str, *, print_mode: bool = False, **kwargs
    ) -> ApiClient:
        token = settings.worker_token.get_secret_value() if settings.worker_token else None
        return cls(settings.api_internal_url, token, camera_id, print_mode=print_mode, **kwargs)

    # --- fire and forget ---

    def send_observation(self, obs: Observation) -> bool:
        """One attempt; on failure log and drop. Returns True if the API accepted it."""
        return self._post_once("/internal/observations", obs)

    def send_health(self, msg: CameraHealthMsg) -> bool:
        """One attempt; on failure log. Returns True if the API accepted it."""
        return self._post_once("/internal/health", msg)

    def _post_once(self, path: str, msg: BaseModel) -> bool:
        if self.print_mode:
            self._print(msg)
            return True
        assert self._http is not None
        try:
            r = self._http.post(path, content=msg.model_dump_json(), headers=_JSON)
            r.raise_for_status()
            return True
        except httpx.HTTPError as e:
            log.warning("POST %s failed, dropped: %s", path, _describe(e))
            return False

    def _print(self, msg: BaseModel) -> None:
        with self._print_lock:
            self._out.write(msg.model_dump_json() + "\n")
            self._out.flush()

    # --- flow events (outbox) ---

    def queue_flow_events(self, events: Iterable[FlowEventMsg]) -> None:
        """Append to the outbox (disk first, then memory) and wake the sender."""
        events = list(events)
        if not events:
            return
        if self.print_mode:
            for i in range(0, len(events), BATCH_MAX):
                self._print(FlowEventBatch(events=events[i : i + BATCH_MAX]))
            return
        with self._cond:
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            with self.outbox_path.open("a", encoding="utf-8") as f:
                f.writelines(e.model_dump_json() + "\n" for e in events)
                f.flush()
                os.fsync(f.fileno())
            self._outbox.extend(events)
            self._cond.notify_all()
        self._start_sender()

    @property
    def pending(self) -> int:
        """Flow events not yet accepted by the API."""
        with self._cond:
            return len(self._outbox)

    def flush(self, timeout: float) -> bool:
        """Wait up to `timeout` s for the outbox to empty. True if it did."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._outbox:
                left = deadline - time.monotonic()
                if left <= 0 or self._stop.is_set():
                    return False
                self._cond.wait(left)
            return True

    def _start_sender(self) -> None:
        with self._cond:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(
                    target=self._run_sender, name=f"outbox-{self.camera_id}", daemon=True
                )
                self._thread.start()

    def _run_sender(self) -> None:
        delay = self._backoff_min
        while not self._stop.is_set():
            with self._cond:
                while not self._outbox and not self._stop.is_set():
                    self._cond.wait()
                if self._stop.is_set():
                    return
                batch = self._outbox[:BATCH_MAX]
            if self._send_batch(batch):
                delay = self._backoff_min
                self._remove_sent(len(batch))
            else:
                log.info("outbox: %d pending, retry in %.0f s", self.pending, delay)
                if self._sleep(delay):
                    return
                delay = min(delay * 2, self._backoff_max)

    def _send_batch(self, batch: list[FlowEventMsg]) -> bool:
        assert self._http is not None
        body = FlowEventBatch(events=batch).model_dump_json()
        try:
            r = self._http.post("/internal/flow-events", content=body, headers=_JSON)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning(
                "POST /internal/flow-events (%d events) failed: %s", len(batch), _describe(e)
            )
            return False
        log.debug("flow events sent: %s", r.text)
        return True

    def _remove_sent(self, n: int) -> None:
        """Drop the first `n` events (only the sender removes, so they're the ones sent)."""
        with self._cond:
            del self._outbox[:n]
            self._rewrite_outbox()
            self._cond.notify_all()

    def _rewrite_outbox(self) -> None:
        if not self._outbox:
            self.outbox_path.unlink(missing_ok=True)
            return
        tmp = self.outbox_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.writelines(e.model_dump_json() + "\n" for e in self._outbox)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.outbox_path)

    def _load_outbox(self) -> list[FlowEventMsg]:
        if not self.outbox_path.exists():
            return []
        events: list[FlowEventMsg] = []
        seen: set[str] = set()
        for n, line in enumerate(self.outbox_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                e = FlowEventMsg.model_validate_json(line)
            except ValidationError:
                # A torn last line after a crash mid-write; the rest is still good.
                log.warning("outbox: skipping unreadable line %d of %s", n, self.outbox_path)
                continue
            if e.event_id not in seen:
                seen.add(e.event_id)
                events.append(e)
        return events

    # --- lifecycle ---

    def close(self) -> None:
        """Stop the sender. Unsent events stay in the outbox file for the next start."""
        with self._cond:
            self._stop.set()
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT_S + 1)
        if self._http is not None:
            self._http.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_JSON = {"Content-Type": "application/json"}


def _describe(e: httpx.HTTPError) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return f"{type(e).__name__}: {e}"
