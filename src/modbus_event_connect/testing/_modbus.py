"""Simulated Modbus units behind a simulated gateway - the standard test double for this library.

Addresses here are the ones a request carries; register numbering is the protocol layer's."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping

from ..modbus._connection import ExceptionCode, FunctionCode, Request, Response

NO_ANSWER = -1
"""A fault code meaning "the device never answers": the request gets `no_answer`."""

_BIT_READS = (FunctionCode.READ_COILS, FunctionCode.READ_DISCRETE_INPUTS)
_REGISTER_READS = (FunctionCode.READ_HOLDING_REGISTERS, FunctionCode.READ_INPUT_REGISTERS)


class SimulatedModbusDevice:
    """One Modbus unit: register images plus the refusals of a real device.

    First match wins: unsupported function (0x01), a planted fault, `busy_for` (0x06), too
    large a read (0x03), then a missing address (0x02); otherwise it reads or writes the image."""

    def __init__(self, *, coils: Mapping[int, int] | None = None,
                 discrete_inputs: Mapping[int, int] | None = None,
                 holding_registers: Mapping[int, int] | None = None,
                 input_registers: Mapping[int, int] | None = None,
                 faults: Mapping[tuple[FunctionCode, int], int] | None = None,
                 busy_for: int = 0, max_registers: int = 125, max_bits: int = 2000,
                 unsupported: Iterable[FunctionCode] = ()) -> None:
        self.coils: dict[int, int] = {a: 1 if v else 0 for a, v in (coils or {}).items()}
        self.discrete_inputs: dict[int, int] = {a: 1 if v else 0 for a, v in (discrete_inputs or {}).items()}
        self.holding_registers: dict[int, int] = dict(holding_registers or {})
        self.input_registers: dict[int, int] = dict(input_registers or {})
        self.faults: dict[tuple[FunctionCode, int], int] = dict(faults or {})
        self.busy_for = busy_for
        self.max_registers = max_registers
        self.max_bits = max_bits
        self.unsupported: frozenset[FunctionCode] = frozenset(unsupported)
        self.requests: list[Request] = []

    def function_codes(self) -> list[FunctionCode]:
        """The function code of every request received, in order - the usual thing to assert on."""
        return [r.function for r in self.requests]

    def handle(self, request: Request) -> Response:
        """Answer one request, as the unit would."""
        self.requests.append(request)
        f = request.function
        if f in self.unsupported:
            return _refuse(f, ExceptionCode.ILLEGAL_FUNCTION)
        touched = _touched(request)
        for address in touched:
            fault = self.faults.get((f, address))
            if fault is not None:
                if fault == NO_ANSWER:
                    return Response(ok=False, no_answer=True, detail=f"{f.name}: no answer (simulated)")
                return _refuse(f, fault)
        if self.busy_for > 0:
            self.busy_for -= 1
            return _refuse(f, ExceptionCode.SERVER_DEVICE_BUSY)
        if f in _REGISTER_READS and request.count > self.max_registers:
            return _refuse(f, ExceptionCode.ILLEGAL_DATA_VALUE)
        if f in _BIT_READS and request.count > self.max_bits:
            return _refuse(f, ExceptionCode.ILLEGAL_DATA_VALUE)
        image = self._image(f)
        if any(a not in image for a in touched):
            return _refuse(f, ExceptionCode.ILLEGAL_DATA_ADDRESS)

        if f.is_read:
            return Response(ok=True, registers=tuple(image[a] for a in touched))
        if f is FunctionCode.MASK_WRITE_REGISTER:
            current = image[request.address]
            image[request.address] = (current & request.and_mask) | (request.or_mask & ~request.and_mask & 0xFFFF)
        elif f in (FunctionCode.WRITE_SINGLE_COIL, FunctionCode.WRITE_MULTIPLE_COILS):
            for a, v in zip(touched, request.values):
                image[a] = 1 if v else 0
        else:
            for a, v in zip(touched, request.values):
                image[a] = v
        return Response(ok=True)

    def _image(self, function: FunctionCode) -> dict[int, int]:
        if function in (FunctionCode.READ_COILS, FunctionCode.WRITE_SINGLE_COIL,
                        FunctionCode.WRITE_MULTIPLE_COILS):
            return self.coils
        if function is FunctionCode.READ_DISCRETE_INPUTS:
            return self.discrete_inputs
        if function is FunctionCode.READ_INPUT_REGISTERS:
            return self.input_registers
        return self.holding_registers


def _touched(request: Request) -> range:
    """The addresses a request reads or writes."""
    f = request.function
    if f.is_read:
        return range(request.address, request.address + request.count)
    if f in (FunctionCode.WRITE_MULTIPLE_REGISTERS, FunctionCode.WRITE_MULTIPLE_COILS):
        return range(request.address, request.address + len(request.values))
    return range(request.address, request.address + 1)


def _refuse(function: FunctionCode, code: int) -> Response:
    return Response(ok=False, exception_code=code, detail=f"{function.name}: exception 0x{code:02X}")


class SimulatedModbusGateway:
    """Several `SimulatedModbusDevice`s behind one connection, asserting requests never overlap on the wire.

    Set `link_down` to fail `open()` and every request; `half_open` to fail every request while
    `connected` stays true, like a pulled cable."""

    def __init__(self, units: Mapping[int, SimulatedModbusDevice], *, delay: float = 0.0) -> None:
        self.units: dict[int, SimulatedModbusDevice] = dict(units)
        self.delay = delay
        self.link_down = False
        self.half_open = False
        self.requests: list[tuple[int, Request]] = []
        self.opens = 0
        self.closes = 0
        self.max_in_flight = 0
        self._open = False
        self._in_flight = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._open and not self.link_down

    async def open(self) -> bool:
        if self.link_down:
            return False
        if not self._open:
            self._open = True
            self.opens += 1
        return True

    async def close(self) -> None:
        if self._open:
            self._open = False
            self.closes += 1

    async def request(self, request: Request) -> Response:
        async with self._lock:
            if not self.connected and not await self.open():
                return Response(ok=False, no_answer=True, detail="the link is down (simulated)")
            self._in_flight += 1
            try:
                if self._in_flight > 1:
                    raise AssertionError("two requests are on the gateway's wire at once")
                self.max_in_flight = max(self.max_in_flight, self._in_flight)
                self.requests.append((request.unit_id, request))
                await asyncio.sleep(self.delay)
                if self.link_down or self.half_open:
                    return Response(ok=False, no_answer=True, detail="no answer (simulated)")
                unit = self.units.get(request.unit_id)
                if unit is None:
                    return _refuse(request.function, ExceptionCode.GATEWAY_TARGET_FAILED)
                return unit.handle(request)
            finally:
                self._in_flight -= 1
