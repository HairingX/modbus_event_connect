"""One micro_nabto device implementing `Device`: points in, `ReadResult` / `WriteResult` out; and
how a model is talked to over micro_nabto, its profile."""
from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from event_connect import CircuitBreaker, Clock, Fixed, Wait

from . import _wire as wire
from ._access import DatapointRegister, SetpointRegister
from ._connection import MicroNabtoConnection, describe
from .._data_type import DataTypeKind
from .._device import EncodedWrite, Identity, Outcome, ReadResult, WriteResult
from .._model import Model, Profile, checked_profiles, profile_of
from .._point import Access, Point

_LOGGER = logging.getLogger(__name__)

MAX_REGISTERS_PER_READ = (wire.MAX_DATAGRAM - wire.EMPTY_DATAPOINT_READ) // wire.DATAPOINT_ITEM
"""The most registers one read can ask for: its request, at five bytes a register, fits one UDP
datagram. A device may answer fewer."""

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


@dataclass(frozen=True, kw_only=True)
class RereadZeros:
    """How registers answered with only zeros, where they held values, are read again before the
    zeros are taken; until then their last values are kept."""
    within: float
    """Seconds after the zeros until they are taken."""
    wait: Wait
    """When to read them again: the first time this long after the zeros, each next time this long
    after the time before."""
    at_once: bool
    """Whether they are read again at once, before anything else."""
    halves: bool
    """Whether, at `within`, they are read in two halves, whose values are taken if they bring
    any: a device may answer only zeros when asked for too many registers at once."""

    def __post_init__(self) -> None:
        if not self.within >= 0:
            raise ValueError(f"within cannot be negative, got {self.within}")


TAKE_ZEROS = RereadZeros(within=0.0, wait=Fixed(0.0), at_once=False, halves=False)
"""Zeros are taken as they come: for a device known to answer zeros only where they are."""


@dataclass(frozen=True, kw_only=True)
class MicroNabtoProfile(Profile):
    """How a model is talked to over micro_nabto."""
    max_registers: int
    """Most registers the device is asked for in one read: 1 to `MAX_REGISTERS_PER_READ`."""
    later_answer_wait: float
    """Seconds a read sent again and answered with only zeros, where values were, is kept open
    for a later answer."""
    zeros: RereadZeros
    """How an answer of only zeros, where values were, is read again before the zeros are taken."""

    def __post_init__(self) -> None:
        if not 1 <= self.max_registers <= MAX_REGISTERS_PER_READ:
            raise ValueError(f"max_registers must be 1-{MAX_REGISTERS_PER_READ}, got {self.max_registers}")
        if not self.later_answer_wait >= 0:
            raise ValueError(f"later_answer_wait cannot be negative, got {self.later_answer_wait}")

    def problems(self, point: Point[Any]) -> list[str]:
        """What is wrong with `point` over micro_nabto: a side outside its two spaces or past their
        addresses, a read larger than one request of this device may be, or a bit to write."""
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

    def __init__(self, connection: MicroNabtoConnection, *, profiles: Sequence[MicroNabtoProfile],
                 owns_connection: bool = False) -> None:
        """Args:
            profiles: the profile of every model the device may be; `use()` chooses one.
            owns_connection: whether `disconnect()` also closes the connection.

        Raises:
            ModelError: no profile, or two of one model.
        """
        self._connection = connection
        self._owns_connection = owns_connection
        self._profiles = checked_profiles(profiles)
        self._profile: MicroNabtoProfile | None = None
        self._episodes: dict[_Register, _Episode] = {}
        self._endings: Counter[str] = Counter()
        self._zero_rereads = 0
        self._longest_until_values = 0.0
        self._largest_with_values = 0
        self._smallest_of_zeros: int | None = None
        self._recent: deque[tuple[float, str]] = deque(maxlen=_HISTORY + 1)
        self._this_read: list[int] = []
        self._outcomes: Counter[Outcome] = Counter()
        self._isolated = 0
        self._only_zeros = 0
        self._last: dict[_Register, int] = {}
        self._zeroed_at: dict[_Register, float] = {}

    @classmethod
    def udp(cls, email: str, *, host: str | None = None, device_id: str | None = None,
            reconnect: CircuitBreaker, profiles: Sequence[MicroNabtoProfile], port: int = wire.DEVICE_PORT,
            timeout: float = 1.0, retries: int = 2, answer_wait: float = 5.0, session_idle: float = 12.0,
            discovery_target: tuple[str, int] = wire.BROADCAST, clock: Clock | None = None) -> MicroNabtoDevice:
        """A device on a session of its own, which `disconnect()` closes; everything but
        `profiles` is the session's, as `MicroNabtoConnection` takes it."""
        connection = MicroNabtoConnection(email, host=host, device_id=device_id, reconnect=reconnect, port=port,
                                          timeout=timeout, retries=retries, answer_wait=answer_wait,
                                          session_idle=session_idle, discovery_target=discovery_target,
                                          clock=clock)
        return cls(connection, profiles=profiles, owns_connection=True)

    @property
    def profile(self) -> MicroNabtoProfile | None:
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
    def later_answer_wait(self) -> float:
        """Seconds a read sent again, answered with only zeros, is kept open for a later answer: the
        chosen profile's, and before one is chosen, the longest of any profile."""
        if self._profile is not None:
            return self._profile.later_answer_wait
        return max(profile.later_answer_wait for profile in self._profiles)

    @property
    def zeros(self) -> RereadZeros:
        """How an answer of only zeros, where values were, is read again: the chosen profile's, and
        before one is chosen, that of the profile that takes the zeros latest."""
        if self._profile is not None:
            return self._profile.zeros
        return max((profile.zeros for profile in self._profiles), key=lambda zeros: zeros.within)

    async def connect(self) -> Identity | None:
        """Establish the session. The identity is what the device says in the handshake."""
        return await self._connection.open()

    async def disconnect(self) -> None:
        if self._owns_connection:
            await self._connection.close()

    def use(self, model: Model) -> MicroNabtoProfile:
        """Talk to the device as the profile of `model` says, and return that profile.

        Raises:
            ModelError: the device was given no profile of `model`.
        """
        self._profile = profile_of(self._profiles, model)
        return self._profile

    async def read(self, points: Sequence[Point[Any]]) -> Mapping[str, ReadResult]:
        spaces: dict[tuple[type[Access], int], list[Point[Any]]] = {}
        for point in points:
            access = _read_side(point)
            if point.registers > self.max_registers:
                raise ValueError(f"point {point.key!r} spans more registers than max_registers")
            spaces.setdefault((type(access), access.obj), []).append(point)

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
            "later_answer_wait": self.later_answer_wait,
            "zeros_taken_after": self.zeros.within,
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
            if registers + point.registers > self.max_registers:
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
        answer = await self._connection.request(build(items), accept=accept,
                                                later_answer_wait=self.later_answer_wait)
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
    return f"modbus_event_connect {metadata.version('modbus_event_connect')}"


def _read_side(point: Point[Any]) -> DatapointRegister | SetpointRegister:
    access = point.read
    if access is None:
        raise TypeError(f"point {point.key!r} has no read side")
    if not isinstance(access, DatapointRegister | SetpointRegister):
        raise TypeError(f"point {point.key!r}: the read side is {type(access).__name__}, not a micro_nabto space")
    return access

