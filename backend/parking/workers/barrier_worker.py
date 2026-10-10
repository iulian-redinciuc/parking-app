"""Barrier worker (docs/design/barrier.md): a barrier controller's contacts in, entry/exit
events out.

Reads the edges of the barrier's `source:` (workers/contacts.py), turns them into cars in and
out with the decoder of its `barrier.mode`, and queues one flow event per car with
`source: "barrier"` (`ApiClient.queue_flow_events`: the outbox delivers them, also across
restarts). Every event gets a fresh `uuid4` `event_id`; `track_id` is the event's number
since the worker started.

- The contacts can't be opened or read (no GPIO chip, no permission, a line in use): the reason
  is logged, the health goes `down` / `connect_failed`, and it tries again every `RETRY_S`.
- Health every 10 s (base `Worker`): `ok` while the contacts are open. There is no frame, so
  no fps or frame age.
- A replay source that has ended stops the worker.

One INFO line per car, an INFO summary every `SUMMARY_EVERY_S`; the edges at DEBUG.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from parking.messages import CameraHealthMsg, Direction, FlowEventMsg
from parking.workers.base import Worker, WorkerError
from parking.workers.contacts import (
    ContactError,
    ContactSource,
    Edge,
    check_inputs,
    make_contact_source,
    make_decoder,
)

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 0.2  # how long one read waits; also how fast the worker notices a stop
RETRY_S = 10.0  # between attempts to open the contacts
SUMMARY_EVERY_S = 60.0
BARRIER_CLASS = "vehicle"  # the contact can't tell what drove through


class BarrierWorker(Worker):
    role = "barrier"

    def __init__(self, config_path: Path, camera_id: str, *, gpiod: Any = None, **kw):
        """`gpiod`: the GPIO module for `gpio:` sources (tests pass a fake)."""
        kw.setdefault("control_port", None)  # nothing to show or reload
        super().__init__(config_path, camera_id, **kw)
        self._gpiod = gpiod
        self.source: ContactSource | None = None
        self.error: str | None = None  # why the contacts aren't open
        self.decoder = make_decoder(self.camera.barrier)
        self.events = 0  # cars counted since the start
        self.counts = {"in": 0, "out": 0}
        self._summary = {"in": 0, "out": 0}
        try:
            self._open()
        except ValueError as e:
            self.api.close()
            raise WorkerError(f"barrier '{camera_id}' source: {e}") from None

    def _open(self) -> bool:
        """Open the contacts. False (and `error` set) if they can't be opened now;
        `ValueError` if the configuration is wrong."""
        try:
            source = make_contact_source(self.camera.source, self.root, self._gpiod)
        except ContactError as e:
            if self.error != str(e):
                log.error("%s: %s", self.camera_id, e)
            self.error = str(e)
            return False
        try:
            check_inputs(source, self.camera.barrier)
            states = source.states()
        except (ValueError, ContactError):
            source.close()
            raise
        self.decoder = make_decoder(self.camera.barrier)
        self.decoder.prime(states)
        self.source, self.error = source, None
        closed = [name for name, on in states.items() if on]
        log.info(
            "%s: contacts open (%s)%s",
            self.camera_id,
            ", ".join(source.inputs),
            f", closed now: {', '.join(closed)}" if closed else "",
        )
        return True

    # --- the loop ---

    def loop(self, max_frames: int | None = None) -> None:
        """`max_frames`: stop after this many cars."""
        cfg = self.camera.barrier
        log.info("%s: barrier worker, mode %s", self.camera_id, cfg.mode)
        next_summary = time.monotonic() + SUMMARY_EVERY_S
        while not self.stop_event.is_set():
            if max_frames is not None and self.events >= max_frames:
                break
            self.alive()
            if time.monotonic() >= next_summary:
                self._log_summary()
                next_summary += SUMMARY_EVERY_S
            if self.source is None:
                try:
                    opened = self._open()
                except (ValueError, ContactError) as e:
                    log.error("%s: %s", self.camera_id, e)
                    self.error, opened = str(e), False
                if not opened:
                    self.stop_event.wait(RETRY_S)
                    continue
            self.step()
            if self.source is not None and self.source.exhausted:
                log.info("%s: source has no more edges", self.camera_id)
                break

    def step(self) -> list[FlowEventMsg]:
        """Read the edges that are there (waiting `READ_TIMEOUT_S` for one) and count them."""
        assert self.source is not None
        try:
            edges = self.source.read(READ_TIMEOUT_S)
        except ContactError as e:
            log.error("%s: %s", self.camera_id, e)
            self.error = str(e)
            self.source.close()
            self.source = None
            return []
        msgs = []
        for edge in edges:
            log.debug("%s: %s %s", self.camera_id, edge.input, "closed" if edge.closed else "open")
            direction = self.decoder.feed(edge)
            if direction is not None:
                msgs.append(self._event(edge, direction))
        if msgs:
            self.api.queue_flow_events(msgs)
        return msgs

    def _event(self, edge: Edge, direction: Direction) -> FlowEventMsg:
        self.events += 1
        self.counts[direction] += 1
        self._summary[direction] += 1
        log.info(
            "%s: %s, running in %d / out %d",
            self.camera_id,
            direction.upper(),
            self.counts["in"],
            self.counts["out"],
        )
        return FlowEventMsg(
            event_id=str(uuid.uuid4()),
            source="barrier",
            camera_id=self.camera_id,
            ts=datetime.fromtimestamp(edge.ts, UTC),
            direction=direction,
            track_id=self.events,
            cls=BARRIER_CLASS,
            confidence=1.0,
        )

    def _log_summary(self) -> None:
        s, self._summary = self._summary, {"in": 0, "out": 0}
        log.info(
            "%s: last %.0f s: in %d, out %d, %d pending in the outbox; running in %d / out %d",
            self.camera_id,
            SUMMARY_EVERY_S,
            s["in"],
            s["out"],
            self.api.pending,
            self.counts["in"],
            self.counts["out"],
        )

    # --- health ---

    def health_message(self, final: bool = False) -> CameraHealthMsg:
        down = final or self.source is None
        return CameraHealthMsg(
            camera_id=self.camera_id,
            ts=datetime.now(UTC),
            state="down" if down else "ok",
            issue="connect_failed" if self.source is None and not final else None,
            started_at=self.started_at,
            **self.system_fields(),
        )

    def shutdown(self) -> None:
        super().shutdown()
        if self.source is not None:
            self.source.close()
