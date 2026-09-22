"""Tests for coil support (function codes 0x01 read / 0x05 write single) on the transport layer.

Coils are the fourth Modbus address space; before this, the transport could only reach
input registers, holding registers and discrete inputs.
"""
import asyncio

from src.modbus_event_connect.modbus_tcp.transport import ModbusTransport, PymodbusTransport


class _FakeTransport:
    """Minimal ModbusTransport stand-in, including the new coil methods."""

    @property
    def is_open(self): return True
    @property
    def last_exception_code(self): return 0
    @property
    def last_error_text(self): return None

    async def open(self): return True
    async def close(self): return None

    async def read_input_registers(self, address, count): return [0] * count
    async def read_holding_registers(self, address, count): return [0] * count
    async def read_discrete_inputs(self, address, count): return [False] * count
    async def read_coils(self, address, count): return [True, False, True][:count]
    async def write_coil(self, address, value): return True
    async def write_register(self, address, value): return True
    async def write_registers(self, address, values): return True


def test_fake_transport_satisfies_the_protocol():
    assert isinstance(_FakeTransport(), ModbusTransport)


def test_pymodbus_transport_exposes_coil_methods():
    assert callable(getattr(PymodbusTransport, "read_coils", None))
    assert callable(getattr(PymodbusTransport, "write_coil", None))


def test_fake_read_coils_returns_the_expected_list():
    transport = _FakeTransport()
    assert asyncio.run(transport.read_coils(0, 3)) == [True, False, True]


def test_fake_write_coil_returns_true():
    transport = _FakeTransport()
    assert asyncio.run(transport.write_coil(0, True)) is True
