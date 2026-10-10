"""A barrier's contacts (docs/design/barrier.md): where the edges come from and how they become
entries and exits.

Sources (`cameras[].source` of a `role: barrier` entry), all giving `Edge`s of named inputs:

- `gpio:<chip>?in=17&out=27` (or `a=17&b=27`): dry contacts on GPIO inputs of this machine,
  read through the kernel's GPIO character device (the `gpiod` package). Options:
  `active=low` (default: the contact closes the input to GND) | `high`, `bias=pull-up`
  (default) | `pull-down` | `none` (an external resistor), `debounce_ms=30`.
- `contacts:<file.csv>?realtime=true&loop=false`: a recorded or hand-written sequence, one
  `seconds,input,closed|open` line per edge (`#` comments), for tests and for trying a
  configuration without the hardware.

Decoders (pure, fed one edge at a time):

- `PulseDecoder`: every closing of `in` is a car in, of `out` a car out; closings of the same
  input less than `min_gap_ms` after the last counted one are the same car (contact bounce,
  a relay that chatters).
- `PairDecoder`: two presence loops `a` and `b` one after the other in one lane. A car that
  comes onto one loop and leaves over the other is counted in the direction it drove; one that
  backs out over the loop it came in on is not.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl

from parking.config import BarrierCfg
from parking.messages import Direction

PULSE_INPUTS = ("in", "out")
PAIR_INPUTS = ("a", "b")
INPUTS = PULSE_INPUTS + PAIR_INPUTS
DEFAULT_DEBOUNCE_MS = 30
MAX_EDGES = 64  # read per call
SETTLE_S = 0.05  # gpio: between switching on the pull resistor and watching the level


class ContactError(Exception):
    """The contacts can't be read (yet): the message says why."""


@dataclass(frozen=True)
class Edge:
    """One input changed: `closed` = the contact closed (a car is there)."""

    ts: float  # epoch seconds
    input: str
    closed: bool


class ContactSource(Protocol):
    inputs: tuple[str, ...]
    exhausted: bool  # nothing more will ever come (a replay that ended)

    def states(self) -> dict[str, bool]:
        """Which inputs are closed right now (read once, before the first `read`)."""
        ...

    def read(self, timeout: float) -> list[Edge]:
        """The edges since the last call, oldest first; waits up to `timeout` s for one."""
        ...

    def close(self) -> None: ...


# --- decoders ---


class PulseDecoder:
    inputs = PULSE_INPUTS

    def __init__(self, min_gap_s: float = 1.0):
        self.min_gap_s = min_gap_s
        self._counted: dict[str, float] = {}

    def prime(self, states: dict[str, bool]) -> None:
        """A contact that is closed at start is a car already counted (or a stuck relay)."""

    def feed(self, edge: Edge) -> Direction | None:
        if not edge.closed or edge.input not in PULSE_INPUTS:
            return None
        last = self._counted.get(edge.input)
        if last is not None and 0 <= edge.ts - last < self.min_gap_s:
            return None
        self._counted[edge.input] = edge.ts
        return "in" if edge.input == "in" else "out"


class PairDecoder:
    inputs = PAIR_INPUTS

    def __init__(self, in_direction: str = "a_to_b", timeout_s: float = 30.0):
        self.in_direction = in_direction
        self.timeout_s = timeout_s
        self._closed = {"a": False, "b": False}
        self._first: str | None = None  # the loop the car in the lane came onto first
        self._other_seen = False  # it reached the second loop
        self._ts = 0.0  # last edge of the passage

    def prime(self, states: dict[str, bool]) -> None:
        """Start from the loops' current state; a car standing on one is taken as coming from
        there (on both: unknown, it isn't counted)."""
        self._closed = {name: bool(states.get(name)) for name in PAIR_INPUTS}
        on = [name for name, closed in self._closed.items() if closed]
        self._first = on[0] if on else None
        self._other_seen = len(on) == 2
        if self._other_seen:
            self._first = None

    def feed(self, edge: Edge) -> Direction | None:
        name = edge.input
        if name not in self._closed or self._closed[name] == edge.closed:
            return None
        both_open = not any(self._closed.values())
        self._closed[name] = edge.closed
        if edge.closed:
            if self._first is None and not both_open:
                pass  # unknown start (`prime` with both loops taken): wait until the lane is clear
            elif self._first is None or (
                both_open
                and not self._other_seen
                and (name == self._first or edge.ts - self._ts > self.timeout_s)
            ):
                self._first, self._other_seen = name, False  # a new passage
            elif name != self._first:
                self._other_seen = True
            self._ts = edge.ts
            return None
        if any(self._closed.values()):
            return None
        # the lane is clear again
        self._ts = edge.ts
        if self._first is None:
            self._other_seen = False
            return None
        if not self._other_seen:
            return None  # loops that don't overlap: the second one may still come
        first, self._first, self._other_seen = self._first, None, False
        if name == first:
            return None  # left over the loop it came in on: backed out
        return "in" if (first == "a") == (self.in_direction == "a_to_b") else "out"


def make_decoder(cfg: BarrierCfg) -> PulseDecoder | PairDecoder:
    if cfg.mode == "pair":
        return PairDecoder(cfg.in_direction, cfg.pair_timeout_s)
    return PulseDecoder(cfg.min_gap_ms / 1000)


# --- sources ---


def _split(spec: str) -> tuple[str, dict[str, str]]:
    target, _, query = spec.partition("?")
    return target, dict(parse_qsl(query, keep_blank_values=True))


def _flag(options: dict[str, str], name: str, default: bool) -> bool:
    value = options.pop(name, None)
    if value is None:
        return default
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise ValueError(f"{name} must be true or false, not '{value}'")


def _no_more(options: dict[str, str], scheme: str) -> None:
    if options:
        raise ValueError(f"unknown {scheme}: option(s): {', '.join(sorted(options))}")


class ReplaySource:
    """`contacts:<file.csv>`: the file's edges at their times after the start (`realtime=false`:
    all at once, with the times they would have had)."""

    def __init__(self, path: Path, realtime: bool = True, loop: bool = False):
        self.path = path
        self.realtime = realtime
        self.loop = loop
        self.script = self._load(path)
        self.inputs = tuple(n for n in INPUTS if any(e[1] == n for e in self.script))
        self.exhausted = not self.script
        self._i = 0
        self._started = time.monotonic()
        self._epoch = time.time()
        self._period = self.script[-1][0] if self.script else 0.0

    @staticmethod
    def _load(path: Path) -> list[tuple[float, str, bool]]:
        if not path.is_file():
            raise ContactError(f"contact file not found: {path}")
        script = []
        with path.open(newline="", encoding="utf-8") as f:
            for n, row in enumerate(csv.reader(f), 1):
                if not row or row[0].lstrip().startswith("#"):
                    continue
                try:
                    seconds, name, state = (c.strip() for c in row)
                    if name not in INPUTS or state not in ("closed", "open") or float(seconds) < 0:
                        raise ValueError
                    script.append((float(seconds), name, state == "closed"))
                except ValueError:
                    raise ContactError(
                        f"{path.name} line {n}: expected `seconds,{'|'.join(INPUTS)},closed|open`"
                    ) from None
        return sorted(script, key=lambda e: e[0])

    def states(self) -> dict[str, bool]:
        return dict.fromkeys(self.inputs, False)

    def read(self, timeout: float) -> list[Edge]:
        if self.exhausted:
            time.sleep(timeout)
            return []
        out: list[Edge] = []
        while not self.exhausted and len(out) < MAX_EDGES:
            offset, name, closed = self.script[self._i]
            if self.realtime:
                wait = offset - (time.monotonic() - self._started)
                if wait > 0:
                    if out or wait > timeout:
                        break
                    time.sleep(wait)
            out.append(Edge(self._epoch + offset, name, closed))
            self._i += 1
            if self._i == len(self.script):
                if self.loop and self._period > 0:
                    self._i = 0
                    self._started += self._period
                    self._epoch += self._period
                else:
                    self.exhausted = True
        if not out:
            time.sleep(timeout)
        return out

    def close(self) -> None:
        pass


class GpioSource:
    """`gpio:<chip>?<input>=<line>...`: edges of GPIO input lines, debounced by the kernel."""

    def __init__(
        self,
        chip: str,
        lines: dict[str, int],
        active_low: bool = True,
        bias: str = "pull-up",
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        gpiod: Any = None,
    ):
        """`gpiod`: the module to use (tests pass a fake); default: the installed package."""
        self.chip = chip
        self.lines = lines
        self.inputs = tuple(lines)
        self.exhausted = False
        self._by_offset = {offset: name for name, offset in lines.items()}
        if gpiod is None:
            try:
                import gpiod
            except ImportError:
                raise ContactError(
                    "the gpiod package is not installed (it comes with the vision extra)"
                ) from None
        self._gpiod = gpiod
        line = gpiod.line
        wiring = {
            "direction": line.Direction.INPUT,
            "bias": {
                "pull-up": line.Bias.PULL_UP,
                "pull-down": line.Bias.PULL_DOWN,
                "none": line.Bias.DISABLED,
            }[bias],
            "active_low": active_low,
        }
        offsets = tuple(self._by_offset)
        try:
            # In two steps: the pull resistor first, the edge detection once the level has
            # settled. In one step the kernel's debouncer can start from the level before the
            # pull took hold and then misses the first closing (seen on a Pi 5).
            self._request = gpiod.request_lines(
                chip, consumer="parking-barrier", config={offsets: gpiod.LineSettings(**wiring)}
            )
        except FileNotFoundError:
            raise ContactError(f"no GPIO chip at {chip}") from None
        except PermissionError:
            raise ContactError(
                f"no permission to open {chip} (the user must be in the gpio group)"
            ) from None
        except OSError as e:
            used = ", ".join(f"{n}={o}" for n, o in lines.items())
            raise ContactError(f"can't use line(s) {used} of {chip}: {e.strerror or e}") from None
        time.sleep(SETTLE_S)
        try:
            self._request.reconfigure_lines(
                {
                    offsets: gpiod.LineSettings(
                        **wiring,
                        edge_detection=line.Edge.BOTH,
                        debounce_period=timedelta(milliseconds=debounce_ms),
                    )
                }
            )
        except OSError as e:
            self._request.release()
            raise ContactError(f"can't watch the lines of {chip}: {e.strerror or e}") from None

    def states(self) -> dict[str, bool]:
        active = self._gpiod.line.Value.ACTIVE
        try:
            values = self._request.get_values(list(self._by_offset))
        except OSError as e:
            raise ContactError(f"reading {self.chip} failed: {e.strerror or e}") from None
        return {name: v == active for name, v in zip(self._by_offset.values(), values, strict=True)}

    def read(self, timeout: float) -> list[Edge]:
        rising = self._gpiod.EdgeEvent.Type.RISING_EDGE
        try:
            if not self._request.wait_edge_events(timedelta(seconds=timeout)):
                return []
            events = self._request.read_edge_events(MAX_EDGES)
        except OSError as e:
            raise ContactError(f"reading {self.chip} failed: {e.strerror or e}") from None
        # the kernel stamps events on the monotonic clock
        now, mono = time.time(), time.monotonic_ns()
        return [
            Edge(
                now - (mono - ev.timestamp_ns) / 1e9,
                self._by_offset[ev.line_offset],
                ev.event_type == rising,  # active = closed (`active_low` is applied by the kernel)
            )
            for ev in events
            if ev.line_offset in self._by_offset
        ]

    def close(self) -> None:
        self._request.release()


def parse_gpio(spec: str) -> dict[str, Any]:
    """`<chip>?in=17&out=27&active=low&bias=pull-up&debounce_ms=30` as `GpioSource` arguments."""
    chip, options = _split(spec)
    if not chip:
        raise ValueError("gpio: needs the chip, e.g. gpio:/dev/gpiochip0?in=17&out=27")
    lines = {}
    for name in INPUTS:
        if name in options:
            value = options.pop(name)
            if not value.isdigit():
                raise ValueError(f"{name} must be a GPIO line number, not '{value}'")
            lines[name] = int(value)
    if not lines:
        raise ValueError("gpio: needs at least one input (in=, out=, or a= and b=)")
    if len(set(lines.values())) != len(lines):
        raise ValueError("gpio: two inputs on the same line")
    active = options.pop("active", "low")
    if active not in ("low", "high"):
        raise ValueError(f"active must be low or high, not '{active}'")
    bias = options.pop("bias", "pull-up" if active == "low" else "pull-down")
    if bias not in ("pull-up", "pull-down", "none"):
        raise ValueError(f"bias must be pull-up, pull-down or none, not '{bias}'")
    debounce = options.pop("debounce_ms", str(DEFAULT_DEBOUNCE_MS))
    if not debounce.isdigit():
        raise ValueError(f"debounce_ms must be a whole number, not '{debounce}'")
    _no_more(options, "gpio")
    return {
        "chip": chip,
        "lines": lines,
        "active_low": active == "low",
        "bias": bias,
        "debounce_ms": int(debounce),
    }


def make_contact_source(spec: str, root: Path, gpiod: Any = None) -> ContactSource:
    """The source for a barrier's `source:` string. `ValueError`: the string is wrong;
    `ContactError`: it is right but the contacts can't be opened now."""
    scheme, sep, rest = spec.partition(":")
    if not sep:
        raise ValueError(f"'{spec}' is not a contact source (gpio:... or contacts:...)")
    if scheme == "gpio":
        return GpioSource(**parse_gpio(rest), gpiod=gpiod)
    if scheme == "contacts":
        target, options = _split(rest)
        realtime = _flag(options, "realtime", True)
        loop = _flag(options, "loop", False)
        _no_more(options, "contacts")
        if not target:
            raise ValueError("contacts: needs a file")
        path = Path(target)
        return ReplaySource(path if path.is_absolute() else root / path, realtime, loop)
    raise ValueError(f"unknown contact source '{scheme}:' (gpio: or contacts:)")


def check_inputs(source: ContactSource, cfg: BarrierCfg) -> None:
    """`ValueError` unless the source has the inputs the barrier's mode reads."""
    have = set(source.inputs)
    if cfg.mode == "pair":
        if not have >= set(PAIR_INPUTS):
            raise ValueError("mode pair needs the inputs a and b")
    elif not have & set(PULSE_INPUTS):
        raise ValueError("mode pulse needs the input in, out or both")
