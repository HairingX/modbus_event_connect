"""Tests for coil support (function codes 0x01 read / 0x05 write single) on the transport layer.

Coils are the fourth Modbus address space; before this, the transport could only reach
input registers, holding registers and discrete inputs.
"""
import asyncio
from typing import List, Sequence

from src.modbus_event_connect.modbus_tcp.transport import ModbusTransport, PymodbusTransport


class _FakeTransport:
    """
    Minimal ModbusTransport stand-in, including the coil methods.

    Annotated to match the protocol exactly, so a signature that drifts apart from
    ModbusTransport is caught by the type checker and not only by the isinstance test below,
    which a runtime_checkable Protocol answers from method names alone.
    """

    @property
    def is_open(self) -> bool: return True
    @property
    def last_exception_code(self) -> int: return 0
    @property
    def last_error_text(self) -> str | None: return None

    async def open(self) -> bool: return True
    async def close(self) -> None: return None

    async def read_input_registers(self, address: int, count: int) -> List[int] | None: return [0] * count
    async def read_holding_registers(self, address: int, count: int) -> List[int] | None: return [0] * count
    async def read_discrete_inputs(self, address: int, count: int) -> List[bool] | None: return [False] * count
    async def read_coils(self, address: int, count: int) -> List[bool] | None: return [True, False, True][:count]
    async def write_coil(self, address: int, value: bool) -> bool: return True
    async def write_register(self, address: int, value: int) -> bool: return True
    async def write_registers(self, address: int, values: Sequence[int]) -> bool: return True


def test_fake_transport_satisfies_the_protocol() -> None:
    # The annotation is the stricter of the two checks: a runtime_checkable Protocol answers
    # isinstance from method names alone, while assignability also compares the signatures.
    transport: ModbusTransport = _FakeTransport()
    assert isinstance(transport, ModbusTransport)


def test_pymodbus_transport_exposes_coil_methods() -> None:
    assert callable(getattr(PymodbusTransport, "read_coils", None))
    assert callable(getattr(PymodbusTransport, "write_coil", None))


def test_fake_read_coils_returns_the_expected_list() -> None:
    transport = _FakeTransport()
    assert asyncio.run(transport.read_coils(0, 3)) == [True, False, True]


def test_fake_write_coil_returns_true() -> None:
    transport = _FakeTransport()
    assert asyncio.run(transport.write_coil(0, True)) is True
