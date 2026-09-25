"""The Modbus protocol layer: `ModbusDevice` against simulated units behind a simulated gateway,
with a fake clock and a `sleep` that does not wait."""
import asyncio
from typing import Any

import pytest

from src.modbus_event_connect._data_type import DataType
from src.modbus_event_connect._device import (
    Device,
    EncodedWrite,
    Outcome,
    ProtocolOptions,
    ReadResult,
)
from src.modbus_event_connect._key import Key
from src.modbus_event_connect._point import Point
from src.modbus_event_connect.micro_nabto._access import DatapointRegister
from src.modbus_event_connect.modbus._access import (
    BitWrite,
    Coil,
    DiscreteInput,
    HoldingRegister,
    InputRegister,
    ModbusOptions,
    RegisterNumbering,
    SingleWrite,
    modicon,
    plain,
)
from src.modbus_event_connect.modbus._connection import (
    ExceptionCode,
    FunctionCode,
    ModbusTcpConnection,
    Request,
    Response,
)
from src.modbus_event_connect.modbus._device import ModbusDevice
from src.modbus_event_connect.testing._clock import FakeClock
from src.modbus_event_connect.testing._modbus import (
    NO_ANSWER,
    SimulatedModbusDevice,
    SimulatedModbusGateway,
)

FC = FunctionCode
HOST = "modbus.invalid"


# ================================================================================ helpers


class Sleeps:
    """An injected `sleep` that records what it was asked to wait, and does not wait."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def configured(device: ModbusDevice) -> ModbusDevice:
    device.configure(ModbusOptions(numbering=plain(first_address=1)))
    return device


class Rig:
    """One device on a simulated gateway, with everything a test wants to look at."""

    def __init__(self, unit: SimulatedModbusDevice, *, unit_id: int = 1,
                 options: ModbusOptions = ModbusOptions(numbering=plain(first_address=1)),
                 delay: float = 0.0, **device: Any) -> None:
        self.unit = unit
        self.gateway = SimulatedModbusGateway({unit_id: unit}, delay=delay)
        self.clock = FakeClock()
        self.sleeps = Sleeps()
        self.device = ModbusDevice(self.gateway, unit_id, clock=self.clock, sleep=self.sleeps, **device)
        self.device.configure(options)

    def sent(self) -> list[tuple[FunctionCode, int, int]]:
        """(function, address, count) of every request the unit received."""
        return [(r.function, r.address, r.count) for r in self.unit.requests]


def u16(key: str, access: Any, **kw: Any) -> Point[Any]:
    return Point(Key(key, int), read=access, data_type=DataType.UINT16, **kw)


def ok(*registers: int) -> ReadResult:
    return ReadResult(Outcome.OK, registers)


def outcomes(result: Any) -> dict[str, Outcome]:
    return {key: raw.outcome for key, raw in result.items()}


def image(start: int, *values: int) -> dict[int, int]:
    return {start + i: v for i, v in enumerate(values)}


# ================================================================================ reading


@pytest.mark.parametrize("access, function, unit", [
    (InputRegister(7), FC.READ_INPUT_REGISTERS, SimulatedModbusDevice(input_registers={7: 1234})),
    (HoldingRegister(7), FC.READ_HOLDING_REGISTERS, SimulatedModbusDevice(holding_registers={7: 1234})),
])
async def test_each_register_table_is_read_with_its_function_code(access: Any, function: FunctionCode,
                                                                  unit: SimulatedModbusDevice) -> None:
    rig = Rig(unit)
    result = await rig.device.read([u16("p", access)])
    assert result == {"p": ok(1234)}
    assert rig.sent() == [(function, 7, 1)]


@pytest.mark.parametrize("access, function, unit", [
    (Coil(3), FC.READ_COILS, SimulatedModbusDevice(coils={3: 1})),
    (DiscreteInput(3), FC.READ_DISCRETE_INPUTS, SimulatedModbusDevice(discrete_inputs={3: 1})),
])
async def test_each_bit_table_is_read_with_its_function_code(access: Any, function: FunctionCode,
                                                             unit: SimulatedModbusDevice) -> None:
    rig = Rig(unit)
    result = await rig.device.read([Point(Key("p", bool), read=access, data_type=DataType.BOOL)])
    assert result == {"p": ok(1)}
    assert rig.sent() == [(function, 3, 1)]


async def test_each_point_gets_exactly_its_own_registers_from_a_batch() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers=image(10, 1, 0xAAAA, 0xBBBB, 4, 0x4142, 0x4300)))
    points = [
        u16("a", HoldingRegister(10)),
        Point(Key("b", int), read=HoldingRegister(11), data_type=DataType.UINT32),
        u16("c", HoldingRegister(13)),
        Point(Key("d", str), read=HoldingRegister(14), data_type=DataType.string(2)),
    ]
    result = await rig.device.read(points)
    assert result == {"a": ok(1), "b": ok(0xAAAA, 0xBBBB), "c": ok(4), "d": ok(0x4142, 0x4300)}
    assert rig.sent() == [(FC.READ_HOLDING_REGISTERS, 10, 6)]


async def test_the_order_points_are_given_in_does_not_matter() -> None:
    rig = Rig(SimulatedModbusDevice(input_registers=image(0, 10, 11, 12)))
    result = await rig.device.read([u16("c", InputRegister(2)), u16("a", InputRegister(0)), u16("b", InputRegister(1))])
    assert result == {"a": ok(10), "b": ok(11), "c": ok(12)}
    assert rig.sent() == [(FC.READ_INPUT_REGISTERS, 0, 3)]


# =============================================================================== batching


async def test_a_gap_is_never_bridged() -> None:
    # Reading 10-12 would ask for 11, which no point needs - and which this unit does not have.
    rig = Rig(SimulatedModbusDevice(holding_registers={10: 1, 12: 3}))
    result = await rig.device.read([u16("a", HoldingRegister(10)), u16("c", HoldingRegister(12))])
    assert result == {"a": ok(1), "c": ok(3)}
    assert rig.sent() == [(FC.READ_HOLDING_REGISTERS, 10, 1), (FC.READ_HOLDING_REGISTERS, 12, 1)]


async def test_tables_are_never_merged() -> None:
    rig = Rig(SimulatedModbusDevice(input_registers={10: 1}, holding_registers={10: 2, 11: 3}, coils={10: 1}, discrete_inputs={11: 0}))
    result = await rig.device.read([
        u16("in", InputRegister(10)), u16("h0", HoldingRegister(10)), u16("h1", HoldingRegister(11)),
        Point(Key("coil", bool), read=Coil(10), data_type=DataType.BOOL), Point(Key("di", bool), read=DiscreteInput(11), data_type=DataType.BOOL),
    ])
    assert result == {"in": ok(1), "h0": ok(2), "h1": ok(3), "coil": ok(1), "di": ok(0)}
    assert sorted(rig.sent()) == sorted([
        (FC.READ_INPUT_REGISTERS, 10, 1), (FC.READ_HOLDING_REGISTERS, 10, 2),
        (FC.READ_COILS, 10, 1), (FC.READ_DISCRETE_INPUTS, 11, 1)])


async def test_a_batch_stops_at_max_registers() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers=image(0, *range(10))), options=ModbusOptions(numbering=plain(first_address=1), max_registers=4))
    result = await rig.device.read([u16(f"r{i}", HoldingRegister(i)) for i in range(10)])
    assert result == {f"r{i}": ok(i) for i in range(10)}
    assert rig.sent() == [(FC.READ_HOLDING_REGISTERS, 0, 4), (FC.READ_HOLDING_REGISTERS, 4, 4),
                          (FC.READ_HOLDING_REGISTERS, 8, 2)]


async def test_a_multi_register_point_is_never_split_across_requests() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers=image(0, 1, 2, 3, 4)), options=ModbusOptions(numbering=plain(first_address=1), max_registers=3))
    result = await rig.device.read([u16("a", HoldingRegister(0)), u16("b", HoldingRegister(1)),
                                    Point(Key("c", int), read=HoldingRegister(2), data_type=DataType.UINT32)])
    assert result == {"a": ok(1), "b": ok(2), "c": ok(3, 4)}
    assert rig.sent() == [(FC.READ_HOLDING_REGISTERS, 0, 2), (FC.READ_HOLDING_REGISTERS, 2, 2)]


async def test_a_batch_stops_at_max_bits() -> None:
    rig = Rig(SimulatedModbusDevice(coils=image(0, 1, 0, 1, 0, 1)), options=ModbusOptions(numbering=plain(first_address=1), max_bits=2))
    result = await rig.device.read([Point(Key(f"c{i}", bool), read=Coil(i), data_type=DataType.BOOL) for i in range(5)])
    assert result == {f"c{i}": ok(1 - i % 2) for i in range(5)}
    assert rig.sent() == [(FC.READ_COILS, 0, 2), (FC.READ_COILS, 2, 2), (FC.READ_COILS, 4, 1)]


async def test_max_registers_does_not_limit_a_bit_table() -> None:
    rig = Rig(SimulatedModbusDevice(discrete_inputs=image(0, *[1] * 10)), options=ModbusOptions(numbering=plain(first_address=1), max_registers=2))
    await rig.device.read([Point(Key(f"d{i}", bool), read=DiscreteInput(i), data_type=DataType.BOOL) for i in range(10)])
    assert rig.sent() == [(FC.READ_DISCRETE_INPUTS, 0, 10)]


async def test_two_views_of_one_register_share_one_request() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={5: 0b1001}))
    result = await rig.device.read([
        u16("raw", HoldingRegister(5)), Point(Key("signed", int), read=HoldingRegister(5), data_type=DataType.INT16),
        Point(Key("bit0", bool), read=HoldingRegister(5), data_type=DataType.bit(0)),
        Point(Key("bit3", bool), read=HoldingRegister(5), data_type=DataType.bit(3)),
    ])
    assert result == {"raw": ok(9), "signed": ok(9), "bit0": ok(9), "bit3": ok(9)}
    assert rig.sent() == [(FC.READ_HOLDING_REGISTERS, 5, 1)]


async def test_overlapping_spans_are_read_together() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={10: 1, 11: 2}))
    result = await rig.device.read([Point(Key("wide", int), read=HoldingRegister(10), data_type=DataType.UINT32),
                                    Point(Key("flag", bool), read=HoldingRegister(11), data_type=DataType.bit(1))])
    assert result == {"wide": ok(1, 2), "flag": ok(2)}
    assert rig.sent() == [(FC.READ_HOLDING_REGISTERS, 10, 2)]


# ============================================================================= addressing


@pytest.mark.parametrize("numbering, access, address, function", [
    (plain(first_address=0), HoldingRegister(1), 0, FC.READ_HOLDING_REGISTERS),
    (plain(first_address=0), Coil(5), 4, FC.READ_COILS),
    (modicon(digits=5, first_address=0), HoldingRegister(40001), 0, FC.READ_HOLDING_REGISTERS),
    (modicon(digits=5, first_address=0), HoldingRegister(40120), 119, FC.READ_HOLDING_REGISTERS),
    (modicon(digits=5, first_address=0), InputRegister(30011), 10, FC.READ_INPUT_REGISTERS),
    (modicon(digits=5, first_address=0), DiscreteInput(10005), 4, FC.READ_DISCRETE_INPUTS),
    (modicon(digits=5, first_address=0), Coil(1), 0, FC.READ_COILS),
    (modicon(digits=6, first_address=0), HoldingRegister(400101), 100, FC.READ_HOLDING_REGISTERS),
    (modicon(digits=6, first_address=0), InputRegister(365536), 65535, FC.READ_INPUT_REGISTERS),
])
async def test_a_register_number_is_sent_as_its_address(
        numbering: RegisterNumbering, access: Any, address: int, function: FunctionCode) -> None:
    everything = {address: 1}
    rig = Rig(SimulatedModbusDevice(coils=everything, discrete_inputs=everything, holding_registers=everything, input_registers=everything),
              options=ModbusOptions(numbering=numbering))
    data_type = DataType.BOOL if access.bits else DataType.UINT16
    result = await rig.device.read([Point(Key("p", bool if access.bits else int), read=access, data_type=data_type)])
    assert result == {"p": ok(1)}
    assert rig.sent() == [(function, address, 1)]


async def test_a_write_goes_to_the_address_of_its_register_number() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={119: 0}), options=ModbusOptions(numbering=modicon(digits=5, first_address=0)))
    point = Point(Key("t", int), read=HoldingRegister(40120), write=HoldingRegister(40120), data_type=DataType.UINT16)
    assert (await rig.device.write(point, EncodedWrite((215,)))).ok
    assert rig.unit.holding_registers[119] == 215


# ========================================================================= 0x02 isolation


@pytest.mark.parametrize("access, data_type, unit", [
    (HoldingRegister, DataType.UINT16, SimulatedModbusDevice(holding_registers={10: 1, 12: 1})),
    (InputRegister, DataType.UINT16, SimulatedModbusDevice(input_registers={10: 1, 12: 1})),
    (Coil, DataType.BOOL, SimulatedModbusDevice(coils={10: 1, 12: 1})),
    (DiscreteInput, DataType.BOOL, SimulatedModbusDevice(discrete_inputs={10: 1, 12: 1})),
])
async def test_a_refused_batch_is_re_read_so_only_the_missing_point_is_missing(
        access: Any, data_type: DataType, unit: SimulatedModbusDevice) -> None:
    rig = Rig(unit)
    value_type = bool if data_type.is_boolean else int
    result = await rig.device.read([Point(Key(f"p{a}", value_type), read=access(a), data_type=data_type)
                                    for a in (10, 11, 12)])
    assert outcomes(result) == {"p10": Outcome.OK, "p11": Outcome.MISSING, "p12": Outcome.OK}
    assert result["p10"] == ok(1) and result["p11"].exception_code == 0x02
    assert [(a, c) for _, a, c in rig.sent()] == [(10, 3), (10, 1), (11, 2), (11, 1), (12, 1)]


async def test_one_absent_address_among_many_is_found_in_few_requests() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={a: a for a in range(32) if a != 21}))
    result = await rig.device.read([u16(f"p{a}", HoldingRegister(a)) for a in range(32)])
    assert [k for k, r in result.items() if r.outcome is Outcome.MISSING] == ["p21"]
    assert len(rig.sent()) <= 1 + 2 * 5, "halving: two reads per level of five"


async def test_points_sharing_a_refused_span_share_its_re_read() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={10: 7}))
    result = await rig.device.read([
        u16("present", HoldingRegister(10)), Point(Key("present_bit", bool), read=HoldingRegister(10), data_type=DataType.bit(0)),
        Point(Key("gone0", bool), read=HoldingRegister(11), data_type=DataType.bit(0)),
        Point(Key("gone1", bool), read=HoldingRegister(11), data_type=DataType.bit(1)),
    ])
    assert outcomes(result) == {"present": Outcome.OK, "present_bit": Outcome.OK,
                                "gone0": Outcome.MISSING, "gone1": Outcome.MISSING}
    assert rig.sent() == [(FC.READ_HOLDING_REGISTERS, 10, 2), (FC.READ_HOLDING_REGISTERS, 10, 1),
                          (FC.READ_HOLDING_REGISTERS, 11, 1)]


async def test_a_refused_single_span_is_missing_without_a_re_read() -> None:
    rig = Rig(SimulatedModbusDevice())
    result = await rig.device.read([u16("a", HoldingRegister(4)), Point(Key("b", bool), read=HoldingRegister(4), data_type=DataType.bit(2))])
    assert outcomes(result) == {"a": Outcome.MISSING, "b": Outcome.MISSING}
    assert rig.sent() == [(FC.READ_HOLDING_REGISTERS, 4, 1)]


async def test_a_refused_batch_in_one_table_leaves_the_others_alone() -> None:
    rig = Rig(SimulatedModbusDevice(input_registers={0: 5, 1: 6}, holding_registers={0: 1}))
    result = await rig.device.read([u16("i0", InputRegister(0)), u16("i1", InputRegister(1)),
                                    u16("h0", HoldingRegister(0)), u16("h1", HoldingRegister(1))])
    assert outcomes(result) == {"i0": Outcome.OK, "i1": Outcome.OK, "h0": Outcome.OK, "h1": Outcome.MISSING}
    assert rig.unit.function_codes().count(FC.READ_INPUT_REGISTERS) == 1


# ======================================================================== exception codes


@pytest.mark.parametrize("code, outcome", [
    (ExceptionCode.ILLEGAL_FUNCTION, Outcome.UNSUPPORTED),
    (ExceptionCode.ILLEGAL_DATA_VALUE, Outcome.ERROR),
    (ExceptionCode.SERVER_DEVICE_FAILURE, Outcome.OFFLINE),
    (ExceptionCode.ACKNOWLEDGE, Outcome.ERROR),
    (ExceptionCode.GATEWAY_PATH_UNAVAILABLE, Outcome.ERROR),
    (ExceptionCode.GATEWAY_TARGET_FAILED, Outcome.NO_ANSWER),
    (0x42, Outcome.ERROR),
])
async def test_every_exception_code_has_its_outcome(code: int, outcome: Outcome) -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={3: 1}, faults={(FC.READ_HOLDING_REGISTERS, 3): code}))
    raw = (await rig.device.read([u16("p", HoldingRegister(3))]))["p"]
    assert (raw.outcome, raw.exception_code, raw.registers) == (outcome, code, ())
    assert "READ_HOLDING_REGISTERS 3" in raw.detail


async def test_an_unsupported_function_is_unsupported() -> None:
    rig = Rig(SimulatedModbusDevice(discrete_inputs={0: 1}, unsupported=[FC.READ_DISCRETE_INPUTS]))
    raw = (await rig.device.read([Point(Key("d", bool), read=DiscreteInput(0), data_type=DataType.BOOL)]))["d"]
    assert (raw.outcome, raw.exception_code) == (Outcome.UNSUPPORTED, 0x01)


async def test_asking_for_more_than_the_unit_accepts_is_an_error() -> None:
    # The model forgot to state max_registers; the unit says 0x03 and the core sees ERROR.
    rig = Rig(SimulatedModbusDevice(holding_registers=image(0, 1, 2, 3), max_registers=2))
    result = await rig.device.read([u16(f"r{i}", HoldingRegister(i)) for i in range(3)])
    assert {r.outcome for r in result.values()} == {Outcome.ERROR}
    assert {r.exception_code for r in result.values()} == {0x03}


async def test_a_unit_the_gateway_cannot_reach_has_not_answered() -> None:
    """0x0B is the gateway speaking for a silent device - NO_ANSWER, not OFFLINE."""
    gateway = SimulatedModbusGateway({1: SimulatedModbusDevice(holding_registers={0: 1})})
    device = configured(ModbusDevice(gateway, 9, clock=FakeClock(), sleep=Sleeps()))
    raw = (await device.read([u16("p", HoldingRegister(0))]))["p"]
    assert (raw.outcome, raw.exception_code) == (Outcome.NO_ANSWER, 0x0B)


async def test_a_unit_the_gateway_cannot_reach_is_backed_off() -> None:
    """The gateway's bus timeout before saying 0x0B costs every other device on the bus that time."""
    gateway = SimulatedModbusGateway({1: SimulatedModbusDevice(holding_registers={0: 1})})
    device = configured(ModbusDevice(gateway, 9, clock=FakeClock(), sleep=Sleeps(), backoff_after=3))
    for _ in range(3):
        await device.read([u16("p", HoldingRegister(0))])
    assert device.diagnostics()["backing_off"] is True
    await device.read([u16("p", HoldingRegister(0))])
    assert len(gateway.requests) == 3, "a device backing off still put requests on the bus"


async def test_no_answer_is_no_answer() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={0: 1}, faults={(FC.READ_HOLDING_REGISTERS, 0): NO_ANSWER}))
    raw = (await rig.device.read([u16("p", HoldingRegister(0))]))["p"]
    assert (raw.outcome, raw.exception_code) == (Outcome.NO_ANSWER, 0)


async def test_a_fault_on_one_address_fails_the_whole_batch_that_touches_it() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers=image(0, 1, 2), faults={(FC.READ_HOLDING_REGISTERS, 1): 0x04}))
    result = await rig.device.read([u16("a", HoldingRegister(0)), u16("b", HoldingRegister(1))])
    assert outcomes(result) == {"a": Outcome.OFFLINE, "b": Outcome.OFFLINE}
    assert len(rig.unit.requests) == 1                 # only 0x02 is isolated


# ================================================================================== busy


async def test_busy_is_retried_with_backoff_until_it_succeeds() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={0: 42}, busy_for=2))
    assert (await rig.device.read([u16("p", HoldingRegister(0))]))["p"] == ok(42)
    assert rig.sleeps.waits == [0.2, 0.4]
    assert len(rig.unit.requests) == 3


async def test_busy_gives_up_after_its_retries() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={0: 42}, busy_for=100), busy_retries=4, busy_delay=0.5)
    raw = (await rig.device.read([u16("p", HoldingRegister(0))]))["p"]
    assert (raw.outcome, raw.exception_code) == (Outcome.BUSY, 0x06)
    assert rig.sleeps.waits == [0.5, 1.0, 2.0, 4.0]
    assert len(rig.unit.requests) == 5
    assert "after 4 busy retries" in raw.detail


async def test_busy_is_retried_for_a_write_too() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={0: 0}, busy_for=1))
    point = Point(Key("s", int), write=HoldingRegister(0), data_type=DataType.UINT16)
    assert (await rig.device.write(point, EncodedWrite((9,)))).ok
    assert rig.unit.holding_registers[0] == 9 and rig.sleeps.waits == [0.2]


async def test_without_busy_retries_busy_is_answered_at_once() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={0: 42}, busy_for=1), busy_retries=0)
    assert (await rig.device.read([u16("p", HoldingRegister(0))]))["p"].outcome is Outcome.BUSY
    assert rig.sleeps.waits == []


# ================================================================================ writing


def setting(access: Any, data_type: DataType = DataType.UINT16) -> Point[Any]:
    return Point(Key("w", bool if data_type.is_boolean else int), read=access, write=access, data_type=data_type)


async def test_one_holding_register_is_written_with_0x06() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={20: 0}))
    result = await rig.device.write(setting(HoldingRegister(20)), EncodedWrite((0x1234,)))
    assert result.ok and result.outcome is Outcome.OK
    assert rig.unit.requests == [Request(1, FC.WRITE_SINGLE_REGISTER, 20, values=(0x1234,))]
    assert rig.unit.holding_registers[20] == 0x1234


async def test_one_holding_register_is_written_with_0x10_under_the_fc16_policy() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={20: 0}, unsupported=[FC.WRITE_SINGLE_REGISTER]),
              options=ModbusOptions(numbering=plain(first_address=1), single_write=SingleWrite.FC16))
    assert (await rig.device.write(setting(HoldingRegister(20)), EncodedWrite((7,)))).ok
    assert rig.unit.requests == [Request(1, FC.WRITE_MULTIPLE_REGISTERS, 20, count=1, values=(7,))]
    assert rig.unit.holding_registers[20] == 7


async def test_several_holding_registers_are_written_with_0x10() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={20: 0, 21: 0}))
    assert (await rig.device.write(setting(HoldingRegister(20), DataType.UINT32), EncodedWrite((1, 2)))).ok
    assert rig.unit.requests == [Request(1, FC.WRITE_MULTIPLE_REGISTERS, 20, count=2, values=(1, 2))]
    assert (rig.unit.holding_registers[20], rig.unit.holding_registers[21]) == (1, 2)


@pytest.mark.parametrize("bit, on, and_mask, or_mask, after", [
    (3, True, 0xFFF7, 0x0008, 0x00F8),
    (4, False, 0xFFEF, 0x0000, 0x00E0),
    (15, True, 0x7FFF, 0x8000, 0x80F0),
    (0, False, 0xFFFE, 0x0000, 0x00F0),
])
async def test_a_bit_is_written_with_mask_write(bit: int, on: bool, and_mask: int, or_mask: int, after: int) -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={9: 0x00F0}))
    point = setting(HoldingRegister(9), DataType.bit(bit))
    assert (await rig.device.write(point, EncodedWrite(bit_index=bit, bit_value=on))).ok
    assert rig.unit.requests == [Request(1, FC.MASK_WRITE_REGISTER, 9, and_mask=and_mask, or_mask=or_mask)]
    assert rig.unit.holding_registers[9] == after


@pytest.mark.parametrize("bit, on, after", [(3, True, 0x00F8), (4, False, 0x00E0)])
async def test_a_bit_is_read_modified_and_written_back_without_mask_write(bit: int, on: bool, after: int) -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={9: 0x00F0}), options=ModbusOptions(numbering=plain(first_address=1), bit_write=BitWrite.READ_MODIFY_WRITE))
    assert (await rig.device.write(setting(HoldingRegister(9), DataType.bit(bit)), EncodedWrite(bit_index=bit, bit_value=on))).ok
    assert rig.unit.requests == [Request(1, FC.READ_HOLDING_REGISTERS, 9, count=1),
                                 Request(1, FC.WRITE_SINGLE_REGISTER, 9, values=(after,))]
    assert rig.unit.holding_registers[9] == after


async def test_concurrent_read_modify_writes_to_one_register_keep_both_bits() -> None:
    # Unserialised, this is read, read, write, write - and the second write undoes the first.
    rig = Rig(SimulatedModbusDevice(holding_registers={9: 0}), options=ModbusOptions(numbering=plain(first_address=1), bit_write=BitWrite.READ_MODIFY_WRITE),
              delay=0.001)
    results = await asyncio.gather(
        rig.device.write(setting(HoldingRegister(9), DataType.bit(0)), EncodedWrite(bit_index=0, bit_value=True)),
        rig.device.write(setting(HoldingRegister(9), DataType.bit(1)), EncodedWrite(bit_index=1, bit_value=True)),
    )
    assert all(r.ok for r in results)
    assert rig.unit.holding_registers[9] == 0b11
    assert rig.unit.function_codes() == [FC.READ_HOLDING_REGISTERS, FC.WRITE_SINGLE_REGISTER,
                                    FC.READ_HOLDING_REGISTERS, FC.WRITE_SINGLE_REGISTER]


async def test_read_modify_write_to_different_registers_do_not_wait_for_each_other() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={1: 0, 2: 0}), options=ModbusOptions(numbering=plain(first_address=1), bit_write=BitWrite.READ_MODIFY_WRITE),
              delay=0.001)
    await asyncio.gather(
        rig.device.write(setting(HoldingRegister(1), DataType.bit(0)), EncodedWrite(bit_index=0, bit_value=True)),
        rig.device.write(setting(HoldingRegister(2), DataType.bit(0)), EncodedWrite(bit_index=0, bit_value=True)),
    )
    assert (rig.unit.holding_registers[1], rig.unit.holding_registers[2]) == (1, 1)
    assert rig.unit.function_codes()[:2] == [FC.READ_HOLDING_REGISTERS, FC.READ_HOLDING_REGISTERS]


async def test_a_failed_read_in_read_modify_write_writes_nothing() -> None:
    rig = Rig(SimulatedModbusDevice(), options=ModbusOptions(numbering=plain(first_address=1), bit_write=BitWrite.READ_MODIFY_WRITE))
    result = await rig.device.write(setting(HoldingRegister(9), DataType.bit(0)), EncodedWrite(bit_index=0, bit_value=True))
    assert (result.outcome, result.exception_code) == (Outcome.MISSING, 0x02)
    assert rig.unit.function_codes() == [FC.READ_HOLDING_REGISTERS]


@pytest.mark.parametrize("value, stored", [(1, 1), (0, 0)])
async def test_a_coil_is_written_with_0x05(value: int, stored: int) -> None:
    rig = Rig(SimulatedModbusDevice(coils={4: 1 - stored}))
    point = Point(Key("c", bool), read=Coil(4), write=Coil(4), data_type=DataType.BOOL)
    assert (await rig.device.write(point, EncodedWrite((value,)))).ok
    assert rig.unit.requests == [Request(1, FC.WRITE_SINGLE_COIL, 4, values=(value,))]
    assert rig.unit.coils[4] == stored


@pytest.mark.parametrize("value", [EncodedWrite((1, 0)), EncodedWrite((2,)), EncodedWrite(bit_index=0, bit_value=True)])
async def test_a_coil_takes_exactly_one_value_of_zero_or_one(value: EncodedWrite) -> None:
    rig = Rig(SimulatedModbusDevice(coils={4: 0}))
    with pytest.raises(ValueError):
        await rig.device.write(Point(Key("c", bool), write=Coil(4), data_type=DataType.BOOL), value)
    assert rig.unit.requests == []


@pytest.mark.parametrize("access", [InputRegister(1), DiscreteInput(1)])
async def test_a_read_only_table_cannot_be_written(access: Any) -> None:
    point = Point(Key("p", bool), read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.BOOL)
    object.__setattr__(point, "write", access)          # a point would refuse this; the device must too
    rig = Rig(SimulatedModbusDevice())
    with pytest.raises(TypeError):
        await rig.device.write(point, EncodedWrite((1,)))


@pytest.mark.parametrize("code, outcome", [(0x02, Outcome.MISSING), (0x04, Outcome.OFFLINE),
                                           (0x03, Outcome.ERROR), (0x01, Outcome.UNSUPPORTED),
                                           (NO_ANSWER, Outcome.NO_ANSWER)])
async def test_a_refused_write_is_reported_not_raised(code: int, outcome: Outcome) -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={0: 5}, faults={(FC.WRITE_SINGLE_REGISTER, 0): code}))
    result = await rig.device.write(setting(HoldingRegister(0)), EncodedWrite((6,)))
    assert not result.ok and result.outcome is outcome
    assert rig.unit.holding_registers[0] == 5                    # a refused write does not land


async def test_a_write_to_an_absent_register_is_missing() -> None:
    rig = Rig(SimulatedModbusDevice())
    assert (await rig.device.write(setting(HoldingRegister(0)), EncodedWrite((6,)))).outcome is Outcome.MISSING


# ==================================================================== programming errors


async def test_a_point_without_a_read_side_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        await Rig(SimulatedModbusDevice()).device.read([Point(Key("w", int), write=HoldingRegister(0), data_type=DataType.UINT16)])


async def test_a_point_without_a_write_side_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        await Rig(SimulatedModbusDevice()).device.write(u16("r", HoldingRegister(0)), EncodedWrite((1,)))


async def test_a_point_of_another_protocol_is_a_programming_error() -> None:
    rig = Rig(SimulatedModbusDevice())
    with pytest.raises(TypeError):
        await rig.device.read([Point(Key("n", int), read=DatapointRegister(3), data_type=DataType.UINT16)])
    assert rig.unit.requests == []


async def test_a_device_reads_nothing_before_it_knows_the_models_options() -> None:
    gateway = SimulatedModbusGateway({1: SimulatedModbusDevice(holding_registers={0: 1})})
    device = ModbusDevice(gateway)
    assert device.options is None
    with pytest.raises(RuntimeError):
        await device.read([Point(Key("p", int), read=HoldingRegister(0))])
    assert gateway.requests == []


def test_a_device_takes_only_modbus_options() -> None:
    device = ModbusDevice(SimulatedModbusGateway({}))
    options = ModbusOptions(numbering=plain(first_address=1), max_registers=32)
    device.configure(options)
    assert device.options is options
    with pytest.raises(TypeError):
        device.configure(ProtocolOptions())


# ============================================================================== backoff


def silent(**kw: Any) -> SimulatedModbusDevice:
    """A unit that never answers a holding read at address 0."""
    return SimulatedModbusDevice(holding_registers={0: 1}, faults={(FC.READ_HOLDING_REGISTERS, 0): NO_ANSWER}, **kw)


async def read0(rig: Rig) -> ReadResult:
    return (await rig.device.read([u16("p", HoldingRegister(0))]))["p"]


async def test_a_device_backs_off_after_consecutive_no_answers() -> None:
    rig = Rig(silent(), backoff_after=3, backoff_for=60)
    for _ in range(2):
        await read0(rig)
    assert rig.device.diagnostics()["backing_off"] is False
    await read0(rig)
    assert rig.device.diagnostics()["backing_off"] is True
    assert len(rig.unit.requests) == 3


async def test_a_device_backing_off_answers_no_answer_without_touching_the_connection() -> None:
    rig = Rig(silent(), backoff_after=3, backoff_for=60)
    for _ in range(3):
        await read0(rig)
    rig.clock.advance(59)
    raw = await read0(rig)
    write = await rig.device.write(setting(HoldingRegister(0)), EncodedWrite((1,)))
    assert raw.outcome is Outcome.NO_ANSWER and "backing off" in raw.detail
    assert write.outcome is Outcome.NO_ANSWER and "backing off" in write.detail
    assert len(rig.gateway.requests) == 3


async def test_a_probe_after_the_backoff_that_gets_no_answer_backs_off_again_at_once() -> None:
    rig = Rig(silent(), backoff_after=3, backoff_for=60)
    for _ in range(3):
        await read0(rig)
    rig.clock.advance(60)
    assert (await read0(rig)).outcome is Outcome.NO_ANSWER      # the probe
    assert len(rig.unit.requests) == 4
    assert (await read0(rig)).outcome is Outcome.NO_ANSWER      # refused, not sent
    assert len(rig.unit.requests) == 4
    assert rig.device.diagnostics()["backing_off_for"] == 60


async def test_an_answered_probe_ends_the_backoff() -> None:
    rig = Rig(silent(), backoff_after=3, backoff_for=60)
    for _ in range(3):
        await read0(rig)
    rig.unit.faults.clear()
    rig.clock.advance(60)
    assert await read0(rig) == ok(1)
    assert rig.device.diagnostics()["consecutive_no_answers"] == 0
    rig.unit.faults[(FC.READ_HOLDING_REGISTERS, 0)] = NO_ANSWER
    await read0(rig)
    await read0(rig)
    assert rig.device.diagnostics()["backing_off"] is False           # the count started again


async def test_only_one_probe_goes_through_while_it_is_out() -> None:
    rig = Rig(silent(), backoff_after=1, backoff_for=10, delay=0.001)
    await read0(rig)
    rig.clock.advance(10)
    first, second = await asyncio.gather(read0(rig), read0(rig))
    assert "backing off" not in first.detail and "backing off" in second.detail
    assert len(rig.unit.requests) == 2


async def test_any_answer_resets_the_count_even_an_exception() -> None:
    rig = Rig(silent(), backoff_after=3)
    await read0(rig)
    await read0(rig)
    await rig.device.read([u16("absent", HoldingRegister(99))])            # 0x02 is an answer
    assert rig.device.diagnostics()["consecutive_no_answers"] == 0
    await read0(rig)
    await read0(rig)
    assert rig.device.diagnostics()["backing_off"] is False


async def test_a_link_that_is_down_counts_as_no_answer() -> None:
    rig = Rig(SimulatedModbusDevice(holding_registers={0: 1}), backoff_after=2)
    rig.gateway.link_down = True
    assert (await read0(rig)).outcome is Outcome.NO_ANSWER
    await read0(rig)
    assert rig.device.diagnostics()["backing_off"] is True


# ================================================================ several devices, one link


def two_devices(delay: float = 0.0) -> tuple[SimulatedModbusGateway, ModbusDevice, ModbusDevice, SimulatedModbusDevice, SimulatedModbusDevice]:
    one = SimulatedModbusDevice(holding_registers={0: 111, 1: 112})
    two = SimulatedModbusDevice(holding_registers={0: 221, 1: 222})
    gateway = SimulatedModbusGateway({1: one, 2: two}, delay=delay)
    clock = FakeClock()
    return (gateway, configured(ModbusDevice(gateway, 1, clock=clock, sleep=Sleeps(), backoff_after=2)),
            configured(ModbusDevice(gateway, 2, clock=clock, sleep=Sleeps(), backoff_after=2)), one, two)


async def test_each_device_reaches_its_own_unit() -> None:
    gateway, first, second, one, two = two_devices()
    points = [u16("a", HoldingRegister(0)), u16("b", HoldingRegister(1))]
    assert await first.read(points) == {"a": ok(111), "b": ok(112)}
    assert await second.read(points) == {"a": ok(221), "b": ok(222)}
    assert [unit for unit, _ in gateway.requests] == [1, 2]
    assert all(r.unit_id == 1 for r in one.requests) and all(r.unit_id == 2 for r in two.requests)


async def test_requests_from_several_devices_never_overlap() -> None:
    gateway, first, second, _, _ = two_devices(delay=0.001)
    gap = [u16("a", HoldingRegister(0)), Point(Key("c", bool), read=HoldingRegister(1), data_type=DataType.bit(0))]
    results = await asyncio.gather(*(device.read(gap) for device in (first, second, first, second)))
    assert all(r["a"].outcome is Outcome.OK for r in results)
    assert gateway.max_in_flight == 1
    assert {unit for unit, _ in gateway.requests} == {1, 2}


async def test_one_devices_fault_and_backoff_do_not_touch_the_other() -> None:
    gateway, first, second, _, two = two_devices()
    two.faults[(FC.READ_HOLDING_REGISTERS, 0)] = NO_ANSWER
    for _ in range(2):
        await second.read([u16("a", HoldingRegister(0))])
    assert second.diagnostics()["backing_off"] is True
    assert await first.read([u16("a", HoldingRegister(0))]) == {"a": ok(111)}
    assert first.diagnostics()["backing_off"] is False
    assert first.diagnostics()["outcomes"] == {**{o.name: 0 for o in Outcome}, "OK": 1}
    assert gateway.connected


async def test_a_non_owning_device_leaves_the_shared_link_open() -> None:
    gateway, first, second, _, _ = two_devices()
    assert await first.connect() == {} and await second.connect() == {}
    assert gateway.opens == 1
    await first.disconnect()
    assert gateway.connected and gateway.closes == 0
    assert (await second.read([u16("a", HoldingRegister(0))]))["a"] == ok(221)


async def test_an_owning_device_closes_its_link() -> None:
    gateway = SimulatedModbusGateway({1: SimulatedModbusDevice()})
    device = ModbusDevice(gateway, 1, owns_connection=True)
    assert await device.connect() == {} and gateway.connected
    await device.disconnect()
    assert not gateway.connected


async def test_connect_reports_an_unreachable_link_as_none() -> None:
    gateway = SimulatedModbusGateway({1: SimulatedModbusDevice()})
    gateway.link_down = True
    assert await ModbusDevice(gateway, 1).connect() is None


def test_tcp_owns_a_pymodbus_connection_and_passes_it_the_link_settings() -> None:
    clock = FakeClock()
    device = ModbusDevice.tcp(HOST, 1502, unit_id=4, timeout=1.5, frame_gap=0.02, clock=clock, busy_retries=1)
    connection = device._connection
    assert isinstance(connection, ModbusTcpConnection) and device._owns_connection
    assert (connection._timeout, connection._frame_gap, connection._clock) == (1.5, 0.02, clock)
    assert (device.unit_id, device._busy_retries, device._clock) == (4, 1, clock)
    assert connection.client is None                               # nothing opened yet


# =========================================================================== diagnostics


class Timed:
    """A connection that takes `seconds` of fake time per request."""

    def __init__(self, inner: SimulatedModbusGateway, clock: FakeClock, seconds: float) -> None:
        self.inner, self.clock, self.seconds = inner, clock, seconds

    @property
    def connected(self) -> bool:
        return self.inner.connected

    async def open(self) -> bool:
        return await self.inner.open()

    async def close(self) -> None:
        await self.inner.close()

    async def request(self, request: Request) -> Response:
        self.clock.advance(self.seconds)
        return await self.inner.request(request)


async def test_diagnostics_count_what_happened_and_hold_no_host() -> None:
    clock = FakeClock()
    unit = SimulatedModbusDevice(holding_registers={0: 1}, busy_for=1)
    device = configured(ModbusDevice(Timed(SimulatedModbusGateway({1: unit}), clock, 0.05), 1, clock=clock, sleep=Sleeps()))
    await device.read([u16("a", HoldingRegister(0))])                       # busy, then OK
    await device.read([u16("b", HoldingRegister(5))])                       # missing
    d = device.diagnostics()
    assert d["requests"] == 3 and d["busy_retries"] == 1
    assert d["outcomes"] == {**{o.name: 0 for o in Outcome}, "OK": 1, "MISSING": 1}
    assert d["consecutive_no_answers"] == 0 and d["backing_off"] is False and d["backing_off_for"] == 0.0
    latency = d["average_latency"]
    assert isinstance(latency, float) and abs(latency - 0.05) < 1e-9


class LeakyClient:
    """A pymodbus client whose every failure names the host, as real ones do."""
    connected = True

    async def connect(self) -> bool:
        return True

    def close(self) -> None:
        pass

    async def read_holding_registers(self, address: int, *, count: int = 1, device_id: int = 1) -> Any:
        raise ConnectionError(f"Connection to ({HOST}, 1502) lost")

    async def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        raise TimeoutError(f"no reply from {HOST}:1502")


async def test_nothing_a_device_reports_names_its_host() -> None:
    device = configured(ModbusDevice.tcp(HOST, 1502, clock=FakeClock(), sleep=Sleeps()))
    assert isinstance(device._connection, ModbusTcpConnection)
    device._connection._client = LeakyClient()                      # no network
    raw = (await device.read([u16("a", HoldingRegister(0))]))["a"]
    write = await device.write(setting(HoldingRegister(0)), EncodedWrite((1,)))
    assert raw.outcome is Outcome.NO_ANSWER and write.outcome is Outcome.NO_ANSWER
    for text in (raw.detail, write.detail, repr(device.diagnostics())):
        assert HOST not in text and "1502" not in text


def test_a_modbus_device_is_a_device_protocol() -> None:
    assert isinstance(ModbusDevice(SimulatedModbusGateway({})), Device)


# ============================================================================ simulators


async def test_the_simulated_unit_applies_mask_write_as_the_specification_says() -> None:
    unit = SimulatedModbusDevice(holding_registers={0: 0x12})
    # Modbus 6.16: result = (current AND and_mask) OR (or_mask AND NOT and_mask)
    unit.handle(Request(1, FC.MASK_WRITE_REGISTER, 0, and_mask=0xF2, or_mask=0x25))
    assert unit.holding_registers[0] == 0x17


async def test_the_simulated_gateway_records_requests_in_order() -> None:
    gateway = SimulatedModbusGateway({1: SimulatedModbusDevice(holding_registers={0: 1}), 2: SimulatedModbusDevice(holding_registers={0: 2})})
    r1 = Request(2, FC.READ_HOLDING_REGISTERS, 0, count=1)
    r2 = Request(1, FC.READ_HOLDING_REGISTERS, 0, count=1)
    assert (await gateway.request(r1)).registers == (2,)
    assert (await gateway.request(r2)).registers == (1,)
    assert gateway.requests == [(2, r1), (1, r2)]
