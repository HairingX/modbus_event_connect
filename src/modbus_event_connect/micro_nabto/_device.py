"""One micro_nabto device implementing `Device`: points in, `ReadResult` / `WriteResult` out."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import _wire as wire
from ._access import DatapointRegister, SetpointRegister
from ._connection import MicroNabtoConnection
from .._data_type import DataTypeKind
from .._device import EncodedWrite, Identity, Outcome, ProtocolOptions, ReadResult, WriteResult
from .._point import Access, Point

MAX_REGISTERS_PER_READ = 64
"""Registers per read request; a CTS 402 answered 108, so this leaves room."""

_MAX_OBJECT = 0xFF
_MAX_ADDRESS: Mapping[tuple[str, type[Access]], int] = {
    ("read", DatapointRegister): 0xFFFF_FFFF,
    ("read", SetpointRegister): 0xFFFF,
    ("write", SetpointRegister): 0xFFFF_FFFF,
}
"""SetpointRegister reads carry a two-byte address; datapoint reads and setpoint writes four."""

_Parse = Callable[[bytes], list[int] | None]
_READS: Mapping[type[Access], tuple[Callable[[Sequence[tuple[int, int]]], bytes], _Parse]] = {
    DatapointRegister: (wire.datapoint_read, wire.datapoint_values),
    SetpointRegister: (wire.setpoint_read, wire.setpoint_values),
}


@dataclass(frozen=True)
class MicroNabtoOptions(ProtocolOptions):
    """What a model states about its device in micro_nabto terms."""
    max_registers: int = MAX_REGISTERS_PER_READ
    """Most registers the device is asked for in one read."""

    def __post_init__(self) -> None:
        if self.max_registers < 1:
            raise ValueError(f"max_registers must be at least 1, got {self.max_registers}")

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

    def __init__(self, connection: MicroNabtoConnection, *, owns_connection: bool = False) -> None:
        """Args:
            owns_connection: whether `disconnect()` also closes the connection.
        """
        self._connection = connection
        self._owns_connection = owns_connection
        self._options: MicroNabtoOptions | None = None
        self._outcomes: Counter[Outcome] = Counter()
        self._isolated = 0

    @classmethod
    def udp(cls, email: str, *, host: str | None = None, device_id: str | None = None,
            **kwargs: Any) -> MicroNabtoDevice:
        """A device on a connection of its own; `kwargs` go to `MicroNabtoConnection`."""
        return cls(MicroNabtoConnection(email, host=host, device_id=device_id, **kwargs), owns_connection=True)

    @property
    def options(self) -> MicroNabtoOptions | None:
        """The model's options; None until `configure()`."""
        return self._options

    async def connect(self) -> Identity | None:
        """Establish the session. The identity is what the device says in the handshake."""
        return await self._connection.open()

    async def disconnect(self) -> None:
        if self._owns_connection:
            await self._connection.close()

    def configure(self, options: ProtocolOptions | None) -> None:
        if options is None:
            self._options: MicroNabtoOptions | None = None
        elif isinstance(options, MicroNabtoOptions):
            self._options = options
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

        result: dict[str, ReadResult] = {}
        for (space, obj), members in spaces.items():
            for batch in self._batches(space, obj, members):
                await self._read_batch(batch, result)
        return result

    async def write(self, point: Point[Any], value: EncodedWrite) -> WriteResult:
        """Send the write without waiting for the device to confirm it: OK means sent."""
        access = point.write
        if not isinstance(access, SetpointRegister):
            raise TypeError(f"point {point.key!r}: only a SetpointRegister can be written over micro_nabto")
        if value.bit_index is not None:
            raise ValueError(f"point {point.key!r}: a single bit cannot be written over micro_nabto")
        items = [(access.obj, access.address + i, register) for i, register in enumerate(value.registers)]
        if await self._connection.send(wire.setpoint_write(items)):
            outcome, detail = Outcome.OK, ""
        else:
            outcome, detail = Outcome.NO_ANSWER, "no session with the device"
        self._outcomes[outcome] += 1
        return WriteResult(outcome, detail=detail)

    def diagnostics(self) -> Mapping[str, object]:
        """Counters for a bug report; never an address, a device id or the email."""
        return {
            "outcomes": {outcome.name: self._outcomes[outcome] for outcome in Outcome},
            "isolated_reads": self._isolated,
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

    async def _read_batch(self, batch: _Batch, result: dict[str, ReadResult]) -> None:
        build, parse = _READS[batch.space]
        items = batch.items
        answer = await self._connection.request(build(items))
        values = parse(answer) if answer is not None else None
        if answer is None:
            raw = ReadResult(Outcome.NO_ANSWER, detail="no answer from the device")
        elif values is None:
            raw = ReadResult(Outcome.ERROR, detail="a malformed answer")
        elif len(values) == len(items):
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


def _read_side(point: Point[Any]) -> Access:
    access = point.read
    if access is None:
        raise TypeError(f"point {point.key!r} has no read side")
    if type(access) not in _READS:
        raise TypeError(f"point {point.key!r}: the read side is {type(access).__name__}, not a micro_nabto space")
    return access


def _obj(access: Access) -> int:
    return access.obj if isinstance(access, DatapointRegister | SetpointRegister) else 0
