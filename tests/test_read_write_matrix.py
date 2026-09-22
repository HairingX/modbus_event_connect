"""
Every read and write path, for every register table and width, against a simulated device.

Two kinds of test live here:

- The matrices pin what works, so no combination of table, width and point kind can quietly
  break. Each case checks both halves: the function code that reached the wire, and the value
  that came back or landed in the device.
- The tests marked `xfail(strict=True)` pin what does not work yet. Each fails today for the
  reason it states. Strict means that the day one starts passing, the suite goes red and the
  marker has to be removed together with the fix - so a known defect stays visible in every
  run (`pytest -rx`) without turning the suite red for everyone else.

The failure tests are characterisation: they record what the client does today when a device
refuses, so a change to that behaviour is a decision someone made, not an accident.
"""
import asyncio
from enum import auto
from typing import Callable, Dict, List

import pytest

from doubles import NO_ANSWER, READ_FUNCTION_CODE, SimulatedDevice, device_info
from src.modbus_event_connect import (
    MODBUS_VALUE_TYPES,
    ModbusDatapoint,
    ModbusDatapointKey,
    ModbusDevice,
    ModbusDeviceAdapter,
    ModbusDeviceBase,
    ModbusDeviceInfo,
    ModbusParser,
    ModbusPointKey,
    ModbusSetpoint,
    ModbusSetpointKey,
    ModbusTCPEventConnect,
    ModbusValueType,
    RegisterTable,
    VersionInfoKeys,
)
from src.modbus_event_connect.modbus_tcp.transport import (
    EXCEPTION_ILLEGAL_DATA_ADDRESS,
    EXCEPTION_ILLEGAL_FUNCTION,
    EXCEPTION_SLAVE_DEVICE_FAILURE,
    ModbusTransport,
)

INPUT, HOLDING, DISCRETE, COIL = (RegisterTable.INPUT, RegisterTable.HOLDING,
                                  RegisterTable.DISCRETE, RegisterTable.COIL)
Value = MODBUS_VALUE_TYPES | None


class DK(ModbusDatapointKey):
    INPUT_W1 = auto()
    INPUT_W2 = auto()
    HOLDING_W1 = auto()
    HOLDING_W2 = auto()
    DISCRETE_W1 = auto()
    COIL_W1 = auto()


class SK(ModbusSetpointKey):
    INPUT_W1 = auto()
    INPUT_W2 = auto()
    HOLDING_W1 = auto()
    HOLDING_W2 = auto()
    DISCRETE_W1 = auto()
    COIL_W1 = auto()
    WRITE_HOLDING_W1 = auto()
    WRITE_HOLDING_W2 = auto()
    WRITE_COIL_W1 = auto()
    COMMAND = auto()


# INPUT and HOLDING deliberately reuse the same addresses with different contents: reading
# the right value back is what proves the tables are separate address spaces.
IMAGE: Dict[RegisterTable, Dict[int, int]] = {
    INPUT:    {100: 1111, 110: 0x0001, 111: 0x0002,
               200: 2222, 210: 0x0003, 211: 0x0004},
    HOLDING:  {100: 3333, 110: 0x0005, 111: 0x0006,
               200: 4444, 210: 0x0007, 211: 0x0008,
               300: 0, 310: 0, 311: 0, 330: 0},
    DISCRETE: {5: 1, 15: 1},
    COIL:     {7: 1, 17: 1, 320: 0},
}


class _Device(ModbusDeviceBase):
    """One point for every readable table and width, as both a datapoint and a setpoint."""

    def __init__(self, info: ModbusDeviceInfo) -> None:
        super().__init__(info)
        self._attr_manufacturer = "TEST"
        self._attr_model_name = "TEST"
        self._attr_version_keys = VersionInfoKeys()
        self._attr_datapoints = [
            ModbusDatapoint(key=DK.INPUT_W1, read_address=100, register_table=INPUT),
            ModbusDatapoint(key=DK.INPUT_W2, read_address=110, read_length=2, register_table=INPUT),
            ModbusDatapoint(key=DK.HOLDING_W1, read_address=100, register_table=HOLDING),
            ModbusDatapoint(key=DK.HOLDING_W2, read_address=110, read_length=2, register_table=HOLDING),
            ModbusDatapoint(key=DK.DISCRETE_W1, read_address=5, register_table=DISCRETE),
            ModbusDatapoint(key=DK.COIL_W1, read_address=7, register_table=COIL),
        ]
        self._attr_setpoints = [
            # read-only setpoints, one per table and width
            ModbusSetpoint(key=SK.INPUT_W1, read_address=200, register_table=INPUT),
            ModbusSetpoint(key=SK.INPUT_W2, read_address=210, read_length=2, register_table=INPUT),
            ModbusSetpoint(key=SK.HOLDING_W1, read_address=200, register_table=HOLDING),
            ModbusSetpoint(key=SK.HOLDING_W2, read_address=210, read_length=2, register_table=HOLDING),
            ModbusSetpoint(key=SK.DISCRETE_W1, read_address=15, register_table=DISCRETE),
            ModbusSetpoint(key=SK.COIL_W1, read_address=17, register_table=COIL),
            # writable setpoints, one per writable table and width
            ModbusSetpoint(key=SK.WRITE_HOLDING_W1, write_address=300, register_table=HOLDING),
            ModbusSetpoint(key=SK.WRITE_HOLDING_W2, write_address=310, write_length=2,
                           read_length=2, register_table=HOLDING),
            ModbusSetpoint(key=SK.WRITE_COIL_W1, write_address=320, register_table=COIL),
            # write-only, no read-back: the shape a momentary command has today
            ModbusSetpoint(key=SK.COMMAND, write_address=330, register_table=HOLDING),
        ]


class _Adapter(ModbusDeviceAdapter):
    def _translate_to_model(self, device_info: ModbusDeviceInfo) -> Callable[[ModbusDeviceInfo], ModbusDevice] | None:
        return _Device


class _Client(ModbusTCPEventConnect):
    def __init__(self, transport: ModbusTransport | None = None) -> None:
        super().__init__(transport=transport)
        self._attr_adapter = _Adapter()
        # Retries are part of what is under test, waiting for them is not.
        self.BUSY_RETRY_INITIAL_DELAY = 0.001


def _connected_client(device: SimulatedDevice) -> _Client:
    client = _Client(transport=device)
    client._attr_adapter.load_device_model(device_info())
    client._sync_read_flags()
    return client


def _read(client: _Client, *points: ModbusDatapoint | ModbusSetpoint) -> Dict[ModbusPointKey, Value]:
    """Read points through the client's own read path, keyed by point key."""
    datapoints = [p for p in points if isinstance(p, ModbusDatapoint)]
    setpoints = [p for p in points if isinstance(p, ModbusSetpoint)]
    result: Dict[ModbusPointKey, Value] = {}
    if datapoints:
        result.update({p.key: v for p, v in asyncio.run(client._request_datapoint_read(datapoints))})
    if setpoints:
        result.update({p.key: v for p, v in asyncio.run(client._request_setpoint_read(setpoints))})
    return result


def _model_point(client: _Client, key: ModbusPointKey) -> ModbusDatapoint | ModbusSetpoint:
    point = (client._attr_adapter.get_datapoint(key) if isinstance(key, ModbusDatapointKey)
             else client._attr_adapter.get_setpoint(key) if isinstance(key, ModbusSetpointKey)
             else None)
    assert point is not None, f"test model has no point {key}"
    return point


def _instantiate(datapoints: List[ModbusDatapoint], setpoints: List[ModbusSetpoint]) -> None:
    """Build and validate a model holding exactly these points."""
    class _AdHoc(ModbusDeviceBase):
        def __init__(self, info: ModbusDeviceInfo) -> None:
            super().__init__(info)
            self._attr_manufacturer = "TEST"
            self._attr_model_name = "TEST"
            self._attr_version_keys = VersionInfoKeys()
            self._attr_datapoints = datapoints
            self._attr_setpoints = setpoints
    _AdHoc(device_info()).instantiate()


# =============================================================================== read matrix

READS = [
    # key,              table,    value in the image
    (DK.INPUT_W1,       INPUT,    1111),
    (DK.INPUT_W2,       INPUT,    0x0001_0002),
    (DK.HOLDING_W1,     HOLDING,  3333),
    (DK.HOLDING_W2,     HOLDING,  0x0005_0006),
    (DK.DISCRETE_W1,    DISCRETE, 1),
    (DK.COIL_W1,        COIL,     1),
    (SK.INPUT_W1,       INPUT,    2222),
    (SK.INPUT_W2,       INPUT,    0x0003_0004),
    (SK.HOLDING_W1,     HOLDING,  4444),
    (SK.HOLDING_W2,     HOLDING,  0x0007_0008),
    (SK.DISCRETE_W1,    DISCRETE, 1),
    (SK.COIL_W1,        COIL,     1),
]


@pytest.mark.parametrize("key,table,expected", READS, ids=[str(r[0]) for r in READS])
def test_every_table_width_and_point_kind_is_read_correctly(
        key: ModbusPointKey, table: RegisterTable, expected: int) -> None:
    device = SimulatedDevice(IMAGE)
    client = _connected_client(device)

    values = _read(client, _model_point(client, key))

    assert device.function_codes == {READ_FUNCTION_CODE[table]}, "read with the wrong function code"
    assert values[key] == expected, "wrong value - the wrong table, width or decoding"


# ============================================================================== write matrix

WRITES = [
    # key,                  value,        function code, where it must land
    (SK.WRITE_HOLDING_W1,   42,           0x06, {(HOLDING, 300): 42}),
    (SK.WRITE_HOLDING_W2,   0x0009_0010,  0x10, {(HOLDING, 310): 0x0009, (HOLDING, 311): 0x0010}),
    (SK.WRITE_COIL_W1,      1,            0x05, {(COIL, 320): 1}),
]


@pytest.mark.parametrize("key,value,function_code,lands", WRITES, ids=[str(w[0]) for w in WRITES])
def test_every_writable_table_and_width_is_written_correctly(
        key: ModbusSetpointKey, value: int, function_code: int,
        lands: Dict[tuple[RegisterTable, int], int]) -> None:
    device = SimulatedDevice(IMAGE)
    client = _connected_client(device)

    assert asyncio.run(client.request_setpoint_write(key, value)) is True, "the write was refused"

    writes = {call[0] for call in device.calls if call[0] in (0x05, 0x06, 0x0F, 0x10)}
    assert writes == {function_code}, "written with the wrong function code"
    for (table, address), expected in lands.items():
        assert device.image[table][address] == expected, f"{table} {address} does not hold the value"


def test_a_coil_write_never_touches_a_holding_register() -> None:
    """
    Coils and holding registers are separate address spaces, like every other pair of tables.

    Coil 320 and holding register 320 are two different things on a real device - often a
    relay and an unrelated setting. A coil write sent as FC 0x06 does not fail: it succeeds,
    against the wrong register.
    """
    image = {table: dict(values) for table, values in IMAGE.items()}
    image[HOLDING][320] = 777                       # an unrelated setting at the same address
    device = SimulatedDevice(image)
    client = _connected_client(device)

    asyncio.run(client.request_setpoint_write(SK.WRITE_COIL_W1, 1))

    assert device.image[HOLDING][320] == 777, "the coil write overwrote a holding register"
    assert not {0x06, 0x10} & device.function_codes, "a coil was written with a register function code"


@pytest.mark.parametrize("table", [INPUT, DISCRETE])
def test_a_write_address_in_a_read_only_table_is_rejected(table: RegisterTable) -> None:
    with pytest.raises(ValueError):
        _instantiate([], [ModbusSetpoint(key=SK.WRITE_HOLDING_W1, write_address=1, register_table=table)])


# ========================================================================= read failure matrix
#
# Two contiguous points in one table, so they are read as one batch. The fault is planted on
# the second address only; the question is what that costs the first.

TABLES = [INPUT, HOLDING, DISCRETE, COIL]


def _two_points(table: RegisterTable) -> tuple[ModbusDatapoint, ModbusDatapoint]:
    return (ModbusDatapoint(key=DK.INPUT_W1, read_address=40, register_table=table),
            ModbusDatapoint(key=DK.INPUT_W2, read_address=41, register_table=table))


def _device_with_fault(table: RegisterTable, fault: int | None = None, *, busy_for: int = 0) -> SimulatedDevice:
    faults = {} if fault is None else {(table, 41): fault}
    return SimulatedDevice({table: {40: 1, 41: 1}}, faults=faults, busy_for=busy_for)


@pytest.mark.parametrize("table", TABLES)
def test_an_absent_register_costs_only_itself(table: RegisterTable) -> None:
    """0x02: the batch is refused whole, then read point by point; only the absent one is lost."""
    device = _device_with_fault(table, EXCEPTION_ILLEGAL_DATA_ADDRESS)
    client = _connected_client(device)
    good, bad = _two_points(table)

    values = _read(client, good, bad)

    assert values == {good.key: 1, bad.key: None}
    assert client.is_available(good.key) and not client.is_available(bad.key)
    assert device.function_codes == {READ_FUNCTION_CODE[table]}, "the fallback left the table"
    assert len(device.calls) == 3, "one batch, then one read per point"


@pytest.mark.parametrize("table", TABLES)
def test_an_offline_peripheral_clears_the_whole_batch_but_keeps_it_polled(table: RegisterTable) -> None:
    """0x04: the registers exist, something behind them is not answering. It will come back."""
    device = _device_with_fault(table, EXCEPTION_SLAVE_DEVICE_FAILURE)
    client = _connected_client(device)
    good, bad = _two_points(table)

    values = _read(client, good, bad)

    assert values == {good.key: None, bad.key: None}
    assert client.is_available(good.key) and client.is_available(bad.key)


@pytest.mark.parametrize("table", TABLES)
def test_no_answer_leaves_the_batch_out_of_the_result(table: RegisterTable) -> None:
    """A timeout reports nothing for the batch, so the last known values stay as they were."""
    device = _device_with_fault(table, NO_ANSWER)
    client = _connected_client(device)
    good, bad = _two_points(table)

    assert _read(client, good, bad) == {}
    assert client.is_available(good.key) and client.is_available(bad.key)


@pytest.mark.parametrize("table", TABLES)
def test_an_unsupported_function_leaves_the_batch_out_of_the_result(table: RegisterTable) -> None:
    """0x01: the device does not implement this table at all. Nothing is recorded as absent."""
    device = _device_with_fault(table, EXCEPTION_ILLEGAL_FUNCTION)
    client = _connected_client(device)
    good, bad = _two_points(table)

    assert _read(client, good, bad) == {}
    assert client.is_available(good.key) and client.is_available(bad.key)


@pytest.mark.parametrize("table", TABLES)
def test_a_briefly_busy_device_is_retried_until_it_answers(table: RegisterTable) -> None:
    device = _device_with_fault(table, busy_for=2)
    client = _connected_client(device)
    good, bad = _two_points(table)

    assert _read(client, good, bad) == {good.key: 1, bad.key: 1}
    assert len(device.calls) == 3, "two busy answers, then the one that succeeded"


@pytest.mark.parametrize("table", TABLES)
def test_a_device_that_stays_busy_is_given_up_on_without_recording_absence(table: RegisterTable) -> None:
    device = _device_with_fault(table, busy_for=1000)
    client = _connected_client(device)
    good, bad = _two_points(table)

    assert _read(client, good, bad) == {}
    assert client.is_available(good.key) and client.is_available(bad.key)
    assert len(device.calls) == client.BUSY_RETRY_MAX_ATTEMPTS


# ======================================================================== write failure matrix

WRITE_OUTCOMES = [
    # fault on the target,              busy_for, accepted, lands
    (None,                              0,        True,     True),
    (EXCEPTION_ILLEGAL_DATA_ADDRESS,    0,        False,    False),
    (EXCEPTION_SLAVE_DEVICE_FAILURE,    0,        False,    False),
    (NO_ANSWER,                         0,        False,    False),
    (None,                              2,        True,     True),
]


@pytest.mark.parametrize("fault,busy_for,accepted,lands", WRITE_OUTCOMES,
                         ids=["ok", "absent", "offline", "no-answer", "busy-then-ok"])
@pytest.mark.parametrize("key,table,address", [
    (SK.WRITE_HOLDING_W1, HOLDING, 300),
    (SK.WRITE_COIL_W1, COIL, 320),
], ids=["holding", "coil"])
def test_a_write_reports_exactly_whether_the_device_took_it(
        key: ModbusSetpointKey, table: RegisterTable, address: int,
        fault: int | None, busy_for: int, accepted: bool, lands: bool) -> None:
    faults = {} if fault is None else {(table, address): fault}
    device = SimulatedDevice(IMAGE, faults=faults, busy_for=busy_for)
    client = _connected_client(device)

    assert asyncio.run(client.request_setpoint_write(key, 1)) is accepted
    assert (device.image[table][address] == 1) is lands
    assert client.write_pending is False, "a finished write left the UI disabled"


# ============================================================================ known defects

@pytest.mark.xfail(strict=True, reason="a point one bit wide is the only width a bit table has, "
                                       "but instantiate() accepts any read_length there and the "
                                       "decoder then packs bits as if they were 16-bit words")
@pytest.mark.parametrize("table", [DISCRETE, COIL])
def test_a_bit_table_point_wider_than_one_bit_is_rejected(table: RegisterTable) -> None:
    with pytest.raises(ValueError):
        _instantiate([ModbusDatapoint(key=DK.DISCRETE_W1, read_address=1, read_length=2,
                                      register_table=table)], [])


def test_points_sharing_an_address_are_each_decoded_correctly() -> None:
    """Two views of one register - raw and scaled here, two bits of one status word later."""
    device = SimulatedDevice({INPUT: {50: 7}})
    client = _connected_client(device)
    raw = ModbusDatapoint(key=DK.INPUT_W1, read_address=50)
    scaled = ModbusDatapoint(key=DK.INPUT_W2, read_address=50, divider=10)

    assert _read(client, raw, scaled) == {raw.key: 7, scaled.key: 0.7}


@pytest.mark.xfail(strict=True, reason="batch_reads needs strictly increasing addresses, so a "
                                       "shared address starts a new batch. With 16 bit flags "
                                       "in one status word that is 16 requests instead of one")
def test_points_sharing_an_address_are_read_with_one_request() -> None:
    device = SimulatedDevice({INPUT: {50: 7}})
    client = _connected_client(device)
    raw = ModbusDatapoint(key=DK.INPUT_W1, read_address=50)
    scaled = ModbusDatapoint(key=DK.INPUT_W2, read_address=50, divider=10)

    _read(client, raw, scaled)

    assert len(device.calls) == 1


_BINARY32_21_0 = [0x41A8, 0x0000]
"""21.0 as an IEEE 754 binary32, high word first - how most energy meters and drives send it."""


def _value_types() -> List[str]:
    return [v for k, v in vars(ModbusValueType).items() if k.isupper() and isinstance(v, str)]


@pytest.mark.xfail(strict=True, reason="ModbusValueType.FLOAT means a scaled integer, not IEEE "
                                       "754. No value type reads a binary32")
def test_an_ieee754_float_can_be_decoded() -> None:
    decoded: Dict[str, Value] = {}
    for value_type in _value_types():
        point = ModbusDatapoint(key=DK.INPUT_W2, read_address=0, read_length=2, value_type=value_type)
        try:
            decoded[value_type] = ModbusParser.values_to_value(list(_BINARY32_21_0), point)
        except Exception:
            decoded[value_type] = None
    assert 21.0 in decoded.values(), f"no value type reads 0x41A80000 as 21.0: {decoded}"


@pytest.mark.xfail(strict=True, reason="no value type encodes IEEE 754, so a binary32 setpoint "
                                       "cannot be written")
def test_an_ieee754_float_can_be_encoded() -> None:
    encoded: Dict[str, List[int] | None] = {}
    for value_type in _value_types():
        point = ModbusSetpoint(key=SK.WRITE_HOLDING_W2, write_address=0, write_length=2,
                               read_length=2, value_type=value_type)
        try:
            encoded[value_type] = ModbusParser.value_to_values(21.0, point, validate=False)
        except Exception:
            encoded[value_type] = None
    assert _BINARY32_21_0 in encoded.values(), f"no value type writes 21.0 as 0x41A80000: {encoded}"


@pytest.mark.xfail(strict=True, reason="writes to one key are coalesced to the newest value, which "
                                       "is right for a state and wrong for an action. There is no "
                                       "way yet to declare a point a command")
def test_every_press_of_a_command_reaches_the_device() -> None:
    """
    "Step up" pressed three times while the first press is still on its way.

    For a setpoint, sending only the newest value is correct: 21.0 then 21.5 then 22.0 should
    end at 22.0 without walking through the others. For a command the presses are the point.
    """
    device = SimulatedDevice(IMAGE, delay=0.01)
    client = _connected_client(device)

    async def three_presses() -> None:
        await asyncio.gather(*(client.request_setpoint_write(SK.COMMAND, 1) for _ in range(3)))

    asyncio.run(three_presses())

    presses = [call for call in device.calls if call[:2] == (0x06, 330)]
    assert len(presses) == 3, f"{len(presses)} of 3 presses reached the device"
