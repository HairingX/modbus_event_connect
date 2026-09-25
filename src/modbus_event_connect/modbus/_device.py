"""One Modbus device - a unit id on a connection - implementing `Device`: turns points
into requests and answers into `ReadResult` / `WriteResult`."""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ._access import (
    MAX_REGISTERS_PER_WRITE,
    BitWrite,
    Coil,
    DiscreteInput,
    HoldingRegister,
    InputRegister,
    ModbusOptions,
    SingleWrite,
)
from ._connection import (
    ExceptionCode,
    FunctionCode,
    ModbusConnection,
    ModbusTcpConnection,
    Request,
    Response,
)
from .._clock import Clock, SystemClock
from .._device import EncodedWrite, Identity, Outcome, ProtocolOptions, ReadResult, WriteResult
from .._point import Access, Point

_READ_FUNCTION: Mapping[type[Access], FunctionCode] = {
    Coil: FunctionCode.READ_COILS,
    DiscreteInput: FunctionCode.READ_DISCRETE_INPUTS,
    HoldingRegister: FunctionCode.READ_HOLDING_REGISTERS,
    InputRegister: FunctionCode.READ_INPUT_REGISTERS,
}

_OUTCOME_BY_CODE: Mapping[int, Outcome] = {
    ExceptionCode.ILLEGAL_FUNCTION: Outcome.UNSUPPORTED,
    ExceptionCode.ILLEGAL_DATA_ADDRESS: Outcome.MISSING,
    ExceptionCode.SERVER_DEVICE_FAILURE: Outcome.OFFLINE,
    ExceptionCode.SERVER_DEVICE_BUSY: Outcome.BUSY,
    # The gateway answered on behalf of this device: the device itself did not. For this
    # device that is no answer at all - the same as a timeout on a direct link.
    ExceptionCode.GATEWAY_TARGET_FAILED: Outcome.NO_ANSWER,
}
"""Exception codes the core can act on. 0x03, 0x0A and anything unlisted are ERROR."""

_CONNECTION_KEYWORDS = ("timeout", "frame_gap")
"""`ModbusDevice.tcp()` keywords that belong to the connection rather than the device."""


@dataclass(frozen=True)
class _Span:
    """The addresses one point's read side covers: `count` registers, or bits, from `start`."""
    start: int
    count: int

    @property
    def end(self) -> int:
        return self.start + self.count


@dataclass
class _Batch:
    """Spans read with one request. `spans` maps each distinct span to the points sharing it."""
    start: int
    end: int
    spans: dict[_Span, list[Point]] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class _Answer:
    """What came of one request after busy retries.

    `response` is None when it was never sent because the device is backing off.
    """
    outcome: Outcome
    response: Response | None
    detail: str

    @property
    def exception_code(self) -> int:
        return self.response.exception_code if self.response is not None else 0


class ModbusDevice:
    """One unit on a Modbus connection; several may share one, each with its own retries and backoff."""

    def __init__(self, connection: ModbusConnection, unit_id: int = 1, *,
                 owns_connection: bool = False, clock: Clock | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 busy_retries: int = 4, busy_delay: float = 0.2,
                 backoff_after: int = 3, backoff_for: float = 60.0) -> None:
        """Args:
            owns_connection: whether `disconnect()` also closes the shared connection.
            busy_retries, busy_delay: retries for a 0x06 answer, doubling `busy_delay` each time.
            backoff_after, backoff_for: unanswered requests before refusing new ones for this long."""
        if not 0 <= unit_id <= 255:
            raise ValueError(f"a unit id is 0-255, got {unit_id}")
        if busy_retries < 0 or busy_delay < 0:
            raise ValueError("busy_retries and busy_delay cannot be negative")
        if backoff_after < 1 or backoff_for < 0:
            raise ValueError("backoff_after must be at least 1 and backoff_for cannot be negative")
        self._connection = connection
        self._unit = unit_id
        self._owns_connection = owns_connection
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._busy_retries = busy_retries
        self._busy_delay = busy_delay
        self._backoff_after = backoff_after
        self._backoff_for = backoff_for
        self._options: ModbusOptions | None = None
        self._bit_locks: dict[int, asyncio.Lock] = {}

        self._consecutive_no_answers = 0
        self._backoff_until: float | None = None
        self._probing = False
        self._requests = 0
        self._busy_repeats = 0
        self._refused_while_backing_off = 0
        self._outcomes: Counter[Outcome] = Counter()
        self._latency_total = 0.0

    @classmethod
    def tcp(cls, host: str, port: int = 502, unit_id: int = 1, **kwargs: Any) -> ModbusDevice:
        """A device on a Modbus TCP connection of its own. `timeout` and `frame_gap` go to the
        connection, `clock` to both, everything else to the device."""
        connection_kwargs: dict[str, Any] = {k: kwargs.pop(k) for k in _CONNECTION_KEYWORDS if k in kwargs}
        if "clock" in kwargs:
            connection_kwargs["clock"] = kwargs["clock"]
        connection = ModbusTcpConnection(host, port, **connection_kwargs)
        return cls(connection, unit_id, owns_connection=True, **kwargs)

    # ---------------------------------------------------------------- Device

    @property
    def unit_id(self) -> int:
        return self._unit

    @property
    def options(self) -> ModbusOptions | None:
        """The model's options; None until `configure()`."""
        return self._options

    async def connect(self) -> Identity | None:
        """Open the connection if it is not open. Modbus has no handshake, so the identity is empty."""
        if not self._connection.connected and not await self._connection.open():
            return None
        return {}

    async def disconnect(self) -> None:
        if self._owns_connection:
            await self._connection.close()

    def configure(self, options: ProtocolOptions | None) -> None:
        if options is None:
            self._options: ModbusOptions | None = None
        elif isinstance(options, ModbusOptions):
            self._options = options
        else:
            raise TypeError(f"a Modbus device needs ModbusOptions, not {type(options).__name__}")

    def _configured(self) -> ModbusOptions:
        if self._options is None:
            raise RuntimeError("configure() the device with its model's options before using it")
        return self._options

    async def read(self, points: Sequence[Point]) -> Mapping[str, ReadResult]:
        tables: dict[type[Access], dict[_Span, list[Point]]] = {}
        for point in points:
            access = point.read
            if access is None:
                raise TypeError(f"point {point.key!r} has no read side")
            table = _table(access, point, "read")
            span = _Span(self._configured().address(access), 1 if access.bits else point.registers)
            tables.setdefault(table, {}).setdefault(span, []).append(point)

        result: dict[str, ReadResult] = {}
        for table, spans in tables.items():
            limit = self._configured().max_bits if table.bits else self._configured().max_registers
            for batch in _batches(spans, limit):
                await self._read_batch(_READ_FUNCTION[table], batch, result)
        return result

    async def write(self, point: Point, value: EncodedWrite) -> WriteResult:
        access = point.write
        if access is None:
            raise TypeError(f"point {point.key!r} has no write side")
        table = _table(access, point, "write")
        if not access.writable:
            raise TypeError(f"point {point.key!r}: {table.__name__} cannot be written")
        address = self._configured().address(access)

        if table is Coil:
            if value.bit_index is not None or len(value.registers) != 1 or value.registers[0] not in (0, 1):
                raise ValueError(f"point {point.key!r}: a coil is written with one register, 0 or 1, "
                                 f"not {value!r}")
            answer = await self._exchange(Request(self._unit, FunctionCode.WRITE_SINGLE_COIL, address,
                                                  values=value.registers))
        elif value.bit_index is not None:
            answer = await self._write_bit(address, value.bit_index, value.bit_value)
        else:
            if len(value.registers) > MAX_REGISTERS_PER_WRITE:
                raise ValueError(f"point {point.key!r}: {len(value.registers)} registers exceed the "
                                 f"Modbus limit of {MAX_REGISTERS_PER_WRITE} per write")
            answer = await self._exchange(self._register_write(address, value.registers))
        return WriteResult(answer.outcome, answer.exception_code, answer.detail)

    def diagnostics(self) -> Mapping[str, object]:
        """Counters for a bug report; never a host or port. `outcomes` counts every exchange (a
        request plus its busy retries) by how it ended."""
        backing_off = self._backing_off()
        return {
            "unit_id": self._unit,
            "requests": self._requests,
            "outcomes": {outcome.name: self._outcomes[outcome] for outcome in Outcome},
            "busy_retries": self._busy_repeats,
            "consecutive_no_answers": self._consecutive_no_answers,
            "backing_off": backing_off,
            "backing_off_for": (self._backoff_until - self._clock.monotonic())
                           if backing_off and self._backoff_until is not None else 0.0,
            "refused_while_backing_off": self._refused_while_backing_off,
            "average_latency": self._latency_total / self._requests if self._requests else None,
        }

    # ---------------------------------------------------------------------- reading

    async def _read_batch(self, function: FunctionCode, batch: _Batch, result: dict[str, ReadResult]) -> None:
        answer = await self._exchange(Request(self._unit, function, batch.start,
                                              count=batch.end - batch.start))
        if answer.outcome is Outcome.MISSING and len(batch.spans) > 1:
            # One absent address refuses the whole request. Halving it until the refusal is
            # pinned down finds a few absent addresses among many in few requests.
            ordered = sorted(batch.spans, key=lambda s: (s.start, s.count))
            middle = len(ordered) // 2
            for half in (ordered[:middle], ordered[middle:]):
                for part in _batches({span: batch.spans[span] for span in half}, batch.end - batch.start):
                    await self._read_batch(function, part, result)
            return
        _deliver(function, batch, answer, result)

    # ---------------------------------------------------------------------- writing

    def _register_write(self, address: int, registers: tuple[int, ...]) -> Request:
        if len(registers) == 1 and self._configured().single_write is SingleWrite.FC06:
            return Request(self._unit, FunctionCode.WRITE_SINGLE_REGISTER, address, values=registers)
        return Request(self._unit, FunctionCode.WRITE_MULTIPLE_REGISTERS, address,
                       count=len(registers), values=registers)

    async def _write_bit(self, address: int, bit: int, on: bool) -> _Answer:
        mask = 1 << bit
        if self._configured().bit_write is BitWrite.MASK:
            return await self._exchange(Request(self._unit, FunctionCode.MASK_WRITE_REGISTER, address,
                                                and_mask=~mask & 0xFFFF, or_mask=mask if on else 0))
        # Read-modify-write. Without the lock, two bit writes to one register interleave as
        # read, read, write, write - and the second write puts back the first one's bit.
        lock = self._bit_locks.setdefault(address, asyncio.Lock())
        async with lock:
            current = await self._exchange(Request(self._unit, FunctionCode.READ_HOLDING_REGISTERS,
                                                   address, count=1))
            if current.outcome is not Outcome.OK or current.response is None:
                return current
            register = current.response.registers[0]
            register = register | mask if on else register & ~mask & 0xFFFF
            return await self._exchange(self._register_write(address, (register,)))

    # ------------------------------------------------------------- one exchange, backoff

    async def _exchange(self, request: Request) -> _Answer:
        """Send `request`, retrying "busy" with backoff. Never raises for the device's doing."""
        delay = self._busy_delay
        attempt = 0
        while True:
            response = await self._send(request)
            if response is None:
                self._refused_while_backing_off += 1
                return _Answer(Outcome.NO_ANSWER, None,
                               f"{_where(request)}: not sent, backing off after "
                               f"{self._backoff_after} unanswered requests")
            if response.exception_code != ExceptionCode.SERVER_DEVICE_BUSY or attempt >= self._busy_retries:
                break
            attempt += 1
            self._busy_repeats += 1
            await self._sleep(delay)
            delay *= 2
        outcome = _outcome(response)
        self._outcomes[outcome] += 1
        return _Answer(outcome, response, _describe(request, response, outcome, attempt))

    async def _send(self, request: Request) -> Response | None:
        """Puts one request on the connection, or returns `None` while the device is backing off.
        Only one probe request goes through once the backoff expires; others wait for its answer."""
        probe = False
        if self._backoff_until is not None:
            if self._probing or self._clock.monotonic() < self._backoff_until:
                return None
            self._probing = probe = True
        started = self._clock.monotonic()
        try:
            response = await self._connection.request(request)
        finally:
            if probe:
                self._probing = False
        self._requests += 1
        self._latency_total += self._clock.monotonic() - started
        # A gateway only answers 0x0B after waiting out its own timeout on the bus, so a device
        # behind it that stopped answering costs every other device that time, like a timeout.
        if response.no_answer or response.exception_code == ExceptionCode.GATEWAY_TARGET_FAILED:
            self._consecutive_no_answers += 1
            if probe or self._consecutive_no_answers >= self._backoff_after:
                self._backoff_until = self._clock.monotonic() + self._backoff_for
        else:
            self._consecutive_no_answers = 0
            self._backoff_until = None
        return response

    def _backing_off(self) -> bool:
        return self._backoff_until is not None and self._clock.monotonic() < self._backoff_until


# ================================================================================ helpers


def _table(access: Access, point: Point, side: str) -> type[Access]:
    table = type(access)
    if table not in _READ_FUNCTION:
        raise TypeError(f"point {point.key!r}: the {side} side is {table.__name__}, not a Modbus table")
    return table


def _batches(spans: Mapping[_Span, list[Point]], limit: int) -> list[_Batch]:
    """Groups spans into requests of at most `limit` registers or bits, joining only spans that
    touch or overlap - reading across a gap could ask for addresses no point needs."""
    batches: list[_Batch] = []
    for span in sorted(spans, key=lambda s: (s.start, s.count)):
        last = batches[-1] if batches else None
        if last is not None and span.start <= last.end and max(last.end, span.end) - last.start <= limit:
            last.end = max(last.end, span.end)
        else:
            last = _Batch(span.start, span.end)
            batches.append(last)
        last.spans[span] = spans[span]
    return batches


def _deliver(function: FunctionCode, batch: _Batch, answer: _Answer, result: dict[str, ReadResult]) -> None:
    """One ReadResult per point in `batch`, from the answer to the request that covered it."""
    registers = answer.response.registers if answer.response is not None else ()
    if answer.outcome is Outcome.OK and len(registers) < batch.end - batch.start:
        answer = _Answer(Outcome.ERROR, answer.response,
                         f"{function.name} {batch.start}+{batch.end - batch.start}: "
                         f"{len(registers)} values in the reply")
    for span, points in batch.spans.items():
        if answer.outcome is Outcome.OK:
            offset = span.start - batch.start
            raw = ReadResult(Outcome.OK, tuple(registers[offset:offset + span.count]))
        else:
            raw = ReadResult(answer.outcome, exception_code=answer.exception_code, detail=answer.detail)
        for point in points:
            result[point.key] = raw


def _outcome(response: Response) -> Outcome:
    if response.ok:
        return Outcome.OK
    if response.no_answer:
        return Outcome.NO_ANSWER
    return _OUTCOME_BY_CODE.get(response.exception_code, Outcome.ERROR)


def _where(request: Request) -> str:
    """The request in a log line: function and address, never the connection's host."""
    count = request.count or len(request.values) or 1
    return f"unit {request.unit_id} {request.function.name} {request.address}" + (f"+{count}" if count > 1 else "")


def _describe(request: Request, response: Response, outcome: Outcome, busy_retries: int) -> str:
    if outcome is Outcome.OK:
        return ""
    text = _where(request)
    if response.exception_code:
        try:
            name = ExceptionCode(response.exception_code).name
        except ValueError:
            name = "unknown exception"
        text += f": {name} (0x{response.exception_code:02X})"
    if busy_retries:
        text += f" after {busy_retries} busy retries"
    if response.detail and not response.exception_code:
        text += f" - {response.detail}"
    return text
