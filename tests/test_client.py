"""The client: the scan, values with quality, events, scheduling and writes - driven through a
fake device, so these tests are never about Modbus itself."""
import asyncio
import logging
from collections.abc import Awaitable, Mapping, Sequence
from typing import TypeVar

import pytest

from src.modbus_event_connect import _client as client_module
from src.modbus_event_connect._client import Client, Status
from src.modbus_event_connect._errors import InvalidValueError
from src.modbus_event_connect._data_type import DataType
from src.modbus_event_connect._device import (
    EncodedWrite,
    Identity,
    Outcome,
    ProtocolOptions,
    ReadResult,
    WriteResult,
)
from src.modbus_event_connect._errors import (
    CannotConnectError,
    NotConnectedError,
    ReadOnlyError,
    UnsupportedDeviceError,
)
from src.modbus_event_connect.modbus._access import (
    Coil,
    HoldingRegister,
    InputRegister,
    ModbusOptions,
    plain,
)
from src.modbus_event_connect._model import Instances, Model, Scan, Section
from src.modbus_event_connect._point import (
    Change,
    Labels,
    Limits,
    Point,
    PollRate,
    Pulse,
    Refresh,
    WriteKind,
)
from src.modbus_event_connect.testing._clock import FakeClock
from src.modbus_event_connect._value import DataValue, Quality

T = TypeVar("T")

# ================================================================================ doubles


class FakeDevice:
    """Answers per key. A key with no answer reads MISSING, as an absent register does."""

    def __init__(self, registers: Mapping[str, tuple[int, ...]], *,
                 identity: Identity | None = None, reachable: bool = True) -> None:
        self.answers: dict[str, ReadResult] = {k: ReadResult(Outcome.OK, v) for k, v in registers.items()}
        self.identity: Identity | None = ({} if identity is None else identity) if reachable else None
        self.reads: list[tuple[str, ...]] = []
        self.writes: list[tuple[str, EncodedWrite]] = []
        self.write_outcomes: dict[str, Outcome] = {}
        self.write_raises: set[str] = set()
        self.configured: list[ProtocolOptions | None] = []
        self.connected = False
        self.delay = 0.0

    async def connect(self) -> Identity | None:
        if self.identity is None:
            return None
        self.connected = True
        return self.identity

    async def disconnect(self) -> None:
        self.connected = False

    def configure(self, options: ProtocolOptions | None) -> None:
        self.configured.append(options)

    async def read(self, points: Sequence[Point]) -> Mapping[str, ReadResult]:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.reads.append(tuple(p.key for p in points))
        missing = ReadResult(Outcome.MISSING, exception_code=2)
        return {p.key: self.answers.get(p.key, missing) for p in points}

    async def write(self, point: Point, value: EncodedWrite) -> WriteResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.writes.append((point.key, value))
        if point.key in self.write_raises:
            raise RuntimeError("the transport broke")
        outcome = self.write_outcomes.get(point.key, Outcome.OK)
        if outcome is Outcome.OK and value.registers:
            self.answers[point.key] = ReadResult(Outcome.OK, value.registers)
        return WriteResult(outcome)

    def diagnostics(self) -> Mapping[str, object]:
        return {}

    def read_keys(self) -> set[str]:
        return {key for batch in self.reads for key in batch}

    def answer(self, key: str, *registers: int) -> None:
        self.answers[key] = ReadResult(Outcome.OK, tuple(registers))

    def fail(self, key: str, outcome: Outcome) -> None:
        self.answers[key] = ReadResult(outcome, exception_code=4 if outcome is Outcome.OFFLINE else 0)


def _room(n: int) -> list[Point]:
    return [Point(f"room_{n}_temp", read=InputRegister(100 + n), data_type=DataType.INT16, scale=0.1)]


BASE = [
    Point("temp", read=InputRegister(1), data_type=DataType.INT16, scale=0.1, no_data=(0x7FFF,), deadband=0.5),
    Point("mode", read=HoldingRegister(2), write=HoldingRegister(2), limits=Limits(0, 4, step=1),
          on_write=Refresh(["fan_in", "fan_out"], after=2.0, until_stable=10.0)),
    Point("fan_in", read=InputRegister(3)),
    Point("fan_out", read=InputRegister(4)),
    Point("serial", read=InputRegister(10), data_type=DataType.UINT32, poll_rate=PollRate.STATIC),
    Point("alarm_summary", read=InputRegister(20), data_type=DataType.BOOL, poll_rate=PollRate.FAST,
          on_change=Refresh(Labels(kind="alarm"))),
    Point("alarm_1", read=InputRegister(21), data_type=DataType.BOOL, poll_rate=PollRate.STATIC, labels={"kind": "alarm"}),
    Point("alarm_2", read=InputRegister(22), data_type=DataType.BOOL, poll_rate=PollRate.STATIC, labels={"kind": "alarm"}),
    Point("counter", read=InputRegister(23), on_change=Refresh(["fan_in"], when=Change.RISING)),
    Point("step_up", write=HoldingRegister(30), write_kind=WriteKind.COMMAND),
    Point("relay", read=Coil(5), write=Coil(5), data_type=DataType.BOOL),
    Point("bell", write=Coil(6), data_type=DataType.BOOL, write_kind=WriteKind.COMMAND, pulse=Pulse(idle=False, after=0.01)),
    Point("name", read=HoldingRegister(40), write=HoldingRegister(40), data_type=DataType.string(2)),
]
COOLING = [Point("cooling_temp", read=InputRegister(50), data_type=DataType.INT16, scale=0.1)]
IDENTITY = [Point("hardware", read=InputRegister(0), poll_rate=PollRate.STATIC)]

REGISTERS: dict[str, tuple[int, ...]] = {
    "hardware": (1,), "temp": (215,), "mode": (1,), "fan_in": (30,), "fan_out": (31,),
    "serial": (0, 1234), "alarm_summary": (0,), "alarm_1": (0,), "alarm_2": (0,), "counter": (5,),
    "relay": (0,), "name": (0x4142, 0), "cooling_temp": (180,),
    "room_1_temp": (201,), "room_2_temp": (202,), "room_3_temp": (203,),
}


class Options(ProtocolOptions):
    """A stand-in for a device's model options."""


OPTIONS = Options()


async def _no_rooms_missing(context: Scan) -> None:
    """A scan step: a room whose temperature is missing is not installed."""
    for n in (1, 2, 3):
        found = await context.read(Labels(room=n))
        if all(v.quality is Quality.MISSING for v in found.values()):
            context.set_available(Labels(room=n), False, reason="room not installed")


def _has_cooling(identity: Identity) -> bool:
    hardware = identity.get("hardware")
    return isinstance(hardware, int) and hardware >= 2


MODEL = Model(
    name="TEST", manufacturer="TEST",
    identity_points=IDENTITY,
    sections=[Section(BASE), Section(COOLING, when=_has_cooling),
            Instances(_room, range(1, 4), label="room")],
    scan_steps=[_no_rooms_missing],
    options=OPTIONS, read_back_after=1.0
)


def _client(registers: Mapping[str, tuple[int, ...]] | None = None, *,
            read_only: bool = False) -> tuple[Client, FakeDevice, FakeClock]:
    device = FakeDevice(REGISTERS if registers is None else registers)
    clock = FakeClock()
    return Client(device, MODEL, clock=clock, read_only=read_only), device, clock


def _without(key: str) -> dict[str, tuple[int, ...]]:
    """The registers, less the one for `key`: the unit does not have it."""
    return {k: v for k, v in REGISTERS.items() if k != key}


def _connected(registers: Mapping[str, tuple[int, ...]] | None = None,
               read_only: bool = False) -> tuple[Client, FakeDevice, FakeClock]:
    client, device, clock = _client(registers, read_only=read_only)
    asyncio.run(client.connect())
    device.reads.clear()
    return client, device, clock


def within(seconds: float, awaitable: Awaitable[T]) -> T:
    """Run to completion or fail: a deadlock must fail the test, not hang the whole suite."""
    return asyncio.run(asyncio.wait_for(awaitable, seconds))  # type: ignore[arg-type]


class Recorder:
    """A subscriber that keeps what it is told."""

    def __init__(self) -> None:
        self.events: list[tuple[str | Status, DataValue | None, DataValue]] = []

    def __call__(self, key: str | Status, old: DataValue | None, new: DataValue) -> None:
        self.events.append((key, old, new))

    @property
    def values(self) -> list[object]:
        return [new.value for _, _, new in self.events]


# =================================================================================== scan

def test_an_unreachable_device_raises_cannot_connect() -> None:
    client = Client(FakeDevice(REGISTERS, reachable=False), MODEL, clock=FakeClock())
    with pytest.raises(CannotConnectError):
        asyncio.run(client.connect())
    assert client.status(Status.CONNECTED).value is False


def test_a_device_no_model_matches_raises_unsupported() -> None:
    client = Client(FakeDevice(REGISTERS), lambda identity: None, clock=FakeClock())
    with pytest.raises(UnsupportedDeviceError):
        asyncio.run(client.connect())


def test_the_selector_sees_the_handshake() -> None:
    seen: list[Identity] = []

    def select(identity: Identity) -> Model | None:
        seen.append(identity)
        return MODEL
    device = FakeDevice(REGISTERS, identity={"device_model": 1140})
    asyncio.run(Client(device, select, clock=FakeClock()).connect())
    assert seen and seen[0]["device_model"] == 1140


def test_the_protocol_is_given_the_models_options() -> None:
    client, device, _ = _client()
    asyncio.run(client.connect())
    assert device.configured == [OPTIONS]


def test_identity_points_decide_which_sections_exist() -> None:
    old, _, _ = _connected({**REGISTERS, "hardware": (1,)})
    new, _, _ = _connected({**REGISTERS, "hardware": (2,)})
    assert "cooling_temp" not in old.points
    assert "cooling_temp" in new.points


def test_every_point_has_a_value_when_connect_returns() -> None:
    client, _, _ = _connected()
    for key in client.points:
        if client.can_read(key):
            assert client.value(key) is not None, key
    assert client.status(Status.CONNECTED).value


def test_connect_reads_the_device_once() -> None:
    """A full read at connect must not look like a recovery, or every point becomes due again."""
    client, device, _ = _connected()
    for key in ("temp", "alarm_summary", "counter"):
        client.subscribe(key, Recorder())
    asyncio.run(client.poll())
    assert device.reads == [], "nothing is due right after connect"


def test_a_scan_step_removes_what_the_unit_does_not_have() -> None:
    registers = {k: v for k, v in REGISTERS.items() if k != "room_2_temp"}
    client, _, _ = _connected(registers)
    assert "room_2_temp" not in client.points
    assert client.instances("room") == (1, 3)
    assert client.unavailable_reasons["room_2_temp"] == "room not installed"


def test_a_scan_read_neither_stores_nor_notifies() -> None:
    seen: list[Mapping[str, DataValue]] = []

    async def peek(context: Scan) -> None:
        seen.append(await context.read(("temp",)))
    model = Model(name="T", manufacturer="T", sections=[Section(BASE)], scan_steps=[peek], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    device = FakeDevice(REGISTERS)
    client = Client(device, model, clock=FakeClock())
    recorder = Recorder()
    client.subscribe("temp", recorder)                       # allowed before connect
    asyncio.run(client.connect())
    assert seen[0]["temp"].value == 21.5
    assert len(recorder.events) == 1, "only the first full read notifies, not the step's question"


def test_a_register_missing_at_the_first_read_is_recorded_unavailable() -> None:
    registers = {k: v for k, v in REGISTERS.items() if k != "fan_out"}
    client, _, _ = _connected(registers)
    assert "fan_out" not in client.points
    assert not client.has("fan_out")
    assert "fan_out" in client.unavailable_reasons


def test_rescan_forgets_availability_and_keeps_interest() -> None:
    registers = {k: v for k, v in REGISTERS.items() if k != "fan_out"}
    client, device, _ = _connected(registers)
    recorder = Recorder()
    client.subscribe("temp", recorder)
    client.set_poll_interval(PollRate.SLOW, 30)
    device.answer("fan_out", 33)                            # the peripheral was plugged in

    asyncio.run(client.rescan())

    assert "fan_out" in client.points
    fan_out = client.value("fan_out")
    assert fan_out is not None and (fan_out.value, fan_out.quality) == (33, Quality.GOOD)
    assert client._scheduler is not None and client._scheduler.interval("temp") == 30
    assert recorder.events, "the subscription survived"


def test_nothing_works_before_connect() -> None:
    client, _, _ = _client()
    with pytest.raises(NotConnectedError):
        asyncio.run(client.poll())
    with pytest.raises(NotConnectedError):
        asyncio.run(client.write("mode", 2))
    with pytest.raises(NotConnectedError):
        _ = client.points


# ======================================================================== values, quality

@pytest.mark.parametrize("registers,value,quality", [
    ((215,), 21.5, Quality.GOOD),
    ((0x7FFF,), None, Quality.NO_DATA),
    ((0xFF9C,), -10.0, Quality.GOOD),
])
def test_a_read_becomes_a_value_with_its_quality(registers: tuple[int, ...], value: object,
                                                 quality: Quality) -> None:
    client, _, _ = _connected({**REGISTERS, "temp": registers})
    current = client.value("temp")
    assert current is not None and (current.value, current.quality) == (value, quality)


def test_an_offline_peripheral_is_offline_and_stays_polled() -> None:
    client, device, clock = _connected()
    client.subscribe("temp", Recorder())
    device.fail("temp", Outcome.OFFLINE)
    clock.advance(60)
    asyncio.run(client.poll())
    current = client.value("temp")
    assert current is not None and current.quality is Quality.OFFLINE
    assert client.has("temp"), "offline is not missing"


@pytest.mark.parametrize("outcome", [Outcome.NO_ANSWER, Outcome.BUSY, Outcome.ERROR])
def test_a_failed_read_keeps_the_last_good_value_as_stale(outcome: Outcome) -> None:
    client, device, clock = _connected()
    client.subscribe("temp", Recorder())
    good = client.value("temp")
    device.fail("temp", outcome)
    clock.advance(60)
    asyncio.run(client.poll())
    stale = client.value("temp")
    assert good is not None and stale is not None
    assert (stale.value, stale.quality, stale.timestamp) == (good.value, Quality.STALE, good.timestamp)
    assert client.consecutive_failures("temp") == 1


def test_consecutive_failures_are_counted_and_reset_by_success() -> None:
    client, device, clock = _connected()
    client.subscribe("temp", Recorder())
    device.fail("temp", Outcome.NO_ANSWER)
    for _ in range(3):
        clock.advance(60)
        asyncio.run(client.poll())
    assert client.consecutive_failures("temp") == 3
    device.answer("temp", 220)
    clock.advance(60)
    asyncio.run(client.poll())
    assert client.consecutive_failures("temp") == 0


def test_registers_that_do_not_fit_the_point_read_as_stale() -> None:
    client, device, _ = _connected()
    client.subscribe("serial", Recorder())
    device.answer("serial", 1)                               # U32 needs two registers
    asyncio.run(client.refresh(["serial"]))
    current = client.value("serial")
    assert current is not None and current.quality is Quality.STALE


# ================================================================================= events

def test_subscribing_delivers_the_current_value_at_once() -> None:
    client, _, _ = _connected()
    recorder = Recorder()
    client.subscribe("temp", recorder)
    assert recorder.values == [21.5]
    assert recorder.events[0][1] is None


def test_a_change_is_delivered_with_old_and_new() -> None:
    client, device, clock = _connected()
    recorder = Recorder()
    client.subscribe("fan_in", recorder)
    device.answer("fan_in", 40)
    clock.advance(60)
    asyncio.run(client.poll())
    _, old, new = recorder.events[-1]
    assert old is not None and (old.value, new.value) == (30, 40)


def test_an_unchanged_value_is_not_delivered_again() -> None:
    client, _, clock = _connected()
    recorder = Recorder()
    client.subscribe("fan_in", recorder)
    clock.advance(60)
    asyncio.run(client.poll())
    assert len(recorder.events) == 1


def test_a_quality_change_is_delivered_even_when_the_value_is_the_same() -> None:
    client, device, clock = _connected()
    recorder = Recorder()
    client.subscribe("fan_in", recorder)
    device.fail("fan_in", Outcome.NO_ANSWER)
    clock.advance(60)
    asyncio.run(client.poll())
    assert [new.quality for _, _, new in recorder.events] == [Quality.GOOD, Quality.STALE]


def test_the_deadband_suppresses_noise_but_not_drift() -> None:
    client, device, clock = _connected()                     # temp: deadband 0.5, starts 21.5
    recorder = Recorder()
    client.subscribe("temp", recorder)
    for raw in (216, 217, 218, 219, 220):                       # 21.6 ... 22.0 in 0.1 steps
        device.answer("temp", raw)
        clock.advance(60)
        asyncio.run(client.poll())
    assert recorder.values == [21.5, 22.0], "compared with the last value reported, not the last read"


def test_a_raising_subscriber_does_not_silence_the_others() -> None:
    client, _, _ = _connected()

    def broken(key: str, old: DataValue | None, new: DataValue) -> None:
        raise RuntimeError("consumer bug")
    recorder = Recorder()
    client.subscribe("temp", broken)
    client.subscribe("temp", recorder)
    assert recorder.values == [21.5]


def test_unsubscribing_stops_delivery_and_reading() -> None:
    client, device, clock = _connected()
    recorder = Recorder()
    unsubscribe = client.subscribe("fan_in", recorder)
    unsubscribe()
    device.answer("fan_in", 99)
    clock.advance(60)
    asyncio.run(client.poll())
    assert len(recorder.events) == 1
    assert "fan_in" not in device.read_keys()


def test_subscribing_to_an_unknown_key_after_connect_raises() -> None:
    client, _, _ = _connected()
    with pytest.raises(KeyError):
        client.subscribe("no_such_point", Recorder())


def test_a_status_is_subscribed_to_on_its_own() -> None:
    client, _, _ = _client()
    seen: list[tuple[Status, object]] = []
    client.subscribe_status(Status.CONNECTED, lambda status, old, new: seen.append((status, new.value)))
    asyncio.run(client.connect())
    assert seen == [(Status.CONNECTED, False), (Status.CONNECTED, True)]
    assert client.status(Status.CONNECTED).value is True


def test_a_point_named_like_a_status_is_an_ordinary_point() -> None:
    """A model's keys are its own: none is reserved for the client's status."""
    model = Model(name="TEST", manufacturer="TEST",
                  sections=[Section([Point("status:connected", read=InputRegister(1))])],
                  options=OPTIONS, read_back_after=1.0)
    client = Client(FakeDevice({"status:connected": (7,)}), model, clock=FakeClock())
    asyncio.run(client.connect())
    current = client.value("status:connected")
    assert current is not None and current.value == 7
    assert client.can_read("status:connected")


# ============================================================================= scheduling

def test_only_what_something_wants_is_read_on_a_timer() -> None:
    client, device, clock = _connected()
    client.subscribe("temp", Recorder())
    client.set_polling("fan_in")
    clock.advance(60)
    asyncio.run(client.poll())
    read = device.read_keys()
    assert {"temp", "fan_in"} <= read
    assert "fan_out" not in read, "nobody asked for it"
    assert "alarm_summary" in read, "a trigger source is read so its trigger can fire"


def test_a_passive_subscriber_listens_without_causing_reads() -> None:
    client, device, clock = _connected()
    recorder = Recorder()
    client.subscribe("fan_out", recorder, poll=False)
    clock.advance(60)
    asyncio.run(client.poll())
    assert "fan_out" not in device.read_keys()
    assert recorder.values == [31], "it still gets the value the first read produced"


def test_set_read_and_subscribe_are_independent_reasons() -> None:
    client, device, clock = _connected()
    unsubscribe = client.subscribe("fan_in", Recorder())
    client.set_polling("fan_in")
    unsubscribe()
    clock.advance(60)
    asyncio.run(client.poll())
    assert "fan_in" in device.read_keys()


def test_an_unavailable_point_is_never_read_whatever_wants_it() -> None:
    client, device, clock = _connected(_without("fan_in"))
    client.subscribe("fan_in", Recorder())
    client.set_polling("fan_in")
    clock.advance(60)
    asyncio.run(client.poll())
    assert "fan_in" not in device.read_keys()


def test_overlapping_refresh_calls_do_one_pass() -> None:
    client, device, clock = _connected()
    client.subscribe("temp", Recorder())
    device.delay = 0.02
    clock.advance(60)

    async def three_at_once() -> None:
        await asyncio.gather(client.poll(), client.poll(), client.poll())
    within(2, three_at_once())
    assert len(device.reads) == 1


def test_the_next_poll_is_counted_in_seconds_from_now() -> None:
    client, _, clock = _connected()
    asyncio.run(client.poll())
    wait = client.seconds_until_next_poll()
    assert wait is not None and wait > 0
    clock.advance(wait / 2)
    assert client.seconds_until_next_poll() == wait / 2
    clock.advance(wait)
    assert client.seconds_until_next_poll() == 0.0


def test_with_nothing_to_poll_there_is_no_next_poll() -> None:
    model = Model(name="TEST", manufacturer="TEST", sections=[
        Section([Point("serial", read=InputRegister(10), data_type=DataType.UINT32, poll_rate=PollRate.STATIC)])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    client = Client(FakeDevice({"serial": (0, 1)}), model, clock=FakeClock())
    asyncio.run(client.connect())
    assert client.seconds_until_next_poll() is None


def test_an_interval_override_is_used_and_kept() -> None:
    client, device, clock = _connected()
    client.subscribe("fan_in", Recorder())
    client.set_poll_interval("fan_in", 5)
    clock.advance(5)
    asyncio.run(client.poll())
    assert "fan_in" in device.read_keys()


@pytest.mark.parametrize("targets,expected", [
    (PollRate.STATIC, {"serial", "alarm_1", "alarm_2", "hardware"}),
    (Labels(kind="alarm"), {"alarm_1", "alarm_2"}),
    (["fan_out"], {"fan_out"}),
])
def test_an_explicit_refresh_reads_its_targets_even_unwanted(targets: object, expected: set[str]) -> None:
    client, device, _ = _connected()
    asyncio.run(client.refresh(targets))  # type: ignore[arg-type]
    assert expected <= device.read_keys()


def test_a_delayed_refresh_waits() -> None:
    client, device, clock = _connected()
    asyncio.run(client.refresh(["fan_out"], after=3))
    assert "fan_out" not in device.read_keys()
    clock.advance(3)
    asyncio.run(client.poll())
    assert "fan_out" in device.read_keys()


def test_a_trigger_reads_its_targets_when_it_changes() -> None:
    client, device, clock = _connected()
    for key in ("alarm_1", "alarm_2"):
        client.subscribe(key, Recorder())                       # STATIC: never on a timer
    device.answer("alarm_summary", 1)
    clock.advance(10)
    asyncio.run(client.poll())                           # the summary changes
    device.reads.clear()
    asyncio.run(client.poll())                           # the alarms follow
    assert {"alarm_1", "alarm_2"} <= device.read_keys()


def test_a_trigger_does_not_fire_without_a_change() -> None:
    client, device, clock = _connected()
    client.subscribe("alarm_1", Recorder())
    clock.advance(10)
    asyncio.run(client.poll())
    device.reads.clear()
    asyncio.run(client.poll())
    assert "alarm_1" not in device.read_keys()


@pytest.mark.parametrize("new,fires", [(6, True), (4, False)])
def test_a_rising_trigger_ignores_a_fall(new: int, fires: bool) -> None:
    client, device, clock = _connected()                     # counter starts at 5
    client.subscribe("fan_in", Recorder())
    device.answer("counter", new)
    clock.advance(60)
    asyncio.run(client.poll())
    device.reads.clear()
    asyncio.run(client.poll())
    assert ("fan_in" in device.read_keys()) is fires


def test_after_an_outage_everything_wanted_is_read_once_again() -> None:
    """Values that stood still during the outage are due at once; the ones just read are not."""
    client, device, clock = _connected()
    for key in ("temp", "fan_in", "serial"):
        client.subscribe(key, Recorder())
    status = Recorder()
    client.subscribe_status(Status.CONNECTED, status)
    _silence(device)
    clock.advance(60)
    asyncio.run(client.poll())
    assert client.status(Status.CONNECTED).value is False

    for key, registers in REGISTERS.items():
        device.answer(key, *registers)
    clock.advance(60)
    device.reads.clear()
    asyncio.run(client.poll())                       # the recovery: temp and fan_in were due
    recovery = device.read_keys()
    device.reads.clear()
    asyncio.run(client.poll())
    assert client.status(Status.CONNECTED).value is True
    assert status.values == [True, False, True]
    assert {"temp", "fan_in"} <= recovery
    assert device.read_keys() == {"serial"}, "a static value was not re-read, or a fresh one was read twice"


# ================================================================================= writes

def test_a_read_only_client_sends_nothing() -> None:
    client, device, _ = _connected(read_only=True)
    with pytest.raises(ReadOnlyError):
        asyncio.run(client.write("mode", 2))
    with pytest.raises(ReadOnlyError):
        asyncio.run(client.write_sequence([("mode", 2)]))
    assert device.writes == []


@pytest.mark.parametrize("key,value,error", [
    ("no_such_point", 1, KeyError),
    ("temp", 1, ValueError),                     # read-only
    ("mode", 5, InvalidValueError),                     # above limits
    ("mode", 1.5, InvalidValueError),                   # off the step
    ("mode", True, InvalidValueError),                  # a bool is not a number
])
def test_a_bad_write_is_refused_before_anything_is_sent(key: str, value: object,
                                                        error: type[Exception]) -> None:
    client, device, _ = _connected()
    with pytest.raises(error):
        asyncio.run(client.write(key, value))  # type: ignore[arg-type]
    assert device.writes == []


def test_a_write_is_read_back_after_its_delay() -> None:
    client, device, clock = _connected()
    client.subscribe("relay", Recorder())
    assert asyncio.run(client.write("relay", True)) is True
    asyncio.run(client.poll())
    assert "relay" not in device.read_keys()
    clock.advance(MODEL.read_back_after)
    asyncio.run(client.poll())
    relay = client.value("relay")
    assert relay is not None and relay.value is True


def test_what_the_scan_read_is_not_read_again() -> None:
    async def first(scan: Scan) -> None:
        await scan.read(["a", "gone"])

    async def second(scan: Scan) -> None:
        found = await scan.read(["a", "b"])
        assert found["a"].value == 1, "a later step gets what an earlier one read"
    points = [Point(key, read=InputRegister(n)) for n, key in enumerate(("a", "b", "c", "gone"), 1)]
    model = Model(name="S", manufacturer="S", sections=[Section(points)], scan_steps=[first, second],
                  options=OPTIONS, read_back_after=1.0)
    device = FakeDevice({"a": (1,), "b": (2,), "c": (3,)})
    client = Client(device, model, clock=FakeClock())
    asyncio.run(client.connect())
    assert sorted(key for batch in device.reads for key in batch) == ["a", "b", "c", "gone"]
    assert {key: value.value for key, value in client.values.items()} == {"a": 1, "b": 2, "c": 3}
    assert "gone" in client.unavailable_reasons


def test_values_holds_only_keys_the_unit_has() -> None:
    registers = {k: v for k, v in REGISTERS.items() if k != "counter"}
    client, _, _ = _connected(registers)
    assert "counter" not in client.points and "counter" not in client.values
    missing = client.value("counter")
    assert missing is not None and missing.quality is Quality.MISSING
    assert set(client.values) <= set(client.points)


def test_a_point_with_its_own_read_back_delay_is_read_back_after_it() -> None:
    slow = Point("slow", read=HoldingRegister(60), write=HoldingRegister(60), read_back_after=5.0)
    model = Model(name="S", manufacturer="S", sections=[Section([slow])], options=OPTIONS, read_back_after=1.0)
    device, clock = FakeDevice({"slow": (0,)}), FakeClock()
    client = Client(device, model, clock=clock)
    asyncio.run(client.connect())
    client.subscribe("slow", Recorder())
    asyncio.run(client.write("slow", 7))
    device.reads.clear()
    clock.advance(1.0)
    asyncio.run(client.poll())
    assert "slow" not in device.read_keys(), "read back at the device's delay, not the point's"
    clock.advance(4.0)
    asyncio.run(client.poll())
    assert "slow" in device.read_keys()


def test_a_write_rereads_what_it_disturbs_until_it_settles() -> None:
    client, device, clock = _connected()
    for key in ("mode", "fan_in", "fan_out"):
        client.subscribe(key, Recorder())
    asyncio.run(client.write("mode", 3))
    reads: list[int] = []
    for ramp in (40, 50, 60, 60):                               # the fan ramps, then holds
        device.answer("fan_in", ramp)
        clock.advance(2.0)
        device.reads.clear()
        asyncio.run(client.poll())
        reads.append(int("fan_in" in device.read_keys()))
    clock.advance(2.0)
    device.reads.clear()
    asyncio.run(client.poll())
    assert reads == [1, 1, 1, 1]
    assert "fan_in" not in device.read_keys(), "stable, so following stopped"


def test_a_refused_write_returns_false() -> None:
    client, device, _ = _connected()
    device.write_outcomes["mode"] = Outcome.BUSY
    assert asyncio.run(client.write("mode", 2)) is False


def test_settings_asked_for_quickly_collapse_to_the_newest() -> None:
    """Four taps while the first is on the wire: only the first and the last are actually sent."""
    client, device, _ = _connected()
    device.delay = 0.01

    async def taps() -> list[bool]:
        return list(await asyncio.gather(*(client.write("mode", v) for v in (1, 2, 3, 4))))
    results = within(2, taps())
    sent = [value.registers[0] for key, value in device.writes if key == "mode"]
    assert sent == [1, 4]
    assert results == [True] * 4, "every caller learns the outcome of the write that stood in for it"


def test_an_overtaken_write_shares_the_failure_of_the_one_that_overtook_it() -> None:
    client, device, _ = _connected()
    device.delay = 0.01
    device.write_outcomes["mode"] = Outcome.BUSY

    async def taps() -> list[bool]:
        return list(await asyncio.gather(*(client.write("mode", v) for v in (1, 2, 3))))
    assert within(2, taps()) == [False, False, False]


def test_a_write_that_raises_reaches_every_caller_it_stood_in_for() -> None:
    client, device, _ = _connected()
    device.delay = 0.01
    device.write_raises.add("mode")

    async def taps() -> list[object]:
        return list(await asyncio.gather(*(client.write("mode", v) for v in (1, 2, 3)),
                                         return_exceptions=True))
    results = within(2, taps())
    assert all(isinstance(r, RuntimeError) for r in results), results
    assert client.status(Status.WRITE_PENDING).value is False, "a failed write left the user interface disabled"


def test_every_command_is_sent_in_order() -> None:
    client, device, _ = _connected()
    device.delay = 0.01

    async def presses() -> None:
        await asyncio.gather(*(client.write("step_up", 1) for _ in range(3)))
    within(2, presses())
    assert [key for key, _ in device.writes] == ["step_up"] * 3


def test_write_pending_brackets_the_whole_operation() -> None:
    client, device, _ = _connected()
    device.delay = 0.01
    recorder = Recorder()
    client.subscribe_status(Status.WRITE_PENDING, recorder)
    asyncio.run(client.write("mode", 2))
    assert recorder.values == [False, True, False]


def test_a_write_sequence_runs_in_order_and_stops_at_a_refusal() -> None:
    client, device, _ = _connected()
    device.write_outcomes["relay"] = Outcome.ERROR
    ok = asyncio.run(client.write_sequence([("mode", 2), ("relay", True), ("name", "AB")]))
    assert ok is False
    assert [key for key, _ in device.writes] == ["mode", "relay"]


def test_a_write_sequence_checks_every_value_before_sending_any() -> None:
    client, device, _ = _connected()
    with pytest.raises(InvalidValueError):
        asyncio.run(client.write_sequence([("mode", 2), ("mode", 9)]))
    assert device.writes == []


def test_a_pulse_returns_to_idle_by_itself() -> None:
    """Exactly one press and one release; the release must not itself start a new pulse."""
    client, device, _ = _connected()

    async def press_and_wait() -> None:
        await client.write("bell", True)
        await asyncio.sleep(0.05)
    asyncio.run(press_and_wait())
    assert [(key, value.registers) for key, value in device.writes] == [("bell", (1,)), ("bell", (0,))]


def test_disconnect_cancels_a_pending_pulse() -> None:
    client, device, _ = _connected()

    async def press_and_leave() -> None:
        await client.write("bell", True)
        await client.disconnect()
        await asyncio.sleep(0.05)
    asyncio.run(press_and_leave())
    assert [value.registers for _, value in device.writes] == [(1,)]
    assert client.status(Status.CONNECTED).value is False


# ===================================================== regression scenarios

def test_two_clients_share_nothing() -> None:
    first, first_protocol, first_clock = _connected(_without("fan_out"))
    second, second_protocol, second_clock = _connected()
    first.subscribe("fan_in", Recorder())
    for clock in (first_clock, second_clock):
        clock.advance(60)
    asyncio.run(first.poll())
    asyncio.run(second.poll())
    assert "fan_in" in first_protocol.read_keys()
    assert "fan_in" not in second_protocol.read_keys()
    assert not first.has("fan_out") and second.has("fan_out")


def test_overlapping_writes_keep_write_pending_until_the_last_finishes() -> None:
    client, device, _ = _connected()
    device.delay = 0.01
    recorder = Recorder()
    client.subscribe_status(Status.WRITE_PENDING, recorder)

    async def two_writes() -> None:
        await asyncio.gather(client.write("mode", 2), client.write("relay", True))
    within(2, two_writes())
    assert recorder.values == [False, True, False], "cleared while the second write was still going"


def test_writes_to_different_keys_are_never_merged() -> None:
    client, device, _ = _connected()
    device.delay = 0.01

    async def two_keys() -> None:
        await asyncio.gather(client.write("mode", 2), client.write("relay", True), client.write("mode", 3))
    within(2, two_keys())
    assert {key for key, _ in device.writes} == {"mode", "relay"}


def test_set_read_before_connect_applies_once_connected() -> None:
    client, device, clock = _client()
    client.set_polling("fan_out")
    asyncio.run(client.connect())
    device.reads.clear()
    clock.advance(60)
    asyncio.run(client.poll())
    assert "fan_out" in device.read_keys()


def test_an_unavailable_trigger_source_is_not_read_either() -> None:
    """A trigger source is read without anyone asking - unless the unit does not have it."""
    client, device, clock = _connected(_without("alarm_summary"))
    clock.advance(60)
    asyncio.run(client.poll())
    assert "alarm_summary" not in device.read_keys()


def test_a_point_found_by_a_rescan_is_read_again() -> None:
    client, device, clock = _connected(_without("fan_in"))
    client.subscribe("fan_in", Recorder())
    clock.advance(60)
    asyncio.run(client.poll())
    assert "fan_in" not in device.read_keys()

    device.answers["fan_in"] = ReadResult(Outcome.OK, REGISTERS["fan_in"])
    asyncio.run(client.rescan())
    device.reads.clear()
    clock.advance(60)
    asyncio.run(client.poll())
    assert "fan_in" in device.read_keys(), "the subscription was kept while it was unavailable"


# ======================================================================== reachability

def _silence(device: FakeDevice, outcome: Outcome = Outcome.NO_ANSWER) -> None:
    for key in list(device.answers):
        device.fail(key, outcome)


class LogRecords(logging.Handler):
    """Keeps the client's log records for the duration of a `with` block."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def __enter__(self) -> "LogRecords":
        logger = logging.getLogger(client_module.__name__)
        self._previous = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(self)
        return self

    def __exit__(self, *_: object) -> None:
        logger = logging.getLogger(client_module.__name__)
        logger.removeHandler(self)
        logger.setLevel(self._previous)


def test_reachability_follows_the_answers_not_the_socket() -> None:
    """A pulled cable leaves a TCP socket open; only the missing answers tell."""
    client, device, clock = _connected()
    client.subscribe("temp", Recorder())
    _silence(device)
    assert device.connected is True
    clock.advance(60)
    asyncio.run(client.poll())
    assert client.status(Status.CONNECTED).value is False


def test_an_outage_and_its_recovery_are_logged_once_each() -> None:
    client, device, clock = _connected()
    client.subscribe("temp", Recorder())
    with LogRecords() as records:
        _silence(device)
        for _ in range(3):
            clock.advance(60)
            asyncio.run(client.poll())
        device.answer("temp", 215)
        for _ in range(3):
            clock.advance(60)
            asyncio.run(client.poll())
    assert [m for m in records.messages if "answer" in m] == ["TEST stopped answering", "TEST answers again"]


def test_a_write_left_unanswered_marks_the_device_unreachable_and_an_answered_one_reachable() -> None:
    client, device, _ = _connected()
    device.write_outcomes["mode"] = Outcome.NO_ANSWER
    asyncio.run(client.write("mode", 2))
    assert client.status(Status.CONNECTED).value is False
    device.write_outcomes["mode"] = Outcome.OK
    asyncio.run(client.write("mode", 2))
    assert client.status(Status.CONNECTED).value is True


@pytest.mark.parametrize("outcome", [Outcome.NO_ANSWER, Outcome.BUSY])
@pytest.mark.parametrize("key", ["hardware", "room_2_temp", "fan_out"],
                         ids=["identity", "scan-step", "first-read"])
def test_connect_commits_nothing_unless_every_scan_read_is_answered(key: str, outcome: Outcome) -> None:
    """An unanswered read must never be reported as a missing register."""
    client, device, _ = _client()
    device.fail(key, outcome)
    with pytest.raises(CannotConnectError):
        asyncio.run(client.connect())
    with pytest.raises(NotConnectedError):
        _ = client.points
    assert device.connected is False, "the connection opened for the scan was left open"


def test_a_failed_connect_on_a_silent_device_reports_it_unreachable() -> None:
    client, device, _ = _client()
    _silence(device)
    with pytest.raises(CannotConnectError):
        asyncio.run(client.connect())
    assert client.status(Status.CONNECTED).value is False


def test_rescan_on_a_silent_device_keeps_the_picture() -> None:
    registers = {k: v for k, v in REGISTERS.items() if k != "room_2_temp"}
    client, device, _ = _connected(registers)
    keys, unavailable = tuple(client.points), client.unavailable_reasons
    _silence(device)
    with pytest.raises(CannotConnectError):
        asyncio.run(client.rescan())
    assert tuple(client.points) == keys, "a silent device would have made room 2 appear"
    assert client.unavailable_reasons == unavailable
    assert client.status(Status.CONNECTED).value is False
