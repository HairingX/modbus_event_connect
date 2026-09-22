"""Pluggable Modbus TCP transports.

The event layer talks to a device through a `ModbusTransport`, never directly to a Modbus
library. Two reasons, both of which are Home Assistant integration quality-scale rules:

* ``async-dependency`` - the library should be fully asyncio, because switching between the
  event loop and worker threads costs real time on every request. `PymodbusTransport` is
  natively async and does no thread hopping.
* ``inject-websession`` - a library should accept a connection rather than opening its own, so
  the host can manage them centrally. Passing a transport into `ModbusTCPEventConnect` lets an
  integration share one socket instead of opening a second one to the same controller.

Writing another transport is small: implement the `ModbusTransport` protocol below. The event
layer neither knows nor cares which Modbus library is underneath.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Protocol, Sequence, cast, runtime_checkable

_LOGGER = logging.getLogger(__name__)

EXCEPTION_NONE = 0x00
EXCEPTION_ILLEGAL_FUNCTION = 0x01
EXCEPTION_ILLEGAL_DATA_ADDRESS = 0x02
EXCEPTION_ILLEGAL_DATA_VALUE = 0x03
EXCEPTION_SLAVE_DEVICE_FAILURE = 0x04
EXCEPTION_ACKNOWLEDGE = 0x05
EXCEPTION_SLAVE_DEVICE_BUSY = 0x06


@runtime_checkable
class ModbusTransport(Protocol):
    """
    What the event layer needs from a Modbus connection.

    Every read returns `None` on failure rather than raising, and records why in
    `last_exception_code` / `last_error_text`, so the caller can tell "this register does not
    exist" (0x02) from "the device is busy, try again" (0x06).
    """

    @property
    def is_open(self) -> bool: ...

    @property
    def last_exception_code(self) -> int:
        """Modbus exception code from the most recent call; EXCEPTION_NONE if there was none."""
        ...

    @property
    def last_error_text(self) -> str | None: ...

    async def open(self) -> bool: ...

    async def close(self) -> None: ...

    async def read_input_registers(self, address: int, count: int) -> List[int] | None: ...

    async def read_holding_registers(self, address: int, count: int) -> List[int] | None: ...

    async def read_discrete_inputs(self, address: int, count: int) -> List[bool] | None: ...

    async def write_register(self, address: int, value: int) -> bool: ...

    async def write_registers(self, address: int, values: Sequence[int]) -> bool: ...


class PymodbusTransport:
    """
    Natively asynchronous transport built on pymodbus.

    pymodbus serialises requests internally (its TransactionManager holds a lock), so this
    transport adds no locking of its own and never leaves the event loop.
    """

    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = 10.0,
                 client: Any = None) -> None:
        """
        Args:
            client: an existing pymodbus async client to use instead of creating one.

        No client is constructed here. `AsyncModbusTcpClient.__init__` calls
        `asyncio.get_running_loop()`, so it can only be built from inside a running loop -
        and a constructor should not be doing connection setup anyway. The client is created
        on the first `open()`.
        """
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._timeout = timeout
        self._last_exception_code: int = EXCEPTION_NONE
        self._last_error_text: str | None = None
        self._client: Any = client

    def _ensure_client(self) -> Any:
        if self._client is None:
            from pymodbus.client import AsyncModbusTcpClient
            self._client = AsyncModbusTcpClient(self._host, port=self._port, timeout=self._timeout)
        return self._client

    @property
    def host(self) -> str: return self._host
    @property
    def port(self) -> int: return self._port
    @property
    def unit_id(self) -> int: return self._unit_id
    @property
    def is_open(self) -> bool: return bool(getattr(self._client, "connected", False))
    @property
    def client(self) -> Any:
        """The underlying pymodbus client, or None before the first open()."""
        return self._client
    @property
    def last_exception_code(self) -> int: return self._last_exception_code
    @property
    def last_error_text(self) -> str | None: return self._last_error_text

    async def open(self) -> bool:
        self._reset_error()
        try:
            return bool(await self._ensure_client().connect())
        except Exception as err:
            self._last_error_text = f"connect failed: {err}"
            return False

    async def close(self) -> None:
        if self._client is None: return
        close = self._client.close()
        if asyncio.iscoroutine(close):
            await close

    def _reset_error(self) -> None:
        self._last_exception_code = EXCEPTION_NONE
        self._last_error_text = None

    def _device_kwargs(self) -> dict[str, int]:
        # pymodbus renamed this keyword across 3.x (slave -> device_id). Pick whichever the
        # installed version accepts, so the library works with the version Home Assistant pins
        # as well as with newer ones.
        import inspect
        params = inspect.signature(self._ensure_client().read_input_registers).parameters
        if "device_id" in params: return {"device_id": self._unit_id}
        if "slave" in params: return {"slave": self._unit_id}
        return {}

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        self._reset_error()
        method: Any = getattr(self._ensure_client(), method_name)
        response: Any
        try:
            # pymodbus is only partially typed, so the result is cast rather than left as
            # Unknown; that keeps this file clean under a strict type checker.
            response = method(*args, **{**kwargs, **self._device_kwargs()})
            if asyncio.iscoroutine(response) or isinstance(response, asyncio.Future):
                response = cast(Any, await response)
        except Exception as err:
            self._last_error_text = f"{method_name} failed: {err}"
            return None
        if response is None:
            self._last_error_text = f"{method_name} returned no response"
            return None
        if bool(getattr(response, "isError", lambda: False)()):
            self._last_exception_code = int(getattr(response, "exception_code", EXCEPTION_NONE))
            self._last_error_text = str(response)
            return None
        return response

    async def read_input_registers(self, address: int, count: int) -> List[int] | None:
        response = await self._call("read_input_registers", address, count=count)
        if response is None: return None
        registers: List[int] = [int(r) for r in response.registers]
        return registers

    async def read_holding_registers(self, address: int, count: int) -> List[int] | None:
        response = await self._call("read_holding_registers", address, count=count)
        if response is None: return None
        registers: List[int] = [int(r) for r in response.registers]
        return registers

    async def read_discrete_inputs(self, address: int, count: int) -> List[bool] | None:
        response = await self._call("read_discrete_inputs", address, count=count)
        if response is None: return None
        bits: List[bool] = [bool(b) for b in response.bits[:count]]
        return bits

    async def write_register(self, address: int, value: int) -> bool:
        return (await self._call("write_register", address, value)) is not None

    async def write_registers(self, address: int, values: Sequence[int]) -> bool:
        return (await self._call("write_registers", address, list(values))) is not None
