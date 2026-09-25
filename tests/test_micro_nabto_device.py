"""`MicroNabtoDevice`, alone and under a client, against a simulated device on localhost UDP."""
import asyncio

import pytest

from src.modbus_event_connect.client import Client
from src.modbus_event_connect.data_type import DataType
from src.modbus_event_connect.device import Device, EncodedWrite, Identity, Outcome
from src.modbus_event_connect.errors import (
    AuthenticationError,
    ReadOnlyError,
    UnsupportedDeviceError,
)
from src.modbus_event_connect.micro_nabto.access import DatapointRegister, SetpointRegister
from src.modbus_event_connect.micro_nabto.connection import MicroNabtoConnection
from src.modbus_event_connect.micro_nabto.device import MicroNabtoDevice, MicroNabtoOptions
from src.modbus_event_connect.modbus.access import HoldingRegister, ModbusOptions, plain
from src.modbus_event_connect.model import Model, Section
from src.modbus_event_connect.point import Limits, Point
from src.modbus_event_connect.testing.clock import FakeClock
from src.modbus_event_connect.testing.micro_nabto import Command, SimulatedMicroNabtoDevice
from src.modbus_event_connect.unit import Unit
from src.modbus_event_connect.value import DataValue, Quality

EMAIL = "user@example.invalid"
DATAPOINT_READ, SETPOINT_READ, SETPOINT_WRITE = 0x2D, 0x2A, 0x2B


def _dp(address: int, *, obj: int = 0, data_type: DataType = DataType.UINT16) -> Point:
    return Point(f"dp_{obj}_{address}", read=DatapointRegister(address, obj=obj), data_type=data_type)


def _sp(address: int, *, data_type: DataType = DataType.UINT16) -> Point:
    return Point(f"sp_{address}", read=SetpointRegister(address), write=SetpointRegister(address), data_type=data_type)


def _simulated(*, clock: FakeClock | None = None, emails: frozenset[str] = frozenset({EMAIL})) -> SimulatedMicroNabtoDevice:
    return SimulatedMicroNabtoDevice(
        emails=emails, clock=clock,
        datapoint_registers={**{(0, a): a for a in range(1, 101)}, (1, 5): 105, (0, 27): 0xFFCB},
        setpoint_registers={(0, a): 100 + a for a in range(30, 40)})


def _device(simulated: SimulatedMicroNabtoDevice, *, clock: FakeClock | None = None,
            email: str = EMAIL) -> MicroNabtoDevice:
    host, port = simulated.address
    device = MicroNabtoDevice(MicroNabtoConnection(email, host=host, port=port, timeout=0.05, retries=1,
                                                   clock=clock), owns_connection=True)
    device.configure(MicroNabtoOptions())
    return device


async def _arrived(simulated: SimulatedMicroNabtoDevice, code: int) -> list[Command]:
    """A write gets no answer to wait for, so wait for the device to have received it."""
    for _ in range(100):
        if simulated.received(code):
            break
        await asyncio.sleep(0.01)
    return simulated.received(code)


def _nothing(key: str, old: DataValue | None, new: DataValue) -> None:
    pass


async def _connected(simulated: SimulatedMicroNabtoDevice) -> MicroNabtoDevice:
    device = _device(simulated)
    assert await device.connect() is not None
    return device


# ================================================================================ contract

def test_a_micro_nabto_device_is_a_device_protocol() -> None:
    assert isinstance(MicroNabtoDevice.udp(EMAIL, host="device.invalid"), Device)


async def test_connect_returns_the_identity_from_the_handshake() -> None:
    async with _simulated() as simulated:
        identity = await _device(simulated).connect()
        assert identity is not None and identity["device_model"] == 1140


async def test_connect_to_a_silent_device_returns_none() -> None:
    async with _simulated() as simulated:
        simulated.silent = True
        assert await _device(simulated).connect() is None


async def test_a_device_reads_nothing_before_it_knows_the_models_options() -> None:
    async with _simulated() as simulated:
        host, port = simulated.address
        device = MicroNabtoDevice(MicroNabtoConnection(EMAIL, host=host, port=port, timeout=0.05, retries=1),
                                  owns_connection=True)
        assert device.options is None
        with pytest.raises(RuntimeError):
            await device.read([_dp(1)])
        assert simulated.received(DATAPOINT_READ) == []
        await device.disconnect()


async def test_options_of_another_protocol_are_refused() -> None:
    async with _simulated() as simulated:
        with pytest.raises(TypeError):
            _device(simulated).configure(ModbusOptions(numbering=plain(first_address=1)))


# ================================================================================= reading

async def test_datapoints_and_setpoints_are_read_with_one_request_each() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        answers = await device.read([_dp(1), _dp(2), _sp(30), _dp(3), _sp(31)])
        assert {k: a.registers for k, a in answers.items()} == {
            "dp_0_1": (1,), "dp_0_2": (2,), "dp_0_3": (3,), "sp_30": (130,), "sp_31": (131,)}
        assert simulated.received(DATAPOINT_READ) == [Command(DATAPOINT_READ, ((0, 1), (0, 2), (0, 3)))]
        assert simulated.received(SETPOINT_READ) == [Command(SETPOINT_READ, ((0, 30), (0, 31)))]


async def test_an_address_the_device_lacks_is_missing_and_the_rest_are_still_read() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        answers = await device.read([_dp(1), _dp(999), _dp(2)])
        assert [answers[k].outcome for k in ("dp_0_1", "dp_0_999", "dp_0_2")] == \
               [Outcome.OK, Outcome.MISSING, Outcome.OK]
        assert [c.items for c in simulated.received(DATAPOINT_READ)] == [
            ((0, 1), (0, 999), (0, 2)), ((0, 1),), ((0, 999),), ((0, 2),)]


async def test_a_read_is_split_at_max_points() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        device.configure(MicroNabtoOptions(max_registers=64))
        answers = await device.read([_dp(a) for a in range(1, 101)])
        assert all(a.outcome is Outcome.OK for a in answers.values()) and len(answers) == 100
        assert [len(c.items) for c in simulated.received(DATAPOINT_READ)] == [64, 36]


async def test_each_object_is_read_on_its_own() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        answers = await device.read([_dp(5), _dp(5, obj=1)])
        assert (answers["dp_0_5"].registers, answers["dp_1_5"].registers) == ((5,), (105,))
        assert len(simulated.received(DATAPOINT_READ)) == 2


async def test_a_two_register_point_reads_two_consecutive_addresses() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        answers = await device.read([_dp(7, data_type=DataType.UINT32)])
        assert answers["dp_0_7"].registers == (7, 8)
        assert simulated.received(DATAPOINT_READ)[0].items == ((0, 7), (0, 8))


async def test_a_silent_device_leaves_every_point_unanswered() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        simulated.silent = True
        answers = await device.read([_dp(1), _sp(30)])
        assert {a.outcome for a in answers.values()} == {Outcome.NO_ANSWER}


async def test_an_answer_cut_short_is_an_error_and_not_a_missing_point() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        simulated.cut_short = True
        answers = await device.read([_dp(1), _dp(2)])
        assert {a.outcome for a in answers.values()} == {Outcome.ERROR}


async def test_a_point_of_another_protocol_cannot_be_read() -> None:
    async with _simulated() as simulated:
        with pytest.raises(TypeError):
            await _device(simulated).read([Point("modbus", read=HoldingRegister(1), data_type=DataType.UINT16)])


# ================================================================================= writing

async def test_a_setpoint_write_names_object_address_and_register() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        result = await device.write(_sp(30), EncodedWrite((5,)))
        assert result.outcome is Outcome.OK
        assert await _arrived(simulated, SETPOINT_WRITE) == [Command(SETPOINT_WRITE, ((0, 30, 5),))]
        assert simulated.setpoint_registers[(0, 30)] == 5


async def test_a_two_register_write_names_consecutive_addresses() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        await device.write(_sp(31, data_type=DataType.UINT32), EncodedWrite((1, 2)))
        assert (await _arrived(simulated, SETPOINT_WRITE))[0].items == ((0, 31, 1), (0, 32, 2))


async def test_a_write_to_an_unreachable_device_is_not_reported_sent() -> None:
    async with _simulated() as simulated:
        simulated.silent = True
        result = await _device(simulated).write(_sp(30), EncodedWrite((5,)))
        assert result.outcome is Outcome.NO_ANSWER


async def test_only_a_setpoint_is_written_and_never_a_single_bit() -> None:
    async with _simulated() as simulated:
        device = _device(simulated)
        with pytest.raises(TypeError):
            await device.write(Point("modbus", read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.UINT16),
                               EncodedWrite((1,)))
        with pytest.raises(ValueError):
            await device.write(_sp(30), EncodedWrite(bit_index=0, bit_value=True))


# ================================================================================= options

@pytest.mark.parametrize("point,problem", [
    (Point("p", read=SetpointRegister(0xFFFF), data_type=DataType.UINT16), None),
    (Point("p", read=SetpointRegister(0xFFFF), data_type=DataType.UINT32), "ends past address 65535"),
    (Point("p", read=DatapointRegister(0x1_0000), data_type=DataType.UINT32), None),
    (Point("p", read=DatapointRegister(1, obj=256), data_type=DataType.UINT16), "object 256"),
    (Point("p", read=HoldingRegister(1), data_type=DataType.UINT16), "not a micro_nabto space"),
    (Point("p", read=SetpointRegister(1), write=SetpointRegister(1), data_type=DataType.bit(3)), "single bit"),
    (Point("p", read=DatapointRegister(1), data_type=DataType.string(65)), "spans 65, more than the 64"),
])
def test_the_options_name_what_micro_nabto_cannot_carry(point: Point, problem: str | None) -> None:
    found = MicroNabtoOptions().problems(point)
    assert (found == []) if problem is None else any(problem in p for p in found), found


def test_max_registers_must_be_positive() -> None:
    with pytest.raises(ValueError):
        MicroNabtoOptions(max_registers=0)


async def test_a_device_leaves_a_given_connection_open_and_closes_its_own() -> None:
    async with _simulated() as simulated:
        host, port = simulated.address
        connection = MicroNabtoConnection(EMAIL, host=host, port=port, timeout=0.05, retries=1)
        given = MicroNabtoDevice(connection)
        assert await given.connect() is not None
        await given.disconnect()
        assert connection.connected
        await connection.close()

        own = MicroNabtoDevice.udp(EMAIL, host=host, port=port, timeout=0.05, retries=1)
        assert await own.connect() is not None
        await own.disconnect()
        assert not own._connection.connected


async def test_diagnostics_count_without_naming_the_address_or_the_email() -> None:
    async with _simulated() as simulated:
        device = await _connected(simulated)
        await device.read([_dp(1), _dp(999)])
        diagnostics = repr(device.diagnostics())
        host, port = simulated.address
        assert "isolated_reads': 1" in diagnostics
        assert host not in diagnostics and str(port) not in diagnostics and EMAIL not in diagnostics


# =============================================================================== the client

TEMP = Point("temp_outside", read=DatapointRegister(27), data_type=DataType.INT16, scale=0.1, unit=Unit.CELSIUS)
FAN = Point("fan_level", read=SetpointRegister(30), write=SetpointRegister(30), data_type=DataType.UINT16, limits=Limits(min=0, max=200))
CTS = Model(name="CTS", manufacturer="Example", sections=[Section([TEMP, FAN])], options=MicroNabtoOptions(), read_back_after=1.0)


def _select(identity: Identity) -> Model | None:
    return CTS if identity.get("device_model") == 1140 else None


def _client(simulated: SimulatedMicroNabtoDevice, *, clock: FakeClock | None = None, read_only: bool = False,
            email: str = EMAIL) -> Client:
    return Client(_device(simulated, clock=clock, email=email), _select, clock=clock, read_only=read_only)


async def test_a_client_picks_its_model_from_the_handshake_and_reads_negative_values() -> None:
    async with _simulated() as simulated:
        client = _client(simulated)
        await client.connect()
        temp, fan = client.value("temp_outside"), client.value("fan_level")
        assert temp is not None and (temp.value, temp.quality) == (-5.3, Quality.GOOD)
        assert fan is not None and fan.value == 130


async def test_a_device_no_model_matches_is_unsupported() -> None:
    async with _simulated() as simulated:
        simulated.identity["device_model"] = 1
        with pytest.raises(UnsupportedDeviceError):
            await _client(simulated).connect()


async def test_a_refused_email_fails_the_connect() -> None:
    async with _simulated(emails=frozenset({"someone@example.invalid"})) as simulated:
        with pytest.raises(AuthenticationError):
            await _client(simulated).connect()


async def test_a_read_only_client_never_sends_a_setpoint_write() -> None:
    async with _simulated() as simulated:
        client = _client(simulated, read_only=True)
        await client.connect()
        with pytest.raises(ReadOnlyError):
            await client.write("fan_level", 3)
        assert simulated.received(SETPOINT_WRITE) == []


async def test_a_written_setpoint_reaches_the_device() -> None:
    async with _simulated() as simulated:
        client = _client(simulated)
        await client.connect()
        assert await client.write("fan_level", 3) is True
        assert await _arrived(simulated, SETPOINT_WRITE) == [Command(SETPOINT_WRITE, ((0, 30, 3),))]


async def test_a_silent_device_makes_the_client_unavailable_until_it_answers_again() -> None:
    clock = FakeClock()
    async with _simulated(clock=clock) as simulated:
        client = _client(simulated, clock=clock)
        await client.connect()
        client.subscribe("temp_outside", _nothing)
        simulated.silent = True
        clock.advance(60)
        await client.poll()
        stale = client.value("temp_outside")
        assert client.connected is False and stale is not None and stale.quality is Quality.STALE

        simulated.silent = False
        clock.advance(60)
        await client.poll()
        await client.poll()
        good = client.value("temp_outside")
        assert client.connected is True and good is not None and good.quality is Quality.GOOD


async def test_a_restarted_device_goes_unnoticed_by_the_client() -> None:
    clock = FakeClock()
    async with _simulated(clock=clock) as simulated:
        client = _client(simulated, clock=clock)
        await client.connect()
        client.subscribe("temp_outside", _nothing)
        simulated.restart()
        clock.advance(60)
        await client.poll()
        temp = client.value("temp_outside")
        assert client.connected is True and temp is not None and temp.quality is Quality.GOOD
