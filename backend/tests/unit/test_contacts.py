"""A barrier's contacts (workers/contacts.py, barrier.md §2–3): the decoders, the replay file
and the GPIO source against a fake `gpiod`."""

import errno
import time
from datetime import timedelta
from enum import Enum
from types import SimpleNamespace

import pytest

from parking.config import BarrierCfg
from parking.workers.contacts import (
    ContactError,
    Edge,
    GpioSource,
    PairDecoder,
    PulseDecoder,
    ReplaySource,
    check_inputs,
    make_contact_source,
    make_decoder,
    parse_gpio,
)


def run(decoder, script):
    """`script`: (seconds, input, closed) -> the directions counted, with their times."""
    out = []
    for ts, name, closed in script:
        direction = decoder.feed(Edge(ts, name, closed))
        if direction:
            out.append((ts, direction))
    return out


# --- pulse ---


def test_pulse_counts_every_closing():
    script = [(0, "in", True), (0.4, "in", False), (5, "out", True), (5.3, "out", False)]
    script += [(9, "in", True), (9.5, "in", False)]
    assert run(PulseDecoder(1.0), script) == [(0, "in"), (5, "out"), (9, "in")]


def test_pulse_bounce_is_one_car_per_input():
    script = [(0, "in", True), (0.01, "in", False), (0.02, "in", True), (0.9, "in", True)]
    script += [(0.5, "out", True), (1.0, "in", True)]  # the other lane isn't locked out
    assert run(PulseDecoder(1.0), script) == [(0, "in"), (0.5, "out"), (1.0, "in")]
    assert run(PulseDecoder(0), [(0, "in", True), (0, "in", True)]) == [(0, "in"), (0, "in")]


def test_pulse_ignores_other_inputs_and_a_contact_closed_at_start():
    decoder = PulseDecoder()
    decoder.prime({"in": True})
    assert run(decoder, [(0, "a", True), (1, "in", False), (2, "in", True)]) == [(2, "in")]


# --- pair ---

A_TO_B = [(0, "a", True), (1, "b", True), (2, "a", False), (3, "b", False)]
B_TO_A = [(0, "b", True), (1, "a", True), (2, "b", False), (3, "a", False)]


def test_pair_direction_from_the_order():
    assert run(PairDecoder("a_to_b"), A_TO_B) == [(3, "in")]
    assert run(PairDecoder("a_to_b"), B_TO_A) == [(3, "out")]
    assert run(PairDecoder("b_to_a"), A_TO_B) == [(3, "out")]
    two = A_TO_B + [(10 + t, n, c) for t, n, c in B_TO_A]
    assert run(PairDecoder(), two) == [(3, "in"), (13, "out")]


def test_pair_backing_out_is_not_counted():
    back = [(0, "a", True), (1, "b", True), (2, "b", False), (3, "a", False)]
    assert run(PairDecoder(), back) == []
    # fully onto the second loop, then back the way it came
    far = [(0, "a", True), (1, "b", True), (2, "a", False), (3, "a", True), (4, "b", False)]
    assert run(PairDecoder(), far + [(5, "a", False)]) == []
    # only ever on the first loop
    assert run(PairDecoder(), [(0, "a", True), (1, "a", False)]) == []


def test_pair_loops_that_do_not_overlap():
    apart = [(0, "a", True), (1, "a", False), (3, "b", True), (4, "b", False)]
    assert run(PairDecoder(timeout_s=30), apart) == [(4, "in")]
    # the second loop long after the first: two cars that each touched one loop
    late = [(0, "a", True), (1, "a", False), (40, "b", True), (41, "b", False)]
    assert run(PairDecoder(timeout_s=30), late) == []
    # the same loop again starts over
    again = [(0, "a", True), (1, "a", False), (2, "a", True), (3, "a", False)]
    assert run(PairDecoder(), again + [(4, "b", True), (5, "b", False)]) == [(5, "in")]


def test_pair_repeated_levels_and_start_states():
    noisy = [(0, "a", True), (0.1, "a", True), (1, "b", True), (2, "a", False), (2.1, "a", False)]
    assert run(PairDecoder(), noisy + [(3, "b", False)]) == [(3, "in")]
    # a car standing on loop a when the worker starts is coming from a
    decoder = PairDecoder()
    decoder.prime({"a": True, "b": False})
    assert run(decoder, [(1, "b", True), (2, "a", False), (3, "b", False)]) == [(3, "in")]
    # on both: the direction is unknown, the next car counts again
    decoder = PairDecoder()
    decoder.prime({"a": True, "b": True})
    assert run(decoder, [(1, "a", False), (2, "b", False)]) == []
    assert run(decoder, [(10 + t, n, c) for t, n, c in B_TO_A]) == [(13, "out")]


def test_make_decoder_and_inputs():
    assert isinstance(make_decoder(BarrierCfg()), PulseDecoder)
    assert make_decoder(BarrierCfg(min_gap_ms=250)).min_gap_s == 0.25
    pair = make_decoder(BarrierCfg(mode="pair", in_direction="b_to_a", pair_timeout_s=5))
    assert (pair.in_direction, pair.timeout_s) == ("b_to_a", 5)
    one = SimpleNamespace(inputs=("out",))
    check_inputs(one, BarrierCfg())
    with pytest.raises(ValueError, match="mode pair needs the inputs a and b"):
        check_inputs(one, BarrierCfg(mode="pair"))
    with pytest.raises(ValueError, match="mode pulse needs the input in, out or both"):
        check_inputs(SimpleNamespace(inputs=("a", "b")), BarrierCfg())


# --- replay file ---


def test_replay_all_at_once(tmp_path):
    file = tmp_path / "contacts.csv"
    file.write_text("# seconds,input,state\n2, out, closed\n0,in,closed\n0.5,in,open\n\n")
    before = time.time()
    source = make_contact_source("contacts:contacts.csv?realtime=false", tmp_path)
    assert source.inputs == ("in", "out") and source.states() == {"in": False, "out": False}
    edges = source.read(0.01)
    assert [(e.input, e.closed) for e in edges] == [("in", True), ("in", False), ("out", True)]
    assert edges[2].ts - edges[0].ts == pytest.approx(2)
    assert before <= edges[0].ts <= time.time()
    assert source.exhausted and source.read(0.01) == []


def test_replay_in_real_time_and_looping(tmp_path):
    file = tmp_path / "c.csv"
    file.write_text("0.05,in,closed\n0.15,in,open\n")
    source = ReplaySource(file, realtime=True, loop=True)
    assert source.read(0.01) == []  # nothing due yet
    seen = []
    deadline = time.monotonic() + 2
    while len(seen) < 4 and time.monotonic() < deadline:
        seen += source.read(0.05)
    assert [e.closed for e in seen] == [True, False, True, False]
    assert not source.exhausted
    assert seen[2].ts - seen[0].ts == pytest.approx(0.15)  # one period later


@pytest.mark.parametrize("line", ["x,in,closed", "1,gate,closed", "1,in,up", "-1,in,open", "1,in"])
def test_replay_bad_lines(tmp_path, line):
    (tmp_path / "c.csv").write_text(f"0,in,closed\n{line}\n")
    with pytest.raises(ContactError, match=r"c\.csv line 2: expected"):
        make_contact_source("contacts:c.csv", tmp_path)


def test_source_strings(tmp_path):
    with pytest.raises(ContactError, match="contact file not found"):
        make_contact_source("contacts:none.csv", tmp_path)
    for spec, error in [
        ("rtsp://cam", "unknown contact source 'rtsp:'"),
        ("nothing", "is not a contact source"),
        ("contacts:", "needs a file"),
        ("contacts:c.csv?speed=2", r"unknown contacts: option\(s\): speed"),
        ("contacts:c.csv?loop=maybe", "loop must be true or false"),
        ("gpio:?in=1", "needs the chip"),
        ("gpio:/dev/gpiochip0", "needs at least one input"),
        ("gpio:/dev/gpiochip0?in=x", "in must be a GPIO line number"),
        ("gpio:/dev/gpiochip0?in=4&out=4", "two inputs on the same line"),
        ("gpio:/dev/gpiochip0?in=4&active=up", "active must be low or high"),
        ("gpio:/dev/gpiochip0?in=4&bias=strong", "bias must be"),
        ("gpio:/dev/gpiochip0?in=4&debounce_ms=fast", "debounce_ms must be a whole number"),
        ("gpio:/dev/gpiochip0?in=4&pin=5", r"unknown gpio: option\(s\): pin"),
    ]:
        with pytest.raises(ValueError, match=error):
            make_contact_source(spec, tmp_path)


def test_parse_gpio_defaults():
    assert parse_gpio("/dev/gpiochip0?in=17&out=27") == {
        "chip": "/dev/gpiochip0",
        "lines": {"in": 17, "out": 27},
        "active_low": True,
        "bias": "pull-up",
        "debounce_ms": 30,
    }
    high = parse_gpio("/dev/gpiochip4?a=5&b=6&active=high&debounce_ms=0")
    assert (high["lines"], high["active_low"], high["bias"]) == (
        {"a": 5, "b": 6},
        False,
        "pull-down",
    )
    assert parse_gpio("/dev/gpiochip0?out=4&bias=none")["bias"] == "none"


# --- GPIO ---


class FakeGpiod:
    """The part of the `gpiod` 2.x API the source uses."""

    class line:  # noqa: N801
        Direction = Enum("Direction", "INPUT OUTPUT")
        Edge = Enum("Edge", "NONE RISING FALLING BOTH")
        Bias = Enum("Bias", "PULL_UP PULL_DOWN DISABLED")
        Value = Enum("Value", "INACTIVE ACTIVE")

    class EdgeEvent:
        Type = Enum("Type", "RISING_EDGE FALLING_EDGE")

    def __init__(self, error=None):
        self.error = error
        self.requests = []
        self.events = []
        self.values = {}
        self.released = False
        self.fail_read = None

    def LineSettings(self, **kw):  # noqa: N802
        return kw

    def request_lines(self, path, consumer, config):
        if self.error:
            raise self.error
        self.requests.append((path, consumer, config))
        return self

    def reconfigure_lines(self, config):
        self.requests.append(config)

    def get_values(self, offsets):
        return [self.values.get(o, self.line.Value.INACTIVE) for o in offsets]

    def wait_edge_events(self, timeout):
        assert isinstance(timeout, timedelta)
        if self.fail_read:
            raise self.fail_read
        return bool(self.events)

    def read_edge_events(self, max_events):
        events, self.events = self.events[:max_events], self.events[max_events:]
        return events

    def release(self):
        self.released = True

    def edge(self, offset, rising, age_s=0.0):
        kind = self.EdgeEvent.Type.RISING_EDGE if rising else self.EdgeEvent.Type.FALLING_EDGE
        ts = time.monotonic_ns() - int(age_s * 1e9)
        self.events.append(SimpleNamespace(line_offset=offset, event_type=kind, timestamp_ns=ts))


def test_gpio_source_requests_debounced_inputs_and_reads_edges(tmp_path):
    fake = FakeGpiod()
    source = make_contact_source("gpio:/dev/gpiochip0?in=17&out=27", tmp_path, gpiod=fake)
    assert source.inputs == ("in", "out")
    # the pull resistor first, the edge detection after the level has settled
    (path, consumer, config), watch = fake.requests
    assert (path, consumer) == ("/dev/gpiochip0", "parking-barrier")
    wiring = {
        "direction": fake.line.Direction.INPUT,
        "bias": fake.line.Bias.PULL_UP,
        "active_low": True,
    }
    assert config == {(17, 27): wiring}
    assert watch == {
        (17, 27): wiring
        | {"edge_detection": fake.line.Edge.BOTH, "debounce_period": timedelta(milliseconds=30)}
    }
    fake.values[27] = fake.line.Value.ACTIVE
    assert source.states() == {"in": False, "out": True}
    assert source.read(0.01) == []
    fake.edge(17, True, age_s=2.0)
    fake.edge(17, False)
    fake.edge(99, True)  # not one of ours
    now = time.time()
    closed, opened = source.read(0.01)
    assert (closed.input, closed.closed, opened.closed) == ("in", True, False)
    assert closed.ts == pytest.approx(now - 2.0, abs=0.2)  # the kernel's time, as wall time
    assert opened.ts == pytest.approx(now, abs=0.2)
    source.close()
    assert fake.released


def test_gpio_source_says_why_it_cannot_open():
    lines = {"in": 17}
    for error, text in [
        (FileNotFoundError(), "no GPIO chip at /dev/gpiochip9"),
        (PermissionError(), "no permission to open /dev/gpiochip9"),
        (OSError(errno.EBUSY, "Device or resource busy"), "can't use line(s) in=17"),
    ]:
        with pytest.raises(ContactError) as e:
            GpioSource("/dev/gpiochip9", lines, gpiod=FakeGpiod(error))
        assert text in str(e.value)
    fake = FakeGpiod()
    source = GpioSource("/dev/gpiochip0", lines, gpiod=fake)
    fake.fail_read = OSError(errno.ENODEV, "No such device")
    with pytest.raises(ContactError, match="reading /dev/gpiochip0 failed: No such device"):
        source.read(0.01)
