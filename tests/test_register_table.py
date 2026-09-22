"""
Tests for RegisterTable: which Modbus address space a point is read from.

Before this, the address space was implied by the point's Python class -
ModbusDatapoint always meant input registers (FC 0x04), ModbusSetpoint always meant
holding registers (FC 0x03). That made discrete inputs (FC 0x02) and coils (FC 0x01)
unreachable, and forced a read-only holding register to be modelled as a setpoint
with no write address.

register_table makes the address space an explicit property of the point instead, and
batch_reads must never merge points from different tables: address 1 in INPUT and
address 1 in HOLDING are unrelated registers.
"""
import asyncio
from enum import auto

import pytest

from src.modbus_event_connect import (
    ModbusDatapoint,
    ModbusDatapointKey,
    ModbusDeviceAdapter,
    ModbusDeviceBase,
    ModbusDeviceInfo,
    ModbusSetpoint,
    ModbusSetpointKey,
    ModbusTCPEventConnect,
    RegisterTable,
    VersionInfo,
    VersionInfoKeys,
)
from src.modbus_event_connect.modbus_tcp.transport import (
    EXCEPTION_ILLEGAL_DATA_ADDRESS,
    EXCEPTION_NONE,
)


class DK(ModbusDatapointKey):
    A = auto()
    B = auto()


class SK(ModbusSetpointKey):
    A = auto()
    B = auto()
    BAD = auto()


class _Device(ModbusDeviceBase):
    """Minimal model: no points of its own. The tests build points ad hoc and pass
    them straight to the read/batch methods, exactly as tests/test_regressions.py does."""

    def __init__(self, device_info: ModbusDeviceInfo):
        super().__init__(device_info)
        self._attr_manufacturer = "TEST"
        self._attr_model_name = "TEST"
        self._attr_version_keys = VersionInfoKeys()
        self._attr_datapoints = []
        self._attr_setpoints = []


class _BadDevice(ModbusDeviceBase):
    """A model whose setpoint contradicts itself: a write_address in a read-only table."""

    def __init__(self, device_info: ModbusDeviceInfo):
        super().__init__(device_info)
        self._attr_manufacturer = "TEST"
        self._attr_model_name = "TEST"
        self._attr_version_keys = VersionInfoKeys()
        self._attr_datapoints = []
        self._attr_setpoints = [
            ModbusSetpoint(key=SK.BAD, write_address=10, register_table=RegisterTable.INPUT),
        ]


class _Adapter(ModbusDeviceAdapter):
    def _translate_to_model(self, device_info: ModbusDeviceInfo):
        return _Device


class _Client(ModbusTCPEventConnect):
    def __init__(self, transport=None):
        super().__init__(transport=transport)
        self._attr_adapter = _Adapter()


def _info(device_id: str = "test") -> ModbusDeviceInfo:
    return ModbusDeviceInfo(device_id=device_id, device_host="h", device_port=502,
                            version=VersionInfo(), identification=None)


def _connected_client() -> _Client:
    client = _Client()
    client._attr_adapter.load_device_model(_info())
    return client


class _RecordingTransport:
    """
    Minimal ModbusTransport stand-in that records which reader was called and with which
    address space, and can fail on demand. Same pattern as tests/test_regressions.py.
    """

    def __init__(self, *, fail=True, exception=EXCEPTION_ILLEGAL_DATA_ADDRESS):
        self.calls = []
        self.fail = fail
        self._exception = exception if fail else EXCEPTION_NONE

    @property
    def is_open(self): return True
    @property
    def last_exception_code(self): return self._exception
    @property
    def last_error_text(self): return None if not self.fail else "recorded failure"

    async def open(self): return True
    async def close(self): return None

    async def _read(self, kind, address, count, bits=False):
        self.calls.append((kind, address, count))
        if self.fail:
            return None
        return [False] * count if bits else [0] * count

    async def read_input_registers(self, address, count):
        return await self._read("input", address, count)
    async def read_holding_registers(self, address, count):
        return await self._read("holding", address, count)
    async def read_discrete_inputs(self, address, count):
        return await self._read("discrete", address, count, bits=True)
    async def read_coils(self, address, count):
        return await self._read("coils", address, count, bits=True)
    async def write_coil(self, address, value):
        self.calls.append(("write_coil", address, value)); return not self.fail
    async def write_register(self, address, value):
        self.calls.append(("write_register", address, value)); return not self.fail
    async def write_registers(self, address, values):
        self.calls.append(("write_registers", address, len(values))); return not self.fail


# ----------------------------------------------------------- reader chosen per register_table

def test_input_table_point_is_read_with_read_input_registers():
    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    point = ModbusDatapoint(key=DK.A, read_address=1, register_table=RegisterTable.INPUT)
    asyncio.run(client._request_datapoint_read([point]))
    assert {c[0] for c in client._transport.calls} == {"input"}


def test_holding_table_point_is_read_with_read_holding_registers():
    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    point = ModbusDatapoint(key=DK.A, read_address=1, register_table=RegisterTable.HOLDING)
    asyncio.run(client._request_datapoint_read([point]))
    assert {c[0] for c in client._transport.calls} == {"holding"}


def test_discrete_table_point_is_read_with_read_discrete_inputs():
    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    point = ModbusDatapoint(key=DK.A, read_address=1, register_table=RegisterTable.DISCRETE)
    asyncio.run(client._request_datapoint_read([point]))
    assert {c[0] for c in client._transport.calls} == {"discrete"}


def test_coil_table_point_is_read_with_read_coils():
    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    point = ModbusDatapoint(key=DK.A, read_address=1, register_table=RegisterTable.COIL)
    asyncio.run(client._request_datapoint_read([point]))
    assert {c[0] for c in client._transport.calls} == {"coils"}


# --------------------------------------------------- batching must never merge across tables

def test_contiguous_addresses_in_different_tables_are_not_batched():
    client = _connected_client()
    in_input = ModbusDatapoint(key=DK.A, read_address=10, register_table=RegisterTable.INPUT)
    in_holding = ModbusDatapoint(key=DK.B, read_address=11, register_table=RegisterTable.HOLDING)
    batches = list(client.batch_reads([in_input, in_holding]))
    assert len(batches) == 2, "address 1 in INPUT and address 1 in HOLDING are unrelated registers"


def test_contiguous_addresses_in_the_same_table_are_still_batched():
    client = _connected_client()
    first = ModbusDatapoint(key=DK.A, read_address=10, register_table=RegisterTable.HOLDING)
    second = ModbusDatapoint(key=DK.B, read_address=11, register_table=RegisterTable.HOLDING)
    batches = list(client.batch_reads([first, second]))
    assert len(batches) == 1


def test_batching_across_all_four_tables_never_merges_any_pair():
    client = _connected_client()
    points = [
        ModbusDatapoint(key=DK.A, read_address=5, register_table=RegisterTable.INPUT),
        ModbusDatapoint(key=DK.A, read_address=5, register_table=RegisterTable.HOLDING),
        ModbusDatapoint(key=DK.A, read_address=5, register_table=RegisterTable.DISCRETE),
        ModbusDatapoint(key=DK.A, read_address=5, register_table=RegisterTable.COIL),
    ]
    batches = list(client.batch_reads(points))
    assert len(batches) == 4
    tables = {batch[0].register_table for batch in batches}
    assert tables == {RegisterTable.INPUT, RegisterTable.HOLDING,
                       RegisterTable.DISCRETE, RegisterTable.COIL}


# ------------------------------------------------- per-point fallback keeps the right table

def test_batch_fallback_after_rejection_stays_on_the_same_table():
    client = _connected_client()
    client._transport = _RecordingTransport(fail=True, exception=EXCEPTION_ILLEGAL_DATA_ADDRESS)
    first = ModbusDatapoint(key=DK.A, read_address=10, register_table=RegisterTable.DISCRETE)
    second = ModbusDatapoint(key=DK.B, read_address=11, register_table=RegisterTable.DISCRETE)
    asyncio.run(client._request_datapoint_read([first, second]))
    # one combined batch read, then one fallback read per point - all against DISCRETE
    assert all(c[0] == "discrete" for c in client._transport.calls)
    assert len(client._transport.calls) == 3


# --------------------------------------------------------------------- instantiate() validation

def test_setpoint_with_write_address_in_input_raises_at_instantiate():
    device = _BadDevice(_info())
    with pytest.raises(ValueError):
        device.instantiate()


# ---------------------------------------------------------------------------------- defaults

def test_unannotated_datapoint_defaults_to_input_and_reads_input_registers():
    point = ModbusDatapoint(key=DK.A, read_address=1)
    assert point.register_table == RegisterTable.INPUT

    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    asyncio.run(client._request_datapoint_read([point]))
    assert {c[0] for c in client._transport.calls} == {"input"}


def test_unannotated_setpoint_defaults_to_holding_and_reads_holding_registers():
    point = ModbusSetpoint(key=SK.A, read_address=1)
    assert point.register_table == RegisterTable.HOLDING

    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    asyncio.run(client._request_setpoint_read([point]))
    assert {c[0] for c in client._transport.calls} == {"holding"}
