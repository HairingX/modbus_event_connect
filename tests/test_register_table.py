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

That every table is actually read and written with its own function code, for every width
and point kind, is tests/test_read_write_matrix.py.
"""
import asyncio
from enum import auto
from typing import Callable

from doubles import RecordingTransport, device_info
from src.modbus_event_connect import (
    ModbusDatapoint,
    ModbusDatapointKey,
    ModbusDevice,
    ModbusDeviceAdapter,
    ModbusDeviceBase,
    ModbusDeviceInfo,
    ModbusSetpoint,
    ModbusSetpointKey,
    ModbusTCPEventConnect,
    RegisterTable,
    VersionInfoKeys,
)
from src.modbus_event_connect.modbus_tcp.transport import ModbusTransport


class DK(ModbusDatapointKey):
    A = auto()
    B = auto()


class SK(ModbusSetpointKey):
    A = auto()
    B = auto()


class _Device(ModbusDeviceBase):
    """Minimal model: no points of its own. The tests build points ad hoc and pass
    them straight to the read/batch methods, exactly as tests/test_regressions.py does."""

    def __init__(self, info: ModbusDeviceInfo) -> None:
        super().__init__(info)
        self._attr_manufacturer = "TEST"
        self._attr_model_name = "TEST"
        self._attr_version_keys = VersionInfoKeys()
        self._attr_datapoints = []
        self._attr_setpoints = []


class _Adapter(ModbusDeviceAdapter):
    def _translate_to_model(self, device_info: ModbusDeviceInfo) -> Callable[[ModbusDeviceInfo], ModbusDevice] | None:
        return _Device


class _Client(ModbusTCPEventConnect):
    def __init__(self, transport: ModbusTransport | None = None) -> None:
        super().__init__(transport=transport)
        self._attr_adapter = _Adapter()


def _connected_client(transport: RecordingTransport | None = None) -> _Client:
    """A client with its model loaded, and optionally a transport already recording."""
    client = _Client()
    client._attr_adapter.load_device_model(device_info())
    if transport is not None:
        client._transport = transport
    return client


# --------------------------------------------------- batching must never merge across tables

def test_contiguous_addresses_in_different_tables_are_not_batched() -> None:
    client = _connected_client()
    in_input = ModbusDatapoint(key=DK.A, read_address=10, register_table=RegisterTable.INPUT)
    in_holding = ModbusDatapoint(key=DK.B, read_address=11, register_table=RegisterTable.HOLDING)
    batches = list(client.batch_reads([in_input, in_holding]))
    assert len(batches) == 2, "address 1 in INPUT and address 1 in HOLDING are unrelated registers"


def test_contiguous_addresses_in_the_same_table_are_still_batched() -> None:
    client = _connected_client()
    first = ModbusDatapoint(key=DK.A, read_address=10, register_table=RegisterTable.HOLDING)
    second = ModbusDatapoint(key=DK.B, read_address=11, register_table=RegisterTable.HOLDING)
    batches = list(client.batch_reads([first, second]))
    assert len(batches) == 1


def test_batching_across_all_four_tables_never_merges_any_pair() -> None:
    client = _connected_client()
    points = [
        ModbusDatapoint(key=DK.A, read_address=5, register_table=table)
        for table in RegisterTable
    ]
    batches = list(client.batch_reads(points))
    assert len(batches) == 4
    assert {batch[0].register_table for batch in batches} == set(RegisterTable)


# ---------------------------------------------------------------------------------- defaults

def test_unannotated_datapoint_defaults_to_input_and_reads_input_registers() -> None:
    point = ModbusDatapoint(key=DK.A, read_address=1)
    assert point.register_table == RegisterTable.INPUT

    transport = RecordingTransport(fail=False)
    client = _connected_client(transport)
    asyncio.run(client._request_datapoint_read([point]))
    assert transport.kinds == {"input"}


def test_unannotated_setpoint_defaults_to_holding_and_reads_holding_registers() -> None:
    point = ModbusSetpoint(key=SK.A, read_address=1)
    assert point.register_table == RegisterTable.HOLDING

    transport = RecordingTransport(fail=False)
    client = _connected_client(transport)
    asyncio.run(client._request_setpoint_read([point]))
    assert transport.kinds == {"holding"}
