"""Mapping a unit: the areas its model's points span, found and read as raw words by the client."""
import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from event_connect import (
    Area,
    CannotConnectError,
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
from modbus_event_connect._device import ReadResult
from modbus_event_connect._mapping import PROBE_STRIDE, areas
from modbus_event_connect._point import Point
from modbus_event_connect.micro_nabto import (
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
)
from modbus_event_connect.testing import NO_ANSWER, SimulatedMicroNabtoDevice, SimulatedModbusDevice, SimulatedModbusGateway
from models import HANDSHAKE_NEVER_AGAIN, NEVER_AGAIN, OFFLINE_AFTER_INTERVALS, TIMEOUT_MARGIN, micro_nabto_model, modbus_model

EMAIL = "user@example.invalid"
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


def test_each_access_names_its_table() -> None:
    assert [access.table for access in (InputRegister(0), HoldingRegister(0), Coil(0), DiscreteInput(0),
                                        DatapointRegister(0), SetpointRegister(0), DatapointRegister(0, obj=2))] == [
        "input", "holding", "coil", "discrete_input", "datapoint", "setpoint", "datapoint:2"]


def test_the_areas_span_each_table_the_points_read_or_write_gaps_included_and_above_up_to_the_limit() -> None:
    assert areas(_model(), 300) == (Area("input", 10, 20), Area("input", 30, 270), Area("holding", 5, 1),
                                    Area("holding", 6, 294))


def test_a_limit_at_or_below_a_span_adds_nothing_above_it() -> None:
    assert areas(_model(), 30) == (Area("input", 10, 20), Area("holding", 5, 1), Area("holding", 6, 24))
    assert areas(_model(), 1) == (Area("input", 10, 20), Area("holding", 5, 1))


def test_the_limit_is_at_least_one_address() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        areas(_model(), 0)


def test_a_bit_table_spans_one_address_a_point() -> None:
    model = modbus_model([Section([Point(FLAG, read=Coil(3), data_type=DataType.BOOL, poll_rate=PollRate.FAST),
                                   Point(Key("other", bool), read=Coil(7), data_type=DataType.BOOL,
                                         poll_rate=PollRate.FAST)])])
    assert areas(model, 1) == (Area("coil", 3, 5),)


async def test_a_bit_table_is_mapped_as_bits_and_a_point_only_written_widens_the_span() -> None:
    model = modbus_model([Section([
        Point(FLAG, read=Coil(3), data_type=DataType.BOOL, poll_rate=PollRate.FAST),
        Point(Key("other", bool), read=Coil(5), data_type=DataType.BOOL, poll_rate=PollRate.FAST),
        Point(Key("reset", bool), write=Coil(9), data_type=DataType.BOOL)])], bit_write=None)
    unit = SimulatedModbusDevice(coils={a: a % 2 for a in range(3, 10)})
    client = await _connected(model, unit)
    snapshot = await client.map_areas(limit=1)
    assert dict(snapshot.values) == {RawAddress("coil", a): a % 2 for a in range(3, 10)}
    assert _reads(unit) == [(FunctionCode.READ_COILS, 3, 7)]


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


async def test_mapping_reads_every_register_of_the_span_and_finds_the_absent_ones() -> None:
    unit = _unit()
    client = await _connected(_model(), unit)
    snapshot = await client.map_areas(limit=1)
    assert dict(snapshot.values) == {**{ir(a): 100 + a for a in range(10, 30) if a not in (15, 16)},
                                     RawAddress("holding", 5): 105}
    assert snapshot.missing == frozenset({ir(15), ir(16)})
    assert all(function.is_read for function, _, _ in _reads(unit))


async def test_after_an_absent_register_the_span_is_read_from_one_at_a_time_doubling_while_it_answers() -> None:
    unit = _unit()
    client = await _connected(_model(max_registers=8), unit)
    await client.map_areas(limit=1)
    inputs = [(address, count) for function, address, count in _reads(unit)
              if function is FunctionCode.READ_INPUT_REGISTERS]
    # 10-17 is refused and halved down to 15 and 16; then 18 alone, 19-20, 21-24, 25-29.
    assert inputs[0] == (10, 8)
    assert inputs[-4:] == [(18, 1), (19, 2), (21, 4), (25, 5)]


async def test_a_span_that_answers_whole_costs_one_request_of_the_models_size_each() -> None:
    unit = _unit(inputs=(15, 16))
    client = await _connected(_model(max_registers=8), unit)
    await client.map_areas(limit=1)
    assert [(a, c) for f, a, c in _reads(unit) if f is FunctionCode.READ_INPUT_REGISTERS] == [(10, 8), (18, 8), (26, 4)]


async def test_above_the_span_one_register_in_ten_is_asked_for_and_only_the_stretches_around_an_answer_read() -> None:
    unit = _unit(inputs=(52, 53), holding=())
    model = modbus_model([Section([Point(LOW, read=InputRegister(10), poll_rate=PollRate.FAST),
                                   Point(HIGH, read=InputRegister(29), poll_rate=PollRate.FAST)])], max_registers=8)
    unit.input_registers[50] = 150
    client = await _connected(model, unit)
    snapshot = await client.map_areas(limit=90)
    probes = list(range(30, 90, PROBE_STRIDE))
    reads = _reads(unit)
    assert [(a, c) for _, a, c in reads if c == 1 and a in probes] == [(a, 1) for a in probes]
    assert {ir(50), ir(52), ir(53)} <= set(snapshot.values)
    # The stretches 40-49, 50-59 and 60-69 are read; those of 30-39 and 70-89 only by their probe.
    assert ir(45) in snapshot.missing and ir(65) in snapshot.missing
    assert ir(35) not in snapshot.missing and ir(35) not in snapshot.values
    assert ir(30) in snapshot.missing and ir(80) in snapshot.missing


async def test_a_mapped_unit_is_read_again_only_where_it_answered_in_requests_of_the_models_size() -> None:
    unit = _unit()
    client = await _connected(_model(max_registers=8), unit)
    found = await client.map_areas(limit=1)
    unit.requests.clear()
    unit.input_registers[12] = 7
    del unit.input_registers[13]
    again = await client.read_mapped(found)
    # 10-14 and 17-19, then 20-27, then 28-29 and holding 5; 10-14 refused for 13, and halved.
    assert [(a, c) for _, a, c in _reads(unit)][:3] == [(10, 5), (10, 2), (12, 3)]
    assert {(a, c) for _, a, c in _reads(unit)} >= {(17, 3), (20, 8), (28, 2), (5, 1)}
    assert not {a for _, a, c in _reads(unit) if c == 1} & {15, 16}
    assert ir(13) in again.missing
    assert difference(found, again)[:2] == (RegisterChange(ir(12), 112, 7, (0, 1, 2, 4, 5, 6)),
                                            RegisterChange(ir(13), 113, None, ()))
    assert not again.missing - {ir(13)}


async def test_a_register_of_a_table_the_model_does_not_read_is_not_read_again() -> None:
    client = await _connected(_model(), _unit())
    with pytest.raises(ValueError, match="reads no table 'datapoint'"):
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
    unit.faults[(FunctionCode.READ_INPUT_REGISTERS, 11)] = NO_ANSWER
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
    assert first == 13      # 0-3; 4-7 halved to 4; 8; 9; probes 10, 20 and 30; setpoint 30; probe 31
    assert [(c.code, len(c.items)) for c in simulated.commands] == [
        (DATAPOINT_READ, 4), (DATAPOINT_READ, 4), (DATAPOINT_READ, 1), (SETPOINT_READ, 2)]
    assert dict(again.values) == dict(found.values)
    assert not simulated.received(SETPOINT_WRITE)
