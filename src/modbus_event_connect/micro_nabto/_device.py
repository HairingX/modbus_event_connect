"""One micro_nabto device implementing `Device`: points in, `ReadResult` / `WriteResult` out."""
from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import metadata
from typing import Any

from . import _wire as wire
from ._access import DatapointRegister, SetpointRegister
from ._connection import MicroNabtoConnection, describe
from .._data_type import DataTypeKind
from .._device import EncodedWrite, Identity, Outcome, ProtocolOptions, ReadResult, WriteResult
from .._point import Access, Point
from .._resilience import Fixed, Retry, Steps, Wait

_LOGGER = logging.getLogger(__name__)

MAX_REGISTERS_PER_READ = 64
"""Registers per read request; a CTS 402 answered 108, so this leaves room."""

REREAD_ZEROS_FOR = 7.5
"""Seconds after an answer of only zeros, where values were, until the zeros are taken: short of
the 10 s between two fast polls, as a Nilan gateway was seen answering a new session's first
reads with zeros, and values 10 s later."""

_HISTORY = 5
"""Requests a warning about zeros names from before them."""
_ENDINGS = ("values_at_once", "values_after_waiting", "values_in_halves", "zeros_taken")

_MAX_OBJECT = 0xFF
_MAX_ADDRESS: Mapping[tuple[str, type[Access]], int] = {
    ("read", DatapointRegister): 0xFFFF_FFFF,
    ("read", SetpointRegister): 0xFFFF,
    ("write", SetpointRegister): 0xFFFF_FFFF,
}
"""SetpointRegister reads carry a two-byte address; datapoint reads and setpoint writes four."""

_Parse = Callable[[bytes], list[int] | None]
_Register = tuple[type[Access], int, int]
"""A register by its space, object and address."""
_READS: Mapping[type[Access], tuple[Callable[[Sequence[tuple[int, int]]], bytes], _Parse]] = {
    DatapointRegister: (wire.datapoint_read, wire.datapoint_values),
    SetpointRegister: (wire.setpoint_read, wire.setpoint_values),
}


@dataclass(frozen=True)
class RereadZeros:
    """How registers answered with only zeros, where they held values, are read again before the
    zeros are taken; until then their last values are kept."""
    within: float = REREAD_ZEROS_FOR
    """Seconds after the zeros until they are taken."""
    wait: Wait = Steps(0.5, then=Fixed(1.0))
    """When to read them again: the first time this long after the zeros, each next time this long
    after the time before."""
    at_once: bool = True
    """Whether they are read again at once, before anything else."""
    halves: bool = True
    """Whether, at `within`, they are read in two halves, whose values are taken if they bring
    any: a device may answer only zeros when asked for too many registers at once."""

    def __post_init__(self) -> None:
        if not self.within >= 0:
            raise ValueError(f"within cannot be negative, got {self.within}")


TAKE_ZEROS = RereadZeros(within=0.0, at_once=False, halves=False)
"""Zeros are taken as they come: for a device known to answer zeros only where they are."""


@dataclass(frozen=True)
class MicroNabtoOptions(ProtocolOptions):
    """What a model states about its device in micro_nabto terms."""
    max_registers: int = MAX_REGISTERS_PER_READ
    """Most registers the device is asked for in one read."""
    later_answer_wait: float | None = None
    """Seconds a read sent again and answered with only zeros, where values were, is kept open
    for a later answer; None leaves the connection's. One given to the connection overrides it."""
    reread_zeros_for: float | None = None
    """Seconds after an answer of only zeros, where values were, until the zeros are taken; None
    leaves `zeros`'. One given to the device overrides it."""
    zeros: RereadZeros | None = None
    """How an answer of only zeros, where values were, is read again; None leaves `RereadZeros()`.
    One given to the device overrides it."""

    def __post_init__(self) -> None:
        if self.max_registers < 1:
            raise ValueError(f"max_registers must be at least 1, got {self.max_registers}")
        if self.later_answer_wait is not None and self.later_answer_wait < 0:
            raise ValueError(f"later_answer_wait cannot be negative, got {self.later_answer_wait}")
        if self.reread_zeros_for is not None and self.reread_zeros_for < 0:
            raise ValueError(f"reread_zeros_for cannot be negative, got {self.reread_zeros_for}")

    def problems(self, point: Point[Any]) -> list[str]:
        found: list[str] = []
        for side, access in (("read", point.read), ("write", point.write)):
            if access is None:
                continue
            if not isinstance(access, DatapointRegister | SetpointRegister):
                found.append(f"the {side} side is {type(access).__name__}, not a micro_nabto space")
                continue
            limit = _MAX_ADDRESS.get((side, type(access)))
            if limit is not None and access.address + point.registers - 1 > limit:
                found.append(f"the {side} side ends past address {limit}")
            if access.obj > _MAX_OBJECT:
                found.append(f"the {side} side's object {access.obj} is more than {_MAX_OBJECT}")
            if side == "read" and point.registers > self.max_registers:
                found.append(f"the read side spans {point.registers}, more than the {self.max_registers} "
                             f"the device is asked for in one request")
        if point.write is not None and point.data_type.kind is DataTypeKind.BIT:
            found.append("a single bit cannot be written over micro_nabto")
        return found


@dataclass
class _Episode:
    """Registers answered with only zeros where they held values, until values come or the zeros
    are taken."""
    span: str
    started: float
    """Monotonic time of the first answer of zeros."""
    readings: list[str] = field(default_factory=list[str])
    """What each reading again answered, and when after the first zeros."""


@dataclass(frozen=True)
class _Batch:
    """Points read with one request, and the registers each spans."""
    space: type[Access]
    obj: int
    points: tuple[Point[Any], ...]

    @property
    def items(self) -> list[tuple[int, int]]:
        return [(self.obj, _read_side(p).address + i) for p in self.points for i in range(p.registers)]


class MicroNabtoDevice:
    """A micro_nabto device; datapoints and setpoints are read in batches, setpoints written."""

    def __init__(self, connection: MicroNabtoConnection, *, owns_connection: bool = False,
                 reread_zeros_for: float | None = None, zeros: RereadZeros | None = None) -> None:
        """Args:
            owns_connection: whether `disconnect()` also closes the connection.
            reread_zeros_for: seconds after an answer of only zeros, where values were, until the
                zeros are taken; None leaves it to `zeros`.
            zeros: how an answer of only zeros, where values were, is read again; None leaves it
                to the model, and to `RereadZeros()`.

        Raises:
            ValueError: `reread_zeros_for` is negative.
        """
        if reread_zeros_for is not None and reread_zeros_for < 0:
            raise ValueError(f"reread_zeros_for cannot be negative, got {reread_zeros_for}")
        self._connection = connection
        self._owns_connection = owns_connection
        self._own_zeros = _zeros(zeros, reread_zeros_for)
        self._episodes: dict[_Register, _Episode] = {}
        self._endings: Counter[str] = Counter()
        self._zero_rereads = 0
        self._longest_until_values = 0.0
        self._largest_with_values = 0
        self._smallest_of_zeros: int | None = None
        self._recent: deque[tuple[float, str]] = deque(maxlen=_HISTORY + 1)
        self._this_read: list[int] = []
        self._options: MicroNabtoOptions | None = None
        self._outcomes: Counter[Outcome] = Counter()
        self._isolated = 0
        self._only_zeros = 0
        self._last: dict[_Register, int] = {}
        self._zeroed_at: dict[_Register, float] = {}

    @classmethod
    def udp(cls, email: str, *, host: str | None = None, device_id: str | None = None,
            reconnect: Retry | None, reread_zeros_for: float | None = None, zeros: RereadZeros | None = None,
            **kwargs: Any) -> MicroNabtoDevice:
        """A device on a connection of its own; `reconnect` and `kwargs` go to `MicroNabtoConnection`."""
        return cls(MicroNabtoConnection(email, host=host, device_id=device_id, reconnect=reconnect, **kwargs),
                   owns_connection=True, reread_zeros_for=reread_zeros_for, zeros=zeros)

    @property
    def options(self) -> MicroNabtoOptions | None:
        """The model's options; None until `configure()`."""
        return self._options

    @property
    def zeros(self) -> RereadZeros:
        """How an answer of only zeros, where values were, is read again: the device's when given,
        else the model's, else `RereadZeros()`."""
        if self._own_zeros is not None:
            return self._own_zeros
        if self._options is not None:
            of_model = _zeros(self._options.zeros, self._options.reread_zeros_for)
            if of_model is not None:
                return of_model
        return RereadZeros()

    @property
    def reread_zeros_for(self) -> float:
        """Seconds after an answer of only zeros until the zeros are taken."""
        return self.zeros.within

    async def connect(self) -> Identity | None:
        """Establish the session. The identity is what the device says in the handshake."""
        return await self._connection.open()

    async def disconnect(self) -> None:
        if self._owns_connection:
            await self._connection.close()

    def configure(self, options: ProtocolOptions | None) -> None:
        if options is None:
            self._options: MicroNabtoOptions | None = None
            self._connection.use_model_later_answer_wait(None)
        elif isinstance(options, MicroNabtoOptions):
            self._options = options
            self._connection.use_model_later_answer_wait(options.later_answer_wait)
        else:
            raise TypeError(f"a micro_nabto device needs MicroNabtoOptions, not {type(options).__name__}")

    def _configured(self) -> MicroNabtoOptions:
        if self._options is None:
            raise RuntimeError("configure() the device with its model's options before using it")
        return self._options

    async def read(self, points: Sequence[Point[Any]]) -> Mapping[str, ReadResult]:
        spaces: dict[tuple[type[Access], int], list[Point[Any]]] = {}
        for point in points:
            access = _read_side(point)
            if point.registers > self._configured().max_registers:
                raise ValueError(f"point {point.key!r} spans more registers than max_registers")
            spaces.setdefault((type(access), _obj(access)), []).append(point)

        batches = [batch for (space, obj), members in spaces.items() for batch in self._batches(space, obj, members)]
        self._this_read = [len(batch.items) for batch in batches]
        result: dict[str, ReadResult] = {}
        for batch in batches:
            await self._read_batch(batch, result)
        return result

    async def write(self, point: Point[Any], value: EncodedWrite) -> WriteResult:
        """Write once and wait for the device's answer: OK when it took the write, BUSY when it
        answered another status - a Nilan CTS 402 answered 0x63 and 0x85 to writes it took later -
        and ERROR for an answer without a status."""
        access = point.write
        if not isinstance(access, SetpointRegister):
            raise TypeError(f"point {point.key!r}: only a SetpointRegister can be written over micro_nabto")
        if value.bit_index is not None:
            raise ValueError(f"point {point.key!r}: a single bit cannot be written over micro_nabto")
        items = [(access.obj, access.address + i, register) for i, register in enumerate(value.registers)]
        answer = await self._connection.request(wire.setpoint_write(items))
        status = wire.write_status(answer) if answer is not None else None
        self._recent.append((self._connection.clock.monotonic(), f"SetpointRegister write of {len(items)}: " + (
            "no answer" if answer is None else "no status" if status is None else f"status 0x{status:02x}")))
        if answer is None:
            return self._written(WriteResult(Outcome.NO_ANSWER, detail="no answer from the device"))
        if status is None:
            return self._written(WriteResult(Outcome.ERROR, detail="an answer without a status"))
        if status == 0:
            return self._written(WriteResult(Outcome.OK))
        return self._written(WriteResult(Outcome.BUSY, exception_code=status,
                                         detail=f"the device answered with status 0x{status:02x}"))

    def _written(self, result: WriteResult) -> WriteResult:
        self._outcomes[result.outcome] += 1
        return result

    def diagnostics(self) -> Mapping[str, object]:
        """Counters for a bug report; never an address, a device id or the email."""
        return {
            "outcomes": {outcome.name: self._outcomes[outcome] for outcome in Outcome},
            "isolated_reads": self._isolated,
            "answers_of_only_zeros": self._only_zeros,
            "reread_zeros_for": self.reread_zeros_for,
            "zero_rereads": self._zero_rereads,
            "zeros_ended": {ending: self._endings[ending] for ending in _ENDINGS},
            "zeros_open": len({id(episode) for episode in self._episodes.values()}),
            "longest_until_values": self._longest_until_values,
            "largest_read_with_values": self._largest_with_values,
            "smallest_read_of_zeros": self._smallest_of_zeros,
            **self._connection.diagnostics(),
        }

    # ---------------------------------------------------------------------- reading

    def _batches(self, space: type[Access], obj: int, points: list[Point[Any]]) -> list[_Batch]:
        batches: list[_Batch] = []
        current: list[Point[Any]] = []
        registers = 0
        for point in points:
            if registers + point.registers > self._configured().max_registers:
                batches.append(_Batch(space, obj, tuple(current)))
                current, registers = [], 0
            current.append(point)
            registers += point.registers
        batches.append(_Batch(space, obj, tuple(current)))
        return batches

    def _only_zeros_where_values_were(self, registers: Sequence[_Register], values: Sequence[int]) -> bool:
        """Whether `values` are all 0, of more than one register, where those last held something else:
        a Nilan gateway was reported answering a whole read so now and then."""
        return len(values) > 1 and self._zeros_where_values_were(registers, values)

    def _zeros_where_values_were(self, registers: Sequence[_Register], values: Sequence[int]) -> bool:
        return not any(values) and any(self._last.get(r, 0) for r in registers)

    async def _request(self, space: type[Access], items: Sequence[tuple[int, int]],
                       accept: Callable[[bytes], bool] | None = None) -> tuple[bool, list[int] | None]:
        """Read `items` with one request: whether the device answered, and the values it answered
        with, if the answer could be read. The request is kept for a later warning."""
        build, parse = _READS[space]
        answer = await self._connection.request(build(items), accept=accept)
        values = parse(answer) if answer is not None else None
        registers = [(space, obj, address) for obj, address in items]
        if answer is None:
            what = "no answer"
        elif values is None:
            what = "a malformed answer"
        elif len(values) != len(items):
            what = "refused"
        elif self._zeros_where_values_were(registers, values):
            what = "only zeros"
            self._smallest_of_zeros = min(self._smallest_of_zeros or len(items), len(items))
        else:
            what = "values"
            if any(values):
                self._largest_with_values = max(self._largest_with_values, len(items))
        self._recent.append((self._connection.clock.monotonic(), f"{space.__name__} read of {len(items)}: {what}"))
        return answer is not None, values

    async def _checked(self, batch: _Batch, items: Sequence[tuple[int, int]],
                       values: list[int]) -> tuple[list[int], float | None]:
        """The values to take for an answer, or the seconds after which to read it again.

        Registers answered with only zeros, where they held values, are read again as `zeros`
        says: at once, then, while the zeros are not taken, the seconds until the next reading
        again are returned, and values are taken whenever a reading brings them. At its `within`
        after the zeros the registers are read in two halves, and their values taken, or else the
        zeros.
        """
        space = batch.space
        registers = [(space, obj, address) for obj, address in items]
        span = f"{space.__name__} {items[0][1]} to {items[-1][1]}, {len(items)} registers ({len(batch.points)} points)"
        exchange = self._connection.last_exchange
        episode = next((self._episodes[r] for r in registers if r in self._episodes), None)
        if exchange is not None and exchange.set_aside:
            self._only_zeros += 1
            _LOGGER.warning("micro_nabto: %s answered only zeros to a request sent %d times, then values "
                            "to it, which are taken; %s", span, exchange.sends, self._last_exchange())
            if episode is not None:
                self._end(episode, "values_after_waiting")
            return values, None
        now = self._connection.clock.monotonic()
        rule = self.zeros
        # While the device is known to answer zeros, one register read alone is no exception.
        zeros = (self._zeros_where_values_were(registers, values) if episode is not None
                 else self._only_zeros_where_values_were(registers, values))
        if not zeros:
            if episode is not None and any(values):
                took = self._end(episode, "values_after_waiting")
                _LOGGER.warning("micro_nabto: %s answered values %.1f s after the zeros of %s; before them: %s; %s",
                                span, took, episode.span, ", ".join(episode.readings), self._last_exchange())
            return values, None
        if episode is None and not rule.at_once:
            self._only_zeros += 1
            episode = _Episode(span, exchange.at if exchange is not None else now)
            episode.readings.append("0.0 s only zeros")
            _LOGGER.warning("micro_nabto: %s answered only zeros, where they held values; addresses %s; "
                            "this read: %s; before it: %s; %s; %s", span, _addresses(items),
                            _requests(self._this_read), self._history(now), _library(), self._last_exchange())
        elif episode is None:
            self._only_zeros += 1
            episode = _Episode(span, exchange.at if exchange is not None else now)
            _LOGGER.warning("micro_nabto: %s answered only zeros, where they held values; reading them again "
                            "at once; addresses %s; this read: %s; before it: %s; %s; %s",
                            span, _addresses(items), _requests(self._this_read), self._history(now),
                            _library(), self._last_exchange())
            answered, again = await self._request(space, items)
            self._zero_rereads += 1
            if again is not None and len(again) == len(items) and any(again):
                self._end(episode, "values_at_once")
                _LOGGER.warning("micro_nabto: %s read again at once: values, which are taken; %s",
                                span, self._last_exchange())
                return again, None
            now = self._connection.clock.monotonic()
            episode.readings.append(f"{now - episode.started:.1f} s {'only zeros' if answered else 'no answer'}")
            if rule.within > now - episode.started:
                _LOGGER.warning("micro_nabto: %s read again at once: %s; the last values are kept, and the "
                                "registers read again for up to %.1f s after the zeros",
                                span, "only zeros" if answered else "no answer", rule.within)
        else:
            episode.readings.append(f"{now - episode.started:.1f} s only zeros")
        for register in registers:
            self._episodes[register] = episode
        elapsed = now - episode.started
        if elapsed < rule.within:
            return values, _next_retry(rule.wait, elapsed, rule.within)
        if not rule.halves:
            took = self._end(episode, "zeros_taken")
            _LOGGER.warning("micro_nabto: %s answered only zeros for %.1f s (%s): the zeros are taken; %s",
                            span, took, ", ".join(episode.readings), self._last_exchange())
            return values, None
        return await self._last_try(episode, space, span, items, registers, values), None

    async def _last_try(self, episode: _Episode, space: type[Access], span: str, items: Sequence[tuple[int, int]],
                        registers: Sequence[_Register], zeros: list[int]) -> list[int]:
        """Read `items` in two halves, which a device that cannot answer so many registers at once
        answers: their values, or else `zeros`."""
        middle = len(items) // 2
        found: list[int] = []
        what = "values"
        for start, end in ((0, middle), (middle, len(items))):
            answered, half = await self._request(space, items[start:end])
            self._zero_rereads += 1
            if half is None or len(half) != end - start:
                what = "no answer" if not answered else "an answer that does not fit"
                break
            if self._zeros_where_values_were(registers[start:end], half):
                what = "only zeros"
                break
            found += half
        if what == "values":
            took = self._end(episode, "values_in_halves")
            _LOGGER.warning("micro_nabto: %s answered only zeros for %.1f s (%s); read in two halves of %d and %d "
                            "registers, it answered values, which are taken: %d registers in one request are "
                            "too many for this device; %s", span, took, ", ".join(episode.readings), middle,
                            len(items) - middle, len(items), self._last_exchange())
            return found
        took = self._end(episode, "zeros_taken")
        _LOGGER.warning("micro_nabto: %s answered only zeros for %.1f s (%s), and read in two halves %s: the zeros "
                        "are taken; %s", span, took, ", ".join(episode.readings), what, self._last_exchange())
        return zeros

    def _end(self, episode: _Episode, ending: str) -> float:
        """Close `episode` with `ending`; the seconds since its zeros."""
        for register in [r for r, open_ in self._episodes.items() if open_ is episode]:
            del self._episodes[register]
        self._endings[ending] += 1
        took = self._connection.clock.monotonic() - episode.started
        if ending != "zeros_taken":
            self._longest_until_values = max(self._longest_until_values, took)
        return took

    def _history(self, now: float) -> str:
        """The requests before the last, the oldest first, by how long ago each was."""
        earlier = list(self._recent)[:-1]
        return "; ".join(f"{now - at:.1f} s ago {what}" for at, what in earlier) or "nothing in this session"

    def _note_values(self, space: type[Access], items: Sequence[tuple[int, int]], values: Sequence[int]) -> None:
        """Remember the values taken, and log when registers that were taken as only zeros hold
        values again, with how long the zeros lasted."""
        registers = [(space, obj, address) for obj, address in items]
        exchange = self._connection.last_exchange
        at = exchange.at if exchange is not None else 0.0
        zeroed = [self._zeroed_at[r] for r in registers if r in self._zeroed_at]
        if self._only_zeros_where_values_were(registers, values):
            self._zeroed_at.update((r, at) for r in registers)
        elif zeroed and any(values):
            for register in registers:
                self._zeroed_at.pop(register, None)
            _LOGGER.warning("micro_nabto: %s %d to %d answered values again, %.1f s after only zeros; %s",
                            space.__name__, items[0][1], items[-1][1], at - min(zeroed), self._last_exchange())
        elif _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("micro_nabto: %s %d to %d answered; %s",
                          space.__name__, items[0][1], items[-1][1], self._last_exchange())
        self._last.update(zip(registers, values))

    def _last_exchange(self) -> str:
        exchange = self._connection.last_exchange
        return describe(exchange) if exchange is not None else "no exchange recorded"

    async def _read_batch(self, batch: _Batch, result: dict[str, ReadResult]) -> None:
        _, parse = _READS[batch.space]
        items = batch.items
        registers = [(batch.space, obj, address) for obj, address in items]

        def worth_taking(payload: bytes) -> bool:
            found = parse(payload)
            return found is None or not self._only_zeros_where_values_were(registers, found)

        answered, values = await self._request(batch.space, items, accept=worth_taking)
        if not answered:
            raw = ReadResult(Outcome.NO_ANSWER, detail="no answer from the device")
        elif values is None:
            raw = ReadResult(Outcome.ERROR, detail="a malformed answer")
        elif len(values) == len(items):
            values, retry = await self._checked(batch, items, values)
            if retry is not None:
                self._outcomes[Outcome.BUSY] += 1
                detail = f"answered only zeros; read again in {retry:.1f} s"
                for point in batch.points:
                    result[point.key] = ReadResult(Outcome.BUSY, detail=detail, retry_after=retry)
                return
            self._note_values(batch.space, items, values)
            self._outcomes[Outcome.OK] += 1
            offset = 0
            for point in batch.points:
                result[point.key] = ReadResult(Outcome.OK, tuple(values[offset:offset + point.registers]))
                offset += point.registers
            return
        else:
            raw = ReadResult(Outcome.MISSING, detail=f"{batch.space.__name__} {items[0][1]}: refused by the device")
        self._outcomes[raw.outcome] += 1
        if raw.outcome is Outcome.MISSING and len(batch.points) > 1:
            # A Nilan CTS 402 refuses a whole request for one absent address. Halving it until
            # the refusal is pinned down finds a few absent addresses among many in few requests.
            self._isolated += 1
            middle = len(batch.points) // 2
            for half in (batch.points[:middle], batch.points[middle:]):
                await self._read_batch(_Batch(batch.space, batch.obj, half), result)
            return
        for point in batch.points:
            result[point.key] = raw


def _zeros(zeros: RereadZeros | None, within: float | None) -> RereadZeros | None:
    """`zeros`, or `RereadZeros()` where only `within` is given, taking `within` when given."""
    if within is None:
        return zeros
    return replace(zeros or RereadZeros(), within=within)


def _next_retry(wait: Wait, elapsed: float, until: float) -> float:
    """Seconds from `elapsed` after the zeros until the next reading again: the times `wait` gives,
    counted from the zeros, and last at `until`."""
    tries = 1
    step = wait.delay(tries)
    while step <= elapsed and step < until:
        tries += 1
        gap = wait.delay(tries)
        if gap <= 0:
            return 0.0
        step += gap
    return max(0.0, min(step, until) - elapsed)


def _addresses(items: Sequence[tuple[int, int]]) -> str:
    return ", ".join(f"{obj}:{address}" if obj else str(address) for obj, address in items)


def _requests(sizes: Sequence[int]) -> str:
    return f"{len(sizes)} request(s) of {', '.join(map(str, sizes))} registers" if sizes else "none"


def _library() -> str:
    try:
        return f"modbus_event_connect {metadata.version('modbus_event_connect')}"
    except metadata.PackageNotFoundError:
        return "modbus_event_connect, version unknown"


def _read_side(point: Point[Any]) -> Access:
    access = point.read
    if access is None:
        raise TypeError(f"point {point.key!r} has no read side")
    if type(access) not in _READS:
        raise TypeError(f"point {point.key!r}: the read side is {type(access).__name__}, not a micro_nabto space")
    return access


def _obj(access: Access) -> int:
    return access.obj if isinstance(access, DatapointRegister | SetpointRegister) else 0
