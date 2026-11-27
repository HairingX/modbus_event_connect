"""Mapping a unit: the areas its model's points span, found and read as raw words by the client."""
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from event_connect import (
    Area,
    CannotConnectError,
    Fixed,
    Key,
    Level,
    NotConnectedError,
    PollRate,
    RawAddress,
    RegisterChange,
    difference,
)
from event_connect.testing import FakeClock
from modbus_event_connect import Section, Snapshot
from modbus_event_connect._client import Client
from modbus_event_connect._data_type import DataType
from modbus_event_connect._device import Outcome, ReadResult
from modbus_event_connect._mapping import areas
from modbus_event_connect._point import Access, Point
from modbus_event_connect.micro_nabto import (
    RereadZeros,
    DatapointRegister,
    MicroNabtoConnection,
    MicroNabtoDevice,
    SetpointRegister,
)
from modbus_event_connect.modbus import (
    Coil,
    DiscreteInput,
    ExceptionCode,
    FunctionCode,
    HoldingRegister,
    InputRegister,
    ModbusDevice,
    ModbusModel,
    NumberRange,
    RegisterNumbering,
    modicon,
    plain,
)
from modbus_event_connect.testing import (
    NO_ANSWER,
    SimulatedMicroNabtoDevice,
    SimulatedModbusDevice,
    SimulatedModbusGateway,
)
from models import (
    HANDSHAKE_NEVER_AGAIN,
    NEVER_AGAIN,
    OFFLINE_AFTER_INTERVALS,
    TIMEOUT_MARGIN,
    micro_nabto_model,
    modbus_model,
)

EMAIL = "user@example.invalid"


@dataclass(frozen=True)
class _Other(Access):
    """An access of a protocol that does not name its space."""
DATAPOINT_READ, SETPOINT_READ, SETPOINT_WRITE = 0x2D, 0x2A, 0x2B
LOW, HIGH, FLAG = Key("low", int), Key("high", int), Key("flag", bool)


def _model(*, max_registers: int = 8, max_bits: int = 2000) -> ModbusModel:
    """Points on input registers 10 and 29 - a span of 20 - and on holding register 5."""
    return modbus_model([Section([
        Point(LOW, read=InputRegister(10), poll_rate=PollRate.FAST),
        Point(HIGH, read=InputRegister(28), data_type=DataType.UINT32, poll_rate=PollRate.FAST),
        Point(Key("setting", int), read=HoldingRegister(5), write=HoldingRegister(5), poll_rate=PollRate.FAST),
    ])], max_registers=max_registers, max_bits=max_bits)


def _unit(*, inputs: Sequence[int] = (), holding: Sequence[int] = (5,)) -> SimulatedModbusDevice:
    """Input registers 10-29, less 15 and 16, and those given; each holds its address plus 100."""
    present = [*(a for a in range(10, 30) if a not in (15, 16)), *inputs]
    return SimulatedModbusDevice(input_registers={a: 100 + a for a in present},
                                 holding_registers={a: 100 + a for a in holding})


async def _connected(model: ModbusModel, unit: SimulatedModbusDevice) -> Client:
    clock = FakeClock(autojump=True)
    device = ModbusDevice(SimulatedModbusGateway({1: unit}), unit_id=1, clock=clock, reconnect=NEVER_AGAIN,
                          models=[model], timeout_margin=TIMEOUT_MARGIN)
    client = Client(device, model, offline_after_intervals=OFFLINE_AFTER_INTERVALS, level=Level.DOCUMENTED,
                    clock=clock, read_only=True)
    await client.connect()
    unit.requests.clear()
    return client


def _reads(unit: SimulatedModbusDevice) -> list[tuple[FunctionCode, int, int]]:
    return [(r.function, r.address, r.count) for r in unit.requests]


def ir(number: int) -> RawAddress:
    return RawAddress("input", number)


# ================================================================================= areas


def test_each_access_names_its_space() -> None:
    assert [access.space for access in (InputRegister(0), HoldingRegister(0), Coil(0), DiscreteInput(0),
                                        DatapointRegister(0), SetpointRegister(0), DatapointRegister(0, obj=2))] == [
        "input", "holding", "coil", "discrete_input", "datapoint", "setpoint", "datapoint:2"]
    assert (SetpointRegister(0, obj=3).space, _Other(0).space) == ("setpoint:3", "_Other")


def test_the_areas_run_from_0_in_each_space_the_points_read_or_write_up_to_the_limit() -> None:
    assert areas(_model(), 300) == (Area("input", 0, 300), Area("holding", 0, 300))


def test_the_areas_reach_the_highest_address_a_point_reads_where_it_is_above_the_limit() -> None:
    assert areas(_model(), 1) == (Area("input", 0, 30), Area("holding", 0, 6))


def test_a_space_starts_at_the_lowest_number_its_numbering_gives_and_skips_numbers_without_an_address() -> None:
    def model(numbering: RegisterNumbering, number: int) -> ModbusModel:
        return modbus_model([Section([Point(LOW, read=InputRegister(number), poll_rate=PollRate.FAST)])],
                            numbering=numbering)

    assert areas(model(plain(first_address=0), 1), 300) == (Area("input", 1, 300),)
    assert areas(model(modicon(digits=5, first_address=0), 30005), 300) == (Area("input", 30001, 300),)
    gapped = RegisterNumbering(input_registers=[NumberRange(20, 30, address=100), NumberRange(1, 10, address=0)])
    assert areas(model(gapped, 5), 300) == (Area("input", 1, 10), Area("input", 20, 11))
    assert micro_nabto_model().numbers(SetpointRegister(0)) == (range(0, 0x10000),)


def test_a_micro_nabto_unit_is_mapped_in_both_its_spaces_whatever_its_points_are_in() -> None:
    only_datapoints = micro_nabto_model([Section([Point(LOW, read=DatapointRegister(320), poll_rate=PollRate.FAST)])])
    assert areas(only_datapoints, 300) == (Area("datapoint", 0, 321), Area("setpoint", 0, 300))
    assert areas(micro_nabto_model(), 300) == (Area("datapoint", 0, 300), Area("setpoint", 0, 300))


def test_a_modbus_unit_is_mapped_only_in_the_spaces_its_points_are_in() -> None:
    assert _model().mapped_spaces() == ()
    assert areas(modbus_model(), 300) == ()


async def test_a_micro_nabto_unit_with_no_points_is_mapped_in_both_its_spaces() -> None:
    model = micro_nabto_model()
    async with SimulatedMicroNabtoDevice(emails=frozenset({EMAIL}), datapoint_registers={(0, 0): 15, (0, 299): 3},
                                         setpoint_registers={(0, 5): 59}) as simulated:
        host, port = simulated.address
        device = MicroNabtoDevice(MicroNabtoConnection(EMAIL, host=host, port=port, timeout=0.05, retries=1,
                                                       answer_wait=0.5, reconnect=HANDSHAKE_NEVER_AGAIN,
                                                       session_idle=12.0, rediscovery_timeout=0.1,
                                                       rediscovery_resend=0.5),
                                  models=[model], owns_connection=True)
        client = Client(device, model, offline_after_intervals=OFFLINE_AFTER_INTERVALS, level=Level.DOCUMENTED,
                        read_only=True)
        await client.connect()
        found = await client.map_areas(limit=300)
        await client.disconnect()
    assert {str(address): word for address, word in found.values.items()} == {
        "datapoint 0": 15, "datapoint 299": 3, "setpoint 5": 59}
    assert not simulated.received(SETPOINT_WRITE)


@pytest.mark.parametrize(("numbering", "number", "image"), [
    (plain(first_address=0), 1, {0: 1, 5: 2, 299: 3}),
    (modicon(digits=5, first_address=0), 30005, {4: 1, 150: 2, 299: 3}),
    (RegisterNumbering(input_registers=[NumberRange(1, 10, address=0), NumberRange(20, 30, address=100)]), 5,
     {4: 1, 100: 2, 110: 3}),
], ids=["numbered-from-1", "modicon", "with-a-gap"])
async def test_a_unit_numbered_otherwise_is_mapped_completely_from_its_lowest_number(
        numbering: RegisterNumbering, number: int, image: dict[int, int]) -> None:
    model = modbus_model([Section([Point(LOW, read=InputRegister(number), poll_rate=PollRate.FAST)])],
                         numbering=numbering, max_registers=8)
    unit = SimulatedModbusDevice(input_registers=image)
    client = await _connected(model, unit)
    snapshot = await client.map_areas(limit=300)
    expected = {a for area in areas(model, 300) for a in area.addresses()}
    assert set(snapshot.values) | snapshot.missing == expected
    assert sorted(snapshot.values.values()) == sorted(image.values())


def test_the_limit_is_at_least_one_address() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        areas(_model(), 0)


def test_a_bit_space_spans_one_address_a_point() -> None:
    model = modbus_model([Section([Point(FLAG, read=Coil(3), data_type=DataType.BOOL, poll_rate=PollRate.FAST),
                                   Point(Key("other", bool), read=Coil(7), data_type=DataType.BOOL,
                                         poll_rate=PollRate.FAST)])])
    assert areas(model, 1) == (Area("coil", 0, 8),)


async def test_a_bit_space_is_mapped_as_bits_and_a_point_only_written_widens_the_span() -> None:
    model = modbus_model([Section([
        Point(FLAG, read=Coil(3), data_type=DataType.BOOL, poll_rate=PollRate.FAST),
        Point(Key("other", bool), read=Coil(5), data_type=DataType.BOOL, poll_rate=PollRate.FAST),
        Point(Key("reset", bool), write=Coil(9), data_type=DataType.BOOL)])], bit_write=None)
    unit = SimulatedModbusDevice(coils={a: a % 2 for a in range(10)})
    client = await _connected(model, unit)
    snapshot = await client.map_areas(limit=1)
    assert dict(snapshot.values) == {RawAddress("coil", a): a % 2 for a in range(10)}
    assert _reads(unit) == [(FunctionCode.READ_COILS, 0, 10)]


def test_a_model_reads_as_many_registers_or_bits_in_one_request_as_it_states() -> None:
    model = _model(max_registers=8, max_bits=16)
    assert (model.read_limit(InputRegister(0)), model.read_limit(Coil(0))) == (8, 16)
    assert micro_nabto_model(max_registers=12).read_limit(SetpointRegister(0)) == 12


async def test_the_areas_are_known_only_once_the_unit_is_scanned() -> None:
    model = _model()
    device = ModbusDevice(SimulatedModbusGateway({1: _unit()}), unit_id=1, reconnect=NEVER_AGAIN, models=[model],
                          timeout_margin=TIMEOUT_MARGIN)
    client = Client(device, model, offline_after_intervals=OFFLINE_AFTER_INTERVALS, level=Level.DOCUMENTED)
    with pytest.raises(NotConnectedError):
        client.mapping_areas(limit=300)
    with pytest.raises(NotConnectedError):
        await client.map_areas(limit=300)
    with pytest.raises(NotConnectedError):
        await client.read_mapped(Snapshot(FakeClock().now(), {}, []))
    unit = _unit()
    connected = await _connected(model, unit)
    assert connected.mapping_areas(limit=30) == areas(model, 30)


# ============================================================================== mapping


async def test_mapping_reads_every_address_from_0_and_finds_the_absent_ones() -> None:
    unit = _unit()
    client = await _connected(_model(), unit)
    snapshot = await client.map_areas(limit=1)
    assert dict(snapshot.values) == {**{ir(a): 100 + a for a in range(10, 30) if a not in (15, 16)},
                                     RawAddress("holding", 5): 105}
    assert snapshot.missing == frozenset({*(ir(a) for a in (*range(10), 15, 16)),
                                          *(RawAddress("holding", a) for a in range(5))})
    assert all(function.is_read for function, _, _ in _reads(unit))


async def test_after_an_absent_register_the_read_goes_on_one_at_a_time_doubling_while_it_answers() -> None:
    unit = _unit(inputs=range(10))
    client = await _connected(_model(max_registers=8), unit)
    await client.map_areas(limit=1)
    inputs = [(address, count) for function, address, count in _reads(unit)
              if function is FunctionCode.READ_INPUT_REGISTERS]
    # 0-7 answers whole; 8-15 is refused and halved down to 15; then 16 alone, absent too, and 17
    # alone, 18-19, 20-23 and 24-29, doubling while each answers.
    assert inputs == [(0, 8), (8, 8), (8, 4), (12, 4), (12, 2), (14, 2), (14, 1), (15, 1),
                      (16, 1), (17, 1), (18, 2), (20, 4), (24, 6)]


async def test_a_space_that_answers_whole_costs_one_request_of_the_models_size_each() -> None:
    unit = _unit(inputs=(*range(10), 15, 16))
    client = await _connected(_model(max_registers=8), unit)
    await client.map_areas(limit=1)
    assert [(a, c) for f, a, c in _reads(unit) if f is FunctionCode.READ_INPUT_REGISTERS] == [
        (0, 8), (8, 8), (16, 8), (24, 6)]


SPARSE = {
    "lone registers between long absent runs": {205: 1, 299: 2},
    "runs at the very start and the very end": {**{a: 1 for a in range(6)}, **{a: 2 for a in range(290, 300)}},
    "every other address": {a: a for a in range(0, 300, 2)},
    "nothing at all": {},
}
"""Holding registers a unit has besides its point's, at holding 5: each layout a read could miss."""


@pytest.mark.parametrize("holding", list(SPARSE.values()), ids=list(SPARSE))
async def test_every_address_up_to_the_limit_is_read_or_found_missing_and_nothing_is_missed(
        holding: dict[int, int]) -> None:
    unit = SimulatedModbusDevice(input_registers={10: 7, 350: 9}, holding_registers={5: 3, **holding})
    model = modbus_model([Section([
        Point(LOW, read=InputRegister(10), poll_rate=PollRate.FAST),
        Point(HIGH, read=InputRegister(350), poll_rate=PollRate.FAST),
        Point(Key("setting", int), read=HoldingRegister(5), write=HoldingRegister(5), poll_rate=PollRate.FAST),
    ])], max_registers=8)
    client = await _connected(model, unit)
    snapshot = await client.map_areas(limit=300)
    for space, top, image in (("input", 351, unit.input_registers), ("holding", 300, unit.holding_registers)):
        for number in range(top):
            address = RawAddress(space, number)
            assert (address in snapshot.values) != (address in snapshot.missing), address
            assert snapshot.values.get(address) == image.get(number), address
    assert len(snapshot.values) + len(snapshot.missing) == 351 + 300


async def test_a_later_read_asks_for_exactly_the_addresses_that_answered() -> None:
    unit = SimulatedModbusDevice(input_registers={10: 7, 11: 8, 205: 1, 299: 2},
                                 holding_registers={5: 3, **{a: a for a in range(0, 300, 2)}})
    client = await _connected(_model(max_registers=8), unit)
    found = await client.map_areas(limit=300)
    unit.requests.clear()
    await client.read_mapped(found)
    asked = {RawAddress("input" if f is FunctionCode.READ_INPUT_REGISTERS else "holding", a)
             for f, start, count in _reads(unit) for a in range(start, start + count)}
    assert asked == set(found.values)


async def test_a_mapped_unit_is_read_again_only_where_it_answered_in_requests_of_the_models_size() -> None:
    unit = _unit()
    client = await _connected(_model(max_registers=8), unit)
    found = await client.map_areas(limit=1)
    unit.requests.clear()
    unit.input_registers[12] = 7
    del unit.input_registers[13]
    again = await client.read_mapped(found)
    # 10-14 and 17-19, then 20-27, then 28-29, and holding 5; 10-14 refused for 13, and halved.
    assert [(a, c) for _, a, c in _reads(unit)][:3] == [(10, 5), (10, 2), (12, 3)]
    assert {(a, c) for _, a, c in _reads(unit)} >= {(17, 3), (20, 8), (28, 2), (5, 1)}
    assert not {a for _, a, c in _reads(unit) if c == 1} & {15, 16}
    assert ir(13) in again.missing
    assert difference(found, again)[:2] == (RegisterChange(ir(12), 112, 7, (0, 1, 2, 4, 5, 6)),
                                            RegisterChange(ir(13), 113, None, ()))
    assert not again.missing - {ir(13)}


async def test_a_register_of_a_space_the_model_does_not_read_is_not_read_again() -> None:
    client = await _connected(_model(), _unit())
    with pytest.raises(ValueError, match="reads no space 'datapoint'"):
        await client.read_mapped(Snapshot(FakeClock().now(), {RawAddress("datapoint", 1): 0}, []))


async def test_nothing_is_read_for_a_mapping_unless_it_is_asked_for() -> None:
    unit = _unit()
    client = await _connected(_model(), unit)
    client.set_polling(LOW)
    for _ in range(5):
        await client.refresh([LOW])
    assert set(_reads(unit)) == {(FunctionCode.READ_INPUT_REGISTERS, 10, 1)}


async def test_a_busy_answer_after_its_tries_stops_the_mapping() -> None:
    unit = _unit()
    client = await _connected(_model(), unit)
    unit.busy_for = 99
    with pytest.raises(CannotConnectError, match="busy"):
        await client.map_areas(limit=1)
    assert len(unit.requests) == 5               # the model's busy retry: 5 attempts, then given up


async def test_a_request_left_unanswered_stops_the_mapping_at_once() -> None:
    unit = _unit()
    client = await _connected(_model(), unit)
    unit.faults[(FunctionCode.READ_INPUT_REGISTERS, 0)] = NO_ANSWER
    with pytest.raises(CannotConnectError, match="no_answer"):
        await client.map_areas(limit=300)
    assert len(unit.requests) == 1


async def test_an_answer_that_cannot_be_read_stops_the_reading_again() -> None:
    unit = _unit()
    client = await _connected(_model(), unit)
    found = await client.map_areas(limit=1)
    unit.faults[(FunctionCode.READ_INPUT_REGISTERS, 10)] = ExceptionCode.ILLEGAL_DATA_VALUE
    with pytest.raises(CannotConnectError, match="error, unit 1 READ_INPUT_REGISTERS 10"):
        await client.read_mapped(found)


async def test_a_mapping_is_read_between_polling_passes_not_during_one() -> None:
    model = _model()
    clock = FakeClock(autojump=True)
    device = ModbusDevice(SimulatedModbusGateway({1: _unit()}), unit_id=1, clock=clock, reconnect=NEVER_AGAIN,
                          models=[model], timeout_margin=TIMEOUT_MARGIN)
    client = Client(device, model, offline_after_intervals=OFFLINE_AFTER_INTERVALS, level=Level.DOCUMENTED,
                    clock=clock)
    await client.connect()
    order: list[str] = []
    real = device.read

    async def read(points: Sequence[Point[Any]]) -> Mapping[str, ReadResult]:
        kind = "poll" if points[0].key == LOW else "mapping"
        order.append(f"{kind} start")
        await asyncio.sleep(0.01)
        answers = await real(points)
        order.append(f"{kind} end")
        return answers

    setattr(device, "read", read)
    await asyncio.gather(client.refresh([LOW]), client.map_areas(limit=1))
    assert order[:3] == ["poll start", "poll end", "mapping start"]


# ============================================================================ micro_nabto


async def test_a_micro_nabto_unit_is_mapped_in_its_own_numbering_and_read_again_in_few_requests() -> None:
    model = micro_nabto_model([Section([Point(LOW, read=DatapointRegister(0), poll_rate=PollRate.FAST),
                                        Point(HIGH, read=DatapointRegister(9), poll_rate=PollRate.FAST),
                                        Point(Key("setting", int), read=SetpointRegister(30),
                                              write=SetpointRegister(30), poll_rate=PollRate.FAST)])],
                              max_registers=4, answer_wait=0.5, later_answer_wait=0.2)
    simulated = SimulatedMicroNabtoDevice(emails=frozenset({EMAIL}),
                                          datapoint_registers={(0, a): 10 + a for a in range(10) if a != 4},
                                          setpoint_registers={(0, 30): 1, (0, 31): 2})
    async with simulated:
        host, port = simulated.address
        device = MicroNabtoDevice(MicroNabtoConnection(EMAIL, host=host, port=port, timeout=0.05, retries=1,
                                                       answer_wait=0.5, reconnect=HANDSHAKE_NEVER_AGAIN,
                                                       session_idle=12.0, rediscovery_timeout=0.1,
                                                       rediscovery_resend=0.5),
                                  models=[model], owns_connection=True)
        client = Client(device, model, offline_after_intervals=OFFLINE_AFTER_INTERVALS, level=Level.DOCUMENTED)
        await client.connect()
        simulated.commands.clear()
        found = await client.map_areas(limit=32)
        first = len(simulated.commands)
        simulated.commands.clear()
        again = await client.read_mapped(found)
        await client.disconnect()
    assert dict(found.values) == {**{RawAddress("datapoint", a): 10 + a for a in range(10) if a != 4},
                                  RawAddress("setpoint", 30): 1, RawAddress("setpoint", 31): 2}
    assert RawAddress("datapoint", 4) in found.missing
    every = {RawAddress(space, a) for space in ("datapoint", "setpoint") for a in range(32)}
    assert set(found.values) | found.missing == every
    assert first == 66      # every one of the 52 absent addresses is asked for, most of them alone
    assert [(c.code, len(c.items)) for c in simulated.commands] == [
        (DATAPOINT_READ, 4), (DATAPOINT_READ, 4), (DATAPOINT_READ, 1), (SETPOINT_READ, 2)]
    assert dict(again.values) == dict(found.values)
    assert not simulated.received(SETPOINT_WRITE)


# ================================================================================== zeros


def _zeros_model() -> Any:
    """A model of two datapoints, taking zeros 2 s after they came."""
    return micro_nabto_model([Section([Point(LOW, read=DatapointRegister(0), poll_rate=PollRate.FAST),
                                        Point(HIGH, read=DatapointRegister(1), poll_rate=PollRate.FAST)])],
                              answer_wait=0.5, later_answer_wait=0.0,
                              zeros=RereadZeros(within=2.0, wait=Fixed(0.5), at_once=True, halves=False))


def _zeros_unit(clock: FakeClock) -> SimulatedMicroNabtoDevice:
    """A unit whose two datapoints read 5 and 6."""
    return SimulatedMicroNabtoDevice(emails=frozenset({EMAIL}), clock=clock, datapoint_registers={(0, 0): 5, (0, 1): 6})


def _zeros_client(simulated: SimulatedMicroNabtoDevice, clock: FakeClock) -> Client:
    model = _zeros_model()
    return Client(_micro_nabto_device(simulated, model, clock), model, offline_after_intervals=OFFLINE_AFTER_INTERVALS,
                  level=Level.DOCUMENTED, clock=clock)


def _micro_nabto_device(simulated: SimulatedMicroNabtoDevice, model: Any, clock: FakeClock) -> MicroNabtoDevice:
    host, port = simulated.address
    return MicroNabtoDevice(MicroNabtoConnection(EMAIL, host=host, port=port, timeout=0.05, retries=1,
                                                 answer_wait=0.5, clock=clock, reconnect=HANDSHAKE_NEVER_AGAIN,
                                                 session_idle=12.0, rediscovery_timeout=0.1, rediscovery_resend=0.5),
                            models=[model], owns_connection=True)


async def test_zeros_the_unit_really_answers_are_taken_once_its_model_takes_them() -> None:
    """Two settings switched off at once answer only zeros where they held values."""
    clock = FakeClock(autojump=True)
    simulated = _zeros_unit(clock)
    async with simulated:
        client = _zeros_client(simulated, clock)
        await client.connect()
        simulated.datapoint_registers.update({(0, 0): 0, (0, 1): 0})
        found = await client.map_areas(limit=2)
        await client.disconnect()
    assert dict(found.values) == {RawAddress("datapoint", 0): 0, RawAddress("datapoint", 1): 0}


async def test_zeros_of_a_gateway_that_holds_nothing_yet_are_not_taken_as_values() -> None:
    clock = FakeClock(autojump=True)
    simulated = _zeros_unit(clock)
    async with simulated:
        client = _zeros_client(simulated, clock)
        await client.connect()
        simulated.zeros_until = clock.monotonic() + 1.0
        found = await client.map_areas(limit=2)
        await client.disconnect()
    assert dict(found.values) == {RawAddress("datapoint", 0): 5, RawAddress("datapoint", 1): 6}


async def test_zeros_read_again_too_often_stop_the_mapping() -> None:
    unit = _unit()
    client = await _connected(_model(), unit)
    model = client.model

    async def only_zeros(points: Sequence[Point[Any]]) -> Mapping[str, ReadResult]:
        return {point.key: ReadResult(Outcome.BUSY, detail="only zeros", retry_after=0.5) for point in points}

    setattr(client, "_read_raw", only_zeros)
    with pytest.raises(CannotConnectError, match="busy, only zeros"):
        await client.map_areas(limit=1)
    assert model is client.model
