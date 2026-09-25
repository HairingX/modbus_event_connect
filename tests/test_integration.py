"""The whole stack at once: client, data_type, scheduler, Modbus device and a simulated gateway,
proving the layers agree with each other end to end."""
import asyncio
import math
import struct
from enum import IntEnum
from collections.abc import Awaitable
from typing import Any, TypeVar

import pytest

from modbus_event_connect._client import Client, Status
from modbus_event_connect._data_type import DataType
from modbus_event_connect._errors import CannotConnectError, InvalidValueError, ReadOnlyError
from modbus_event_connect._key import Key
from modbus_event_connect._model import Model, RepeatedSection, Section
from modbus_event_connect._point import Labels, Point, PollRate, Pulse, Refresh, WriteKind
from modbus_event_connect._unit import Unit
from modbus_event_connect._value import DataValue, Quality
from modbus_event_connect.modbus import (
    Coil,
    DiscreteInput,
    FunctionCode,
    HoldingRegister,
    InputRegister,
    ModbusDevice,
    ModbusOptions,
    plain,
)
from modbus_event_connect.testing import SimulatedModbusDevice, SimulatedModbusGateway
from modbus_event_connect.testing._clock import FakeClock

T = TypeVar("T")


def _room(n: int) -> list[Point[Any]]:
    return [Point(Key(f"room_{n}_temp", float), read=InputRegister(100 + n), data_type=DataType.INT16, scale=0.1,
                  valid_raw=range(-0x8000, 0x7FFF), unit=Unit.CELSIUS)]


POWER = Key("power", float)
ENERGY = Key("energy", float)
TEMP = Key("temp", float)
FAN_RPM = Key("fan_rpm", int)
STATUS_WORD = Key("status_word", int)
LAMP_ON = Key("lamp_on", bool)
FAULT = Key("fault", bool)
NAME = Key("name", str)
RELAY = Key("relay", bool)
BLIND_UP = Key("blind_up", bool)
ALARM_ANY = Key("alarm_any", bool)
ALARM_1 = Key("alarm_1", bool)


class FanSpeed(IntEnum):
    OFF = 0
    LOW = 1
    HIGH = 2


FAN_SPEED = Key("fan_speed", FanSpeed)
ALARM_2 = Key("alarm_2", bool)


MODEL = Model(
    name="BMS", manufacturer="TEST",
    options=ModbusOptions(numbering=plain(first_address=0), max_registers=8),
    sections=[
        Section([
            Point(POWER, read=InputRegister(1), data_type=DataType.FLOAT32, unit=Unit.WATT),
            Point(ENERGY, read=InputRegister(3), data_type=DataType.UINT32, scale=0.1, unit=Unit.KILOWATT_HOUR),
            Point(TEMP, read=InputRegister(5), data_type=DataType.INT16, scale=0.1, valid_raw=range(-0x8000, 0x7FFF)),
            Point(FAN_RPM, read=InputRegister(6), unit=Unit.RPM),
            Point(STATUS_WORD, read=HoldingRegister(10)),
            Point(LAMP_ON, read=HoldingRegister(10), write=HoldingRegister(10), data_type=DataType.bit(0)),
            Point(FAULT, read=HoldingRegister(10), data_type=DataType.bit(3)),
            Point(FAN_SPEED, read=HoldingRegister(11), write=HoldingRegister(11),
                  on_write=Refresh(["fan_rpm"], after=1.0, until_stable=5.0)),
            Point(NAME, read=HoldingRegister(20), write=HoldingRegister(20), data_type=DataType.string(4)),
            Point(RELAY, read=Coil(1), write=Coil(1), data_type=DataType.BOOL),
            Point(BLIND_UP, write=Coil(5), data_type=DataType.BOOL, write_kind=WriteKind.COMMAND,
                  pulse=Pulse(idle=False, after=0.01)),
            Point(ALARM_ANY, read=DiscreteInput(1), data_type=DataType.BOOL, poll_rate=PollRate.FAST,
                  on_change=Refresh(Labels(kind="alarm"))),
            Point(ALARM_1, read=DiscreteInput(2), data_type=DataType.BOOL, poll_rate=PollRate.STATIC, labels={"kind": "alarm"}),
            Point(ALARM_2, read=DiscreteInput(3), data_type=DataType.BOOL, poll_rate=PollRate.STATIC, labels={"kind": "alarm"}),
        ]),
        RepeatedSection(_room, range(1, 4), label="room"),
    ], read_back_after=1.0
)


def _f32(value: float) -> tuple[int, int]:
    high, low = struct.unpack(">HH", struct.pack(">f", value))
    return int(high), int(low)


def _unit() -> SimulatedModbusDevice:
    """The device. Wire addresses: the manual's number minus one. Room 2 is not installed."""
    power = _f32(1234.5)
    return SimulatedModbusDevice(
        input_registers={
            0: power[0], 1: power[1],           # power, F32
            2: 0x0001, 3: 0xE240,               # energy raw 123456 -> 12345.6 kWh
            4: 215,                              # temp 21.5
            5: 900,                              # fan_rpm
            100: 201, 102: 203,                  # rooms 1 and 3
        },
        holding_registers={9: 0b1000, 10: 1, 19: 0x4142, 20: 0, 21: 0, 22: 0},    # fault bit set, fan low, "AB"
        coils={0: 0, 4: 0},
        discrete_inputs={0: 0, 1: 0, 2: 0},
    )


async def _no_wait(seconds: float) -> None:
    return None


def _stack(units: dict[int, SimulatedModbusDevice] | None = None, *, unit_id: int = 1,
           read_only: bool = False, delay: float = 0.0) -> tuple[Client, SimulatedModbusGateway, FakeClock, ModbusDevice]:
    gateway = SimulatedModbusGateway(units if units is not None else {1: _unit()}, delay=delay)
    clock = FakeClock()
    device = ModbusDevice(gateway, unit_id=unit_id, clock=clock, sleep=_no_wait,
                          backoff_after=2, backoff_for=30.0)
    return Client(device, MODEL, clock=clock, read_only=read_only), gateway, clock, device


def _connected(units: dict[int, SimulatedModbusDevice] | None = None, *, unit_id: int = 1,
               read_only: bool = False) -> tuple[Client, SimulatedModbusGateway, FakeClock, ModbusDevice]:
    stack = _stack(units, unit_id=unit_id, read_only=read_only)
    asyncio.run(stack[0].connect())
    return stack


def _connected_with(*, input_registers: dict[int, int] | None = None,
                    holding_registers: dict[int, int] | None = None,
                    ) -> tuple[Client, SimulatedModbusGateway, FakeClock, ModbusDevice]:
    """Connected to a device whose registers differ from the standard ones where given."""
    unit = _unit()
    unit.input_registers.update(input_registers or {})
    unit.holding_registers.update(holding_registers or {})
    return _connected({1: unit})


def within(seconds: float, awaitable: Awaitable[T]) -> T:
    """Run to completion or fail: a deadlock must fail the test, not hang the suite."""
    return asyncio.run(asyncio.wait_for(awaitable, seconds))  # type: ignore[arg-type]


def _value[V](client: Client, key: Key[V]) -> DataValue[V]:
    current = client.value(key)
    assert current is not None, f"{key} was never read"
    return current


def _nothing(key: str, old: DataValue[Any] | None, new: DataValue[Any]) -> None:
    """A subscriber that only needs to exist."""


# =============================================================== from the wire to the value

def test_values_arrive_decoded_through_every_layer() -> None:
    client, _, _, _ = _connected()
    expected: dict[Key[Any], object] = {
        POWER: 1234.5, ENERGY: 12345.6, TEMP: 21.5, FAN_RPM: 900, STATUS_WORD: 8,
        LAMP_ON: False, FAULT: True, FAN_SPEED: FanSpeed.LOW, NAME: "AB", RELAY: False,
        ALARM_ANY: False, Key("room_1_temp", float): 20.1, Key("room_3_temp", float): 20.3,
    }
    for key, value in expected.items():
        current = _value(client, key)
        assert (current.value, current.quality) == (value, Quality.GOOD), key


def test_one_based_numbers_are_sent_one_lower() -> None:
    _, gateway, _, _ = _connected()
    starts = {(request.function, request.address) for _, request in gateway.requests}
    assert (FunctionCode.READ_INPUT_REGISTERS, 0) in starts, "InputRegister(1) is address 0"
    assert (FunctionCode.READ_HOLDING_REGISTERS, 9) in starts, "HoldingRegister(10) is address 9"
    assert (FunctionCode.READ_COILS, 0) in starts
    assert (FunctionCode.READ_DISCRETE_INPUTS, 0) in starts


def test_points_sharing_a_register_cost_one_request() -> None:
    """status_word, lamp_on and fault are three views of holding register 9."""
    _, gateway, _, _ = _connected()
    touching = [r for _, r in gateway.requests
                if r.function is FunctionCode.READ_HOLDING_REGISTERS and r.address <= 9 < r.address + r.count]
    assert len(touching) == 1


def test_no_request_exceeds_the_limit_the_model_declares() -> None:
    _, gateway, _, _ = _connected()
    assert all(r.count <= 8 for _, r in gateway.requests if r.function.is_read)


# ========================================================= values that must not be misread

def test_negative_zero_keeps_its_sign_through_the_whole_stack() -> None:
    client, _, _, _ = _connected_with(input_registers={0: 0x8000, 1: 0x0000})
    power = _value(client, POWER)
    assert power.value == 0.0 and isinstance(power.value, float)
    assert math.copysign(1.0, power.value) == -1.0, "-0.0 became +0.0 somewhere on the way up"


@pytest.mark.parametrize("registers", [(0x7FC0, 0x0000), (0x7F80, 0x0000), (0xFF80, 0x0000)],
                         ids=["nan", "+inf", "-inf"])
def test_a_meter_answering_nan_or_infinity_reads_as_no_data(registers: tuple[int, int]) -> None:
    client, _, _, _ = _connected_with(input_registers={0: registers[0], 1: registers[1]})
    power = _value(client, POWER)
    assert (power.value, power.quality) == (None, Quality.NO_DATA)


def test_a_sensor_sentinel_reads_as_no_data_not_as_a_temperature() -> None:
    client, _, _, _ = _connected_with(input_registers={4: 0x7FFF})
    temp = _value(client, TEMP)
    assert (temp.value, temp.quality) == (None, Quality.NO_DATA), "3276.7 °C is not a reading"


def test_a_negative_temperature_is_not_read_as_a_huge_positive_one() -> None:
    client, _, _, _ = _connected_with(input_registers={4: 0xFF9C})
    assert _value(client, TEMP).value == -10.0


def test_an_unknown_fan_state_reads_as_no_data_and_keeps_what_the_device_said() -> None:
    client, _, _, _ = _connected_with(holding_registers={10: 7})
    fan = _value(client, FAN_SPEED)
    assert (fan.value, fan.quality, fan.raw) == (None, Quality.NO_DATA, (7,))


# ========================================================================= availability

def test_an_absent_room_is_found_once_and_never_asked_for_again() -> None:
    client, gateway, clock, _ = _connected()
    assert "room_2_temp" not in client.points
    assert client.instances("room") == (1, 3)

    for key in (Key("room_1_temp", float), Key("room_3_temp", float)):
        client.subscribe(key, _nothing)
    gateway.requests.clear()
    clock.advance(60)
    asyncio.run(client.poll())
    assert gateway.requests, "the installed rooms are read"
    assert not any(r.address <= 101 < r.address + r.count for _, r in gateway.requests
                   if r.function is FunctionCode.READ_INPUT_REGISTERS)


def test_an_offline_peripheral_is_offline_and_stays_polled() -> None:
    """0x04 clears the whole batch it came back for.

    The registers exist; the device behind them does not answer.
    """
    client, gateway, clock, _ = _connected()
    client.subscribe(TEMP, _nothing)
    gateway.units[1].faults[(FunctionCode.READ_INPUT_REGISTERS, 4)] = 0x04
    clock.advance(60)
    asyncio.run(client.poll())
    assert _value(client, TEMP).quality is Quality.OFFLINE
    assert client.has("temp")


def test_a_device_the_gateway_cannot_reach_does_not_connect() -> None:
    """0x0B is the gateway reporting that the device behind it did not answer."""
    client, gateway, _, _ = _stack(unit_id=7)
    with pytest.raises(CannotConnectError):
        asyncio.run(client.connect())
    assert client.status(Status.CONNECTED).value is False
    assert gateway.requests, "the gateway was never asked"


def test_a_half_open_link_is_noticed_and_recovered_from() -> None:
    """A pulled cable leaves the socket open. Only the missing answers can tell."""
    client, gateway, clock, device = _connected()
    client.subscribe(TEMP, _nothing)
    gateway.half_open = True
    for _ in range(3):
        clock.advance(60)
        asyncio.run(client.poll())
    assert gateway.connected is True, "test premise: the link still reports itself connected"
    assert client.status(Status.CONNECTED).value is False
    assert _value(client, TEMP).quality is Quality.STALE
    assert device.diagnostics()["backing_off"] is True

    gateway.half_open = False
    clock.advance(30)
    asyncio.run(client.poll())                       # the first answer: reachable again
    assert client.status(Status.CONNECTED).value is True
    asyncio.run(client.poll())                       # and everything is due
    assert _value(client, TEMP).quality is Quality.GOOD


def test_a_busy_device_is_waited_out() -> None:
    client, gateway, clock, _ = _connected()
    client.subscribe(TEMP, _nothing)
    gateway.units[1].input_registers[4] = 222
    gateway.units[1].busy_for = 2
    clock.advance(60)
    asyncio.run(client.poll())
    temp = _value(client, TEMP)
    assert (temp.value, temp.quality) == (22.2, Quality.GOOD)


def test_a_pulled_cable_goes_stale_backs_off_and_recovers() -> None:
    client, gateway, clock, device = _connected()
    client.subscribe(TEMP, _nothing)
    good = _value(client, TEMP)

    gateway.link_down = True
    for _ in range(3):
        clock.advance(60)
        asyncio.run(client.poll())
    stale = _value(client, TEMP)
    assert (stale.value, stale.quality, stale.timestamp) == (good.value, Quality.STALE, good.timestamp)
    assert client.status(Status.CONNECTED).value is False
    diagnostics = device.diagnostics()
    assert diagnostics["backing_off"] is True
    assert isinstance(diagnostics["refused_while_backing_off"], int) and diagnostics["refused_while_backing_off"] > 0

    gateway.link_down = False
    gateway.units[1].input_registers[4] = 230
    clock.advance(30)
    asyncio.run(client.poll())
    asyncio.run(client.poll())
    temp = _value(client, TEMP)
    assert (temp.value, temp.quality) == (23.0, Quality.GOOD)
    assert client.status(Status.CONNECTED).value is True


# ================================================================================= writes

def _writes(gateway: SimulatedModbusGateway) -> list[tuple[FunctionCode, int]]:
    return [(r.function, r.address) for _, r in gateway.requests if not r.function.is_read]


def test_a_lamp_bit_is_set_atomically_leaving_its_neighbours() -> None:
    client, gateway, clock, _ = _connected()
    client.subscribe(LAMP_ON, _nothing)
    client.subscribe(FAULT, _nothing)
    gateway.requests.clear()

    assert asyncio.run(client.write(LAMP_ON, True)) is True

    [(_, mask)] = [(u, r) for u, r in gateway.requests if r.function is FunctionCode.MASK_WRITE_REGISTER]
    assert (mask.address, mask.and_mask, mask.or_mask) == (9, 0xFFFE, 0x0001)
    assert gateway.units[1].holding_registers[9] == 0b1001, "the fault bit next to it was disturbed"
    clock.advance(MODEL.read_back_after)
    asyncio.run(client.poll())
    assert _value(client, LAMP_ON).value is True
    assert _value(client, FAULT).value is True


def test_a_relay_is_written_as_a_coil_and_nothing_else() -> None:
    client, gateway, _, _ = _connected()
    holding_before = dict(gateway.units[1].holding_registers)
    gateway.requests.clear()
    assert asyncio.run(client.write(RELAY, True)) is True
    assert _writes(gateway) == [(FunctionCode.WRITE_SINGLE_COIL, 0)]
    assert gateway.units[1].coils[0] == 1
    assert gateway.units[1].holding_registers == holding_before


def test_a_fan_state_is_written_by_name_and_its_effects_followed() -> None:
    client, gateway, clock, _ = _connected()
    client.subscribe(FAN_RPM, _nothing)
    gateway.requests.clear()
    assert asyncio.run(client.write(FAN_SPEED, FanSpeed.HIGH)) is True
    assert _writes(gateway) == [(FunctionCode.WRITE_SINGLE_REGISTER, 10)]
    assert gateway.units[1].holding_registers[10] == 2

    seen: list[object] = []
    for rpm in (1200, 1500, 1500):                           # ramping, then steady
        gateway.units[1].input_registers[5] = rpm
        clock.advance(1.0)
        asyncio.run(client.poll())
        seen.append(_value(client, FAN_RPM).value)
    assert seen == [1200, 1500, 1500], "the ramp was followed, not caught once halfway"


def test_text_is_written_in_one_request() -> None:
    client, gateway, _, _ = _connected()
    gateway.requests.clear()
    assert asyncio.run(client.write(NAME, "Hi")) is True
    [(_, write)] = [(u, r) for u, r in gateway.requests if not r.function.is_read]
    assert (write.function, write.address, write.values) == \
           (FunctionCode.WRITE_MULTIPLE_REGISTERS, 19, (0x4869, 0, 0, 0))


@pytest.mark.parametrize("key,value", [(FAN_SPEED, 7), (NAME, "far too long text"),
                                       (LAMP_ON, 2), (RELAY, "on")])
def test_a_value_the_point_refuses_never_reaches_the_wire(key: Key[Any], value: Any) -> None:
    client, gateway, _, _ = _connected()
    gateway.requests.clear()
    with pytest.raises(InvalidValueError):
        asyncio.run(client.write(key, value))
    assert gateway.requests == []


def test_a_read_only_client_never_reaches_the_wire() -> None:
    client, gateway, _, _ = _connected(read_only=True)
    gateway.requests.clear()
    with pytest.raises(ReadOnlyError):
        asyncio.run(client.write(RELAY, True))
    assert _writes(gateway) == []


def test_a_blind_command_pulses_and_comes_back_to_rest() -> None:
    client, gateway, _, _ = _connected()
    gateway.requests.clear()

    async def press() -> None:
        await client.write(BLIND_UP, True)
        await asyncio.sleep(0.05)
    within(2, press())
    assert _writes(gateway) == [(FunctionCode.WRITE_SINGLE_COIL, 4)] * 2
    assert gateway.units[1].coils[4] == 0


def test_a_refused_write_is_reported_and_leaves_the_device_unchanged() -> None:
    client, gateway, _, _ = _connected()
    gateway.units[1].faults[(FunctionCode.WRITE_SINGLE_REGISTER, 10)] = 0x04
    assert asyncio.run(client.write(FAN_SPEED, FanSpeed.HIGH)) is False
    assert gateway.units[1].holding_registers[10] == 1
    assert client.status(Status.WRITE_PENDING).value is False


# ======================================================================= shared gateway

def test_two_devices_share_one_gateway_without_mixing_up() -> None:
    first, second = _unit(), _unit()
    second.input_registers[4] = 190
    gateway = SimulatedModbusGateway({1: first, 2: second}, delay=0.001)
    clock = FakeClock()
    clients = [Client(ModbusDevice(gateway, unit_id=u, clock=clock, sleep=_no_wait), MODEL, clock=clock)
               for u in (1, 2)]

    async def scenario() -> None:
        # One event loop for the whole scenario: the shared gateway's lock belongs
        # to the loop that first used it.
        await asyncio.gather(*(c.connect() for c in clients))
        assert _value(clients[0], TEMP).value == 21.5
        assert _value(clients[1], TEMP).value == 19.0

        second.faults[(FunctionCode.READ_INPUT_REGISTERS, 4)] = 0x04
        for client in clients:
            client.subscribe(TEMP, _nothing)
        clock.advance(60)
        await asyncio.gather(*(c.poll() for c in clients))
    within(5, scenario())

    assert gateway.max_in_flight == 1, "two requests were on the wire at once"
    assert _value(clients[0], TEMP).quality is Quality.GOOD, "one unit's fault reached the other"
    assert _value(clients[1], TEMP).quality is Quality.OFFLINE


# ============================================================================= triggers

def test_an_alarm_summary_brings_the_alarms_in() -> None:
    client, gateway, clock, _ = _connected()
    for key in (ALARM_1, ALARM_2):
        client.subscribe(key, _nothing)
    gateway.units[1].discrete_inputs[0] = 1
    gateway.units[1].discrete_inputs[2] = 1
    clock.advance(10)
    asyncio.run(client.poll())                          # the summary rises
    asyncio.run(client.poll())                          # the alarms follow
    assert _value(client, ALARM_ANY).value is True
    assert _value(client, ALARM_2).value is True
    assert _value(client, ALARM_1).value is False


def test_status_follows_the_connection() -> None:
    client, gateway, clock, _ = _stack()
    seen: list[object] = []
    client.subscribe_status(Status.CONNECTED, lambda k, o, n: seen.append(n.value))
    asyncio.run(client.connect())
    client.subscribe(TEMP, _nothing)
    gateway.link_down = True
    clock.advance(60)
    asyncio.run(client.poll())
    assert seen == [False, True, False]


# ===================================================== regression scenarios

def test_a_slow_device_does_not_block_the_event_loop() -> None:
    """Everything else on the host's loop must keep running while the bus is slow."""
    gateway = SimulatedModbusGateway({1: _unit()}, delay=0.05)
    clock = FakeClock()
    client = Client(ModbusDevice(gateway, unit_id=1, clock=clock, sleep=_no_wait), MODEL, clock=clock)

    async def scenario() -> int:
        stop = asyncio.Event()
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)
        beating = asyncio.create_task(heartbeat())
        await client.connect()                               # several requests, 50 ms each
        stop.set()
        await beating
        return ticks
    assert within(10, scenario()) >= 10, "the loop stood still while the device was read"


def test_writes_land_in_order_even_when_the_device_is_busy() -> None:
    """A write refused as busy is retried before later writes go out, never after them."""
    client, gateway, _, _ = _connected()
    gateway.units[1].busy_for = 3

    async def presses() -> None:
        await asyncio.gather(*(client.write(NAME, text) for text in ("A", "B", "C", "D")))
    within(5, presses())
    assert gateway.units[1].holding_registers[19] == 0x4400, "the device holds an earlier value than the last one asked for"
