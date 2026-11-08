"""One Modbus device - a unit id on a connection - implementing `Device`: turns points
into requests and answers into `ReadResult` / `WriteResult`."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from event_connect import NO_RETRY, Breaker, CircuitBreaker, Clock, SystemClock

from ._access import (
    MAX_REGISTERS_PER_WRITE,
    BitWrite,
    Coil,
    DiscreteInput,
    HoldingRegister,
    InputRegister,
    ModbusProfile,
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
from .._device import EncodedWrite, Identity, Outcome, ReadResult, WriteResult
from .._model import Model, checked_profiles, profile_of
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

_LOGGER = logging.getLogger(__name__)


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
    spans: dict[_Span, list[Point[Any]]] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class _Answer:
    """What came of one request after busy retries.

    `response` is None when it was never sent because the device's breaker is open.
    """
    outcome: Outcome
    response: Response | None
    detail: str

    @property
    def exception_code(self) -> int:
        return self.response.exception_code if self.response is not None else 0


class ModbusDevice:
    """One unit on a Modbus connection; several may share one, each with its own busy retries and
    its own breaker."""

    def __init__(self, connection: ModbusConnection, unit_id: int, *, reconnect: CircuitBreaker,
                 profiles: Sequence[ModbusProfile], owns_connection: bool = False,
                 clock: Clock | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Args:
            reconnect: when the device is no longer asked, after requests in a row went
                unanswered, and when it is asked again, which also reopens a closed connection;
                its retry's `ready()` is asked before each try.
            profiles: the profile of every model the device may be; `use()` chooses one.
            owns_connection: whether `disconnect()` also closes the shared connection.

        Raises:
            ValueError: `unit_id` is not 0-255.
            ModelError: no profile, or two of one model.
        """
        if not 0 <= unit_id <= 255:
            raise ValueError(f"a unit id is 0-255, got {unit_id}")
        self._connection = connection
        self._unit = unit_id
        self._owns_connection = owns_connection
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._reconnect = reconnect
        self._profiles = checked_profiles(profiles)
        self._profile: ModbusProfile | None = None
        self._bit_locks: dict[int, asyncio.Lock] = {}
        self._breaker: Breaker = reconnect.breaker()

        self._requests = 0
        self._busy_repeats = 0
        self._refused_while_open = 0
        self._outcomes: Counter[Outcome] = Counter()
        self._latency_total = 0.0

    @classmethod
    def tcp(cls, host: str, port: int, unit_id: int, *, reconnect: CircuitBreaker,
            profiles: Sequence[ModbusProfile], timeout: float = 3.0, frame_gap: float = 0.0,
            clock: Clock | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> ModbusDevice:
        """A device on a Modbus TCP connection of its own, which `disconnect()` closes; `timeout`
        and `frame_gap` are the connection's, `clock` and `sleep` both's."""
        connection = ModbusTcpConnection(host, port, timeout=timeout, frame_gap=frame_gap, clock=clock, sleep=sleep)
        return cls(connection, unit_id, reconnect=reconnect, profiles=profiles, owns_connection=True,
                   clock=clock, sleep=sleep)

    # ---------------------------------------------------------------- Device

    @property
    def unit_id(self) -> int:
        """The unit id every request carries."""
        return self._unit

    @property
    def profile(self) -> ModbusProfile | None:
        """The profile `use()` chose; None before it chose one."""
        return self._profile

    @property
    def max_registers(self) -> int:
        """Most registers asked for in one read: the chosen profile's, and before one is chosen,
        the fewest any profile takes."""
        if self._profile is not None:
            return self._profile.max_registers
        return min(profile.max_registers for profile in self._profiles)

    @property
    def max_bits(self) -> int:
        """Most coils or discrete inputs asked for in one read: the chosen profile's, and before one
        is chosen, the fewest any profile takes."""
        if self._profile is not None:
            return self._profile.max_bits
        return min(profile.max_bits for profile in self._profiles)

    @property
    def reconnect(self) -> CircuitBreaker:
        """When the device is no longer asked after it stopped answering, and when it is asked again."""
        return self._reconnect

    async def connect(self) -> Identity | None:
        """Open the connection if it is not open, and ask the device again whatever its
        `reconnect` said. Modbus has no handshake, so the identity is empty."""
        self._breaker.reset()
        if not self._connection.connected and not await self._connection.open():
            return None
        return {}

    async def disconnect(self) -> None:
        if self._owns_connection:
            await self._connection.close()

    def use(self, model: Model) -> ModbusProfile:
        """Talk to the device as the profile of `model` says, and return that profile.

        Raises:
            ModelError: the device was given no profile of `model`.
        """
        self._profile = profile_of(self._profiles, model)
        return self._profile

    def _chosen(self) -> ModbusProfile:
        """The chosen profile: without it, no register has an address.

        Raises:
            RuntimeError: `use()` has not chosen one.
        """
        if self._profile is None:
            raise RuntimeError("use() the device with its model first: how its registers are numbered "
                               "is the model's profile's")
        return self._profile

    async def read(self, points: Sequence[Point[Any]]) -> Mapping[str, ReadResult]:
        tables: dict[type[Access], dict[_Span, list[Point[Any]]]] = {}
        for point in points:
            access = point.read
            if access is None:
                raise TypeError(f"point {point.key!r} has no read side")
            table = _table(access, point, "read")
            span = _Span(self._chosen().address(access), 1 if access.bits else point.registers)
            tables.setdefault(table, {}).setdefault(span, []).append(point)

        result: dict[str, ReadResult] = {}
        for table, spans in tables.items():
            limit = self.max_bits if table.bits else self.max_registers
            for batch in _batches(spans, limit):
                await self._read_batch(_READ_FUNCTION[table], batch, result)
        return result

    async def write(self, point: Point[Any], value: EncodedWrite) -> WriteResult:
        """Write once; a 0x06 answer is `BUSY`."""
        access = point.write
        if access is None:
            raise TypeError(f"point {point.key!r} has no write side")
        table = _table(access, point, "write")
        if not access.writable:
            raise TypeError(f"point {point.key!r}: {table.__name__} cannot be written")
        address = self._chosen().address(access)

        if table is Coil:
            if value.bit_index is not None or len(value.registers) != 1 or value.registers[0] not in (0, 1):
                raise ValueError(f"point {point.key!r}: a coil is written with one register, 0 or 1, "
                                 f"not {value!r}")
            answer = await self._exchange(Request(self._unit, FunctionCode.WRITE_SINGLE_COIL, address,
                                                  values=value.registers), retry=False)
        elif value.bit_index is not None:
            answer = await self._write_bit(address, value.bit_index, value.bit_value)
        else:
            if len(value.registers) > MAX_REGISTERS_PER_WRITE:
                raise ValueError(f"point {point.key!r}: {len(value.registers)} registers exceed the "
                                 f"Modbus limit of {MAX_REGISTERS_PER_WRITE} per write")
            answer = await self._exchange(self._register_write(address, value.registers), retry=False)
        return WriteResult(answer.outcome, answer.exception_code, answer.detail)

    def diagnostics(self) -> Mapping[str, object]:
        """Counters for a bug report; never a host or port.

        `outcomes` counts every exchange (a request plus its busy retries) by how it ended. Of the
        breaker: whether it is `open`, and for how many seconds more (`open_for`, infinite once it
        gave up), its `openings` since the last answer, whether it `gave_up`, and the requests
        refused while it was open.
        """
        open_for = self._breaker.open_for(self._clock.monotonic())
        return {
            "unit_id": self._unit,
            "requests": self._requests,
            "outcomes": {outcome.name: self._outcomes[outcome] for outcome in Outcome},
            "busy_retries": self._busy_repeats,
            "consecutive_no_answers": self._breaker.unanswered_in_a_row,
            "open": open_for > 0,
            "open_for": open_for,
            "openings": self._breaker.openings,
            "gave_up": self._breaker.gave_up,
            "refused_while_open": self._refused_while_open,
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
        if len(registers) == 1 and self._chosen().single_write is SingleWrite.FC06:
            return Request(self._unit, FunctionCode.WRITE_SINGLE_REGISTER, address, values=registers)
        return Request(self._unit, FunctionCode.WRITE_MULTIPLE_REGISTERS, address,
                       count=len(registers), values=registers)

    async def _write_bit(self, address: int, bit: int, on: bool) -> _Answer:
        mask = 1 << bit
        if self._chosen().bit_write is BitWrite.MASK:
            return await self._exchange(Request(self._unit, FunctionCode.MASK_WRITE_REGISTER, address,
                                                and_mask=~mask & 0xFFFF, or_mask=mask if on else 0),
                                        retry=False)
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
            return await self._exchange(self._register_write(address, (register,)), retry=False)

    # ------------------------------------------------------------ one exchange, the breaker

    async def _exchange(self, request: Request, *, retry: bool = True) -> _Answer:
        """Send `request`, trying "busy" again as the profile's `busy` says unless `retry` is False.
        Never raises for the device's doing."""
        rule = self._chosen().busy if retry else NO_RETRY
        started = self._clock.monotonic()
        tries = 0
        while True:
            response = await self._send(request)
            if response is None:
                self._refused_while_open += 1
                return _Answer(Outcome.NO_ANSWER, None,
                               f"{_where(request)}: not sent, the breaker is open after "
                               f"{self._reconnect.after} unanswered requests")
            tries += 1
            if response.exception_code != ExceptionCode.SERVER_DEVICE_BUSY:
                break
            delay = rule.next_delay(tries, self._clock.monotonic() - started)
            if delay is None:
                break
            self._busy_repeats += 1
            await self._sleep(delay)
        outcome = _outcome(response)
        self._outcomes[outcome] += 1
        return _Answer(outcome, response, _describe(request, response, outcome, tries - 1))

    async def _send(self, request: Request) -> Response | None:
        """Puts one request on the connection, or returns `None` while the breaker is open. Once
        its time is up, it lets one probe through if `reconnect`'s retry is ready; the others wait
        for its answer."""
        if not await self._ready_to_probe(request):
            return None
        attempt = self._breaker.attempt(self._clock.monotonic())
        if attempt is None:
            return None
        with attempt:
            started = self._clock.monotonic()
            response = await self._connection.request(request)
            self._requests += 1
            self._latency_total += self._clock.monotonic() - started
            # A gateway only answers 0x0B after waiting out its own timeout on the bus, so a device
            # behind it that stopped answering costs every other device that time, like a timeout.
            if response.no_answer or response.exception_code == ExceptionCode.GATEWAY_TARGET_FAILED:
                openings = self._breaker.openings
                attempt.unanswered(self._clock.monotonic())
                if self._breaker.openings != openings:
                    self._log_opening(request)
            else:
                attempt.answered()
        return response

    async def _ready_to_probe(self, request: Request) -> bool:
        """Whether a probe that is due may go, as `reconnect`'s retry says; True when none is due."""
        now = self._clock.monotonic()
        breaker = self._breaker
        if not breaker.is_open or breaker.open_for(now) > 0:
            return True
        if await self._reconnect.retry.ready():
            return True
        breaker.postpone(self._clock.monotonic())
        _LOGGER.debug("unit %d: reconnect not ready; asking again in %.1f s",
                      request.unit_id, breaker.open_for(self._clock.monotonic()))
        return False

    def _log_opening(self, request: Request) -> None:
        breaker = self._breaker
        if breaker.gave_up:
            _LOGGER.debug("unit %d: no answer after try %d; not asked again until connected",
                          request.unit_id, breaker.openings)
        else:
            _LOGGER.debug("unit %d: no answer after try %d; asking again in %.1f s",
                          request.unit_id, breaker.openings, breaker.open_for(self._clock.monotonic()))


# ================================================================================ helpers


def _table(access: Access, point: Point[Any], side: str) -> type[Access]:
    table = type(access)
    if table not in _READ_FUNCTION:
        raise TypeError(f"point {point.key!r}: the {side} side is {table.__name__}, not a Modbus table")
    return table


def _batches(spans: Mapping[_Span, list[Point[Any]]], limit: int) -> list[_Batch]:
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
