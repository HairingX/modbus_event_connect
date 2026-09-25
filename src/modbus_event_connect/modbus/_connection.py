"""The Modbus link: requests sent one at a time, each answer returned with its own status."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable

from .._clock import Clock, SystemClock


class FunctionCode(IntEnum):
    """The Modbus function codes this library sends."""
    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_COILS = 0x0F
    WRITE_MULTIPLE_REGISTERS = 0x10
    MASK_WRITE_REGISTER = 0x16

    @property
    def is_read(self) -> bool:
        return self in _READS

    @property
    def reads_bits(self) -> bool:
        """Whether the answer is one bit per address rather than a 16-bit register."""
        return self in (FunctionCode.READ_COILS, FunctionCode.READ_DISCRETE_INPUTS)


_READS = frozenset({FunctionCode.READ_COILS, FunctionCode.READ_DISCRETE_INPUTS,
                    FunctionCode.READ_HOLDING_REGISTERS, FunctionCode.READ_INPUT_REGISTERS})


class ExceptionCode(IntEnum):
    """The exception codes a device or gateway answers with, as the Modbus specification names them."""
    ILLEGAL_FUNCTION = 0x01
    """The device does not implement this function code."""
    ILLEGAL_DATA_ADDRESS = 0x02
    """At least one address in the request does not exist on this unit."""
    ILLEGAL_DATA_VALUE = 0x03
    """The request is malformed for this device - often a count larger than it accepts."""
    SERVER_DEVICE_FAILURE = 0x04
    """The address exists, but what is behind it failed - a peripheral that is offline."""
    ACKNOWLEDGE = 0x05
    SERVER_DEVICE_BUSY = 0x06
    """Try again later - some devices answer this while they persist a change."""
    GATEWAY_PATH_UNAVAILABLE = 0x0A
    """The gateway has no route to this unit - a configuration error."""
    GATEWAY_TARGET_FAILED = 0x0B
    """The gateway sent the request on, and the unit behind it did not answer."""


@dataclass(frozen=True)
class Request:
    """One Modbus request, in wire terms. `count` is for a read; a write carries `values` instead."""
    unit_id: int
    function: FunctionCode
    address: int
    count: int = 0
    values: tuple[int, ...] = ()
    and_mask: int = 0xFFFF
    or_mask: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.unit_id <= 255:
            raise ValueError(f"a unit id is 0-255, got {self.unit_id}")
        if not 0 <= self.address <= 0xFFFF:
            raise ValueError(f"an address is 0-65535, got {self.address}")
        if self.function.is_read and self.count < 1:
            raise ValueError(f"a read needs a count of at least 1, got {self.count}")
        if any(not 0 <= v <= 0xFFFF for v in self.values):
            raise ValueError(f"values must be 16-bit, got {self.values}")
        if not (0 <= self.and_mask <= 0xFFFF and 0 <= self.or_mask <= 0xFFFF):
            raise ValueError("and_mask and or_mask must be 16-bit")


@dataclass(frozen=True)
class Response:
    """The answer to one request. Exactly one of `ok`, `exception_code` or `no_answer` describes
    what happened."""
    ok: bool
    registers: tuple[int, ...] = ()
    """For a read: the registers, or one 0 / 1 per bit, exactly `count` of them."""
    exception_code: int = 0
    no_answer: bool = False
    detail: str = ""
    """Free text for logs. Never the host or port: it ends up in bug reports."""


@runtime_checkable
class ModbusConnection(Protocol):
    """One Modbus link, shared by every device behind it; implementers never raise."""

    @property
    def connected(self) -> bool: ...

    async def open(self) -> bool:
        """Open the link if it is not open. True when it is open afterwards."""
        ...

    async def close(self) -> None: ...

    async def request(self, request: Request) -> Response: ...


class ModbusTcpConnection:
    """Modbus TCP through pymodbus's `AsyncModbusTcpClient`, natively async; holds its own lock
    to measure the frame gap from the end of the previous answer."""

    def __init__(self, host: str, port: int = 502, *, timeout: float = 3.0, frame_gap: float = 0.0,
                 client: Any = None, clock: Clock | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Args:
            timeout: seconds to wait for an answer; a lost answer is reported, not retried.
            frame_gap: seconds kept between the end of one answer and the next request."""
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        if frame_gap < 0:
            raise ValueError(f"frame_gap cannot be negative, got {frame_gap}")
        self._host = host
        self._port = port
        self._timeout = timeout
        self._frame_gap = frame_gap
        self._client: Any = client
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_answer_at: float | None = None
        self._unit_keyword: str | None = None

    @property
    def client(self) -> Any:
        """The underlying pymodbus client, or None before first use."""
        return self._client

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "connected", False))

    async def open(self) -> bool:
        async with self._lock:
            return await self._open()

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def request(self, request: Request) -> Response:
        async with self._lock:
            if not self.connected and not await self._open():
                return Response(ok=False, no_answer=True, detail="the connection could not be opened")
            await self._keep_frame_gap()
            try:
                return await self._send(request)
            finally:
                self._last_answer_at = self._clock.monotonic()

    # --------------------------------------------------------------------------- internals

    def _ensure_client(self) -> Any:
        if self._client is None:
            from pymodbus.client import AsyncModbusTcpClient
            # retries=0: see `timeout` in __init__.
            self._client = AsyncModbusTcpClient(self._host, port=self._port, timeout=self._timeout,
                                                retries=0)
        return self._client

    async def _open(self) -> bool:
        if self.connected:
            return True
        try:
            result: Any = self._ensure_client().connect()
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            return False

    async def _close(self) -> None:
        if self._client is None:
            return
        result: Any = self._client.close()
        if inspect.isawaitable(result):
            await result

    async def _keep_frame_gap(self) -> None:
        if self._frame_gap <= 0 or self._last_answer_at is None:
            return
        wait = self._last_answer_at + self._frame_gap - self._clock.monotonic()
        if wait > 0:
            await self._sleep(wait)

    def _unit_kwargs(self, unit: int) -> dict[str, int]:
        """The keyword carrying the unit id: `device_id` or `slave`, whichever pymodbus accepts."""
        if self._unit_keyword is None:
            method: Any = self._ensure_client().read_holding_registers
            params = inspect.signature(method).parameters
            if "device_id" in params:
                self._unit_keyword = "device_id"
            elif "slave" in params:
                self._unit_keyword = "slave"
            elif any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                self._unit_keyword = "device_id"
            else:
                # Sending without a unit would reach whichever unit pymodbus defaults to - a
                # silent wrong answer. Refuse instead.
                raise TypeError("this pymodbus version takes neither device_id nor slave")
        return {self._unit_keyword: unit}

    def _call(self, request: Request, unit: dict[str, int]) -> Any:
        """Start the pymodbus call for `request`. Returns what pymodbus returns, awaited later."""
        client = self._ensure_client()
        f, address = request.function, request.address
        if f is FunctionCode.READ_COILS:
            return client.read_coils(address, count=request.count, **unit)
        if f is FunctionCode.READ_DISCRETE_INPUTS:
            return client.read_discrete_inputs(address, count=request.count, **unit)
        if f is FunctionCode.READ_HOLDING_REGISTERS:
            return client.read_holding_registers(address, count=request.count, **unit)
        if f is FunctionCode.READ_INPUT_REGISTERS:
            return client.read_input_registers(address, count=request.count, **unit)
        if f is FunctionCode.WRITE_SINGLE_COIL:
            return client.write_coil(address, bool(request.values[0]), **unit)
        if f is FunctionCode.WRITE_SINGLE_REGISTER:
            return client.write_register(address, request.values[0], **unit)
        if f is FunctionCode.WRITE_MULTIPLE_COILS:
            return client.write_coils(address, [bool(v) for v in request.values], **unit)
        if f is FunctionCode.WRITE_MULTIPLE_REGISTERS:
            return client.write_registers(address, list(request.values), **unit)
        return client.mask_write_register(address=address, and_mask=request.and_mask,
                                          or_mask=request.or_mask, **unit)

    async def _send(self, request: Request) -> Response:
        name = request.function.name
        unit = self._unit_kwargs(request.unit_id)      # outside the try: an incompatible pymodbus is a bug
        try:
            pending = self._call(request, unit)
            # A guard above pymodbus's own timeout: if the client hangs anyway, drop the link so
            # a late reply is never taken as the answer to the next request.
            async with asyncio.timeout(self._timeout * 2):
                response: Any = (await pending
                                 if inspect.isawaitable(pending) else pending)
        except TimeoutError:
            await self._close()
            return Response(ok=False, no_answer=True, detail=f"{name}: no answer in time")
        except Exception as err:
            # pymodbus reports a timeout, a dropped link and a garbled frame by raising. The
            # message can name the host, so only the kind of error is kept.
            return Response(ok=False, no_answer=True, detail=f"{name}: {type(err).__name__}")
        if response is None:
            return Response(ok=False, no_answer=True, detail=f"{name}: no response")
        if bool(getattr(response, "isError", lambda: False)()):
            code = int(getattr(response, "exception_code", 0) or 0)
            return Response(ok=False, exception_code=code,
                            detail=f"{name}: exception 0x{code:02X}")
        if not request.function.is_read:
            return Response(ok=True)
        if request.function.reads_bits:
            bits: list[Any] = list(getattr(response, "bits", None) or [])
            values = tuple(1 if b else 0 for b in bits[:request.count])
        else:
            registers: list[Any] = list(getattr(response, "registers", None) or [])
            values = tuple(int(r) for r in registers[:request.count])
        if len(values) < request.count:
            return Response(ok=False, detail=f"{name}: {len(values)} values in the reply, "
                                             f"{request.count} asked for")
        return Response(ok=True, registers=values)
