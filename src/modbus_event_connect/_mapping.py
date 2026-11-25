"""Mapping a unit: which registers of each table it holds, found and read as raw words."""
from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from event_connect import Area, CannotConnectError, Key, PollRate, RawAddress

from ._data_type import DataType
from ._device import Outcome, ReadResult
from ._model import Model, declared_points
from ._point import Access, Point

PROBE_STRIDE = 10
"""Above the addresses a model reads, one address in this many is asked for alone: registers lie
in runs, and the documented gaps between runs are wider than this."""

_ABSENT = frozenset({Outcome.MISSING, Outcome.UNSUPPORTED})
"""Answers that say the unit has nothing at an address."""

Read = Callable[[Sequence[Point[Any]]], Awaitable[Mapping[str, ReadResult]]]
"""Reads points through the device, as the client does."""


@dataclass(frozen=True)
class _Table:
    """One table a model reads: an access in it, and the lowest and highest address its points read."""
    access: Access
    low: int
    high: int

    def point(self, number: int) -> Point[Any]:
        """A point reading the one raw register, or bit, at `number`."""
        access = dataclasses.replace(self.access, address=number)
        if access.bits:
            return Point(Key(f"{access.table}_{number}", bool), read=access, data_type=DataType.BOOL,
                         poll_rate=PollRate.STATIC)
        return Point(Key(f"{access.table}_{number}", int), read=access, poll_rate=PollRate.STATIC)


def _tables(model: Model) -> dict[str, _Table]:
    """Each table the model's points read or write, whatever the unit and the level, by name, in
    the order they first appear."""
    found: dict[str, _Table] = {}
    for point in declared_points(model):
        for access in (point.read, point.write):
            if access is None:
                continue
            last = access.address + (1 if access.bits else point.registers) - 1
            known = found.get(access.table)
            found[access.table] = _Table(access, access.address, last) if known is None else _Table(
                known.access, min(known.low, access.address), max(known.high, last))
    return found


def areas(model: Model, limit: int) -> tuple[Area, ...]:
    """For each table `model` reads or writes: from the lowest to the highest address its points
    read or write, the gaps included; and above that, up to address `limit` - 1, where that is higher.

    Raises:
        ValueError: `limit` is below 1.
    """
    if limit < 1:
        raise ValueError(f"the limit is at least 1, got {limit}")
    found: list[Area] = []
    for name, table in _tables(model).items():
        found.append(Area(name, table.low, table.high - table.low + 1))
        if limit - 1 > table.high:
            found.append(Area(name, table.high + 1, limit - 1 - table.high))
    return tuple(found)


class Mapper:
    """Reads registers of one model's tables as raw words, through `read`, stopping at the first
    request the unit answers busy, leaves unanswered, or answers so that it cannot be read."""

    def __init__(self, model: Model, read: Read) -> None:
        self._model = model
        self._read = read
        self._tables = _tables(model)
        self.words: dict[RawAddress, int] = {}
        """Every word read."""
        self.missing: set[RawAddress] = set()
        """Every address the unit said it has nothing at."""

    async def map(self, limit: int) -> None:
        """Find the registers of `areas(model, limit)`: inside the span the model reads, every one;
        above it, around each probe that answers.

        Raises:
            CannotConnectError: a request was answered busy, left unanswered, or could not be read.
        """
        for area in areas(self._model, limit):
            table = self._tables[area.table]
            numbers = list(range(area.start, area.end))
            if area.start <= table.high:
                await self._gallop(area.table, numbers)
            else:
                await self._above(area.table, numbers)

    async def reread(self, addresses: Iterable[RawAddress]) -> None:
        """Read `addresses` again, as many in one request as the model reads.

        Raises:
            ValueError: an address of a table the model does not read.
            CannotConnectError: a request was answered busy, left unanswered, or could not be read.
        """
        by_table: dict[str, list[int]] = {}
        for address in addresses:
            if address.table not in self._tables:
                raise ValueError(f"the {self._model.name} model reads no table {address.table!r}")
            by_table.setdefault(address.table, []).append(address.number)
        for name, numbers in by_table.items():
            size = self._limit(name)
            ordered = sorted(numbers)
            for start in range(0, len(ordered), size):
                await self._ask(name, ordered[start:start + size])

    async def _gallop(self, table: str, numbers: Sequence[int]) -> None:
        """Read `numbers` in requests as large as the model reads; after a request that held an
        absent address, from one address, doubling while every address answers, so that a run of
        absent addresses costs one request each rather than the two a halving pays for each."""
        largest = self._limit(table)
        size, start = largest, 0
        while start < len(numbers):
            part = numbers[start:start + size]
            start += len(part)
            size = min(size * 2, largest) if await self._ask(table, part) else 1

    async def _above(self, table: str, numbers: Sequence[int]) -> None:
        """Ask for one address in every `PROBE_STRIDE` alone, then read whole the stretches of
        `PROBE_STRIDE` around each that answered: one with an answer, and those beside it, less
        the probes, which are known."""
        stretches = [numbers[at:at + PROBE_STRIDE] for at in range(0, len(numbers), PROBE_STRIDE)]
        answered = [await self._ask(table, stretch[:1]) for stretch in stretches]
        near = [any(answered[max(0, at - 1):at + 2]) for at in range(len(stretches))]
        for at, stretch in enumerate(stretches):
            if near[at]:
                await self._gallop(table, stretch[1:])

    async def _ask(self, table: str, numbers: Sequence[int]) -> bool:
        """Read `numbers` of `table` with the device's own request; whether every one answered.

        Raises:
            CannotConnectError: the unit answered busy, did not answer, or answered so that the
                answer could not be read.
        """
        maker = self._tables[table]
        points = [maker.point(number) for number in numbers]
        answers = await self._read(points)
        every = True
        for number, point in zip(numbers, points):
            answer = answers[point.key]
            address = RawAddress(table, number)
            if answer.outcome is Outcome.OK:
                self.words[address] = answer.registers[0]
            elif answer.outcome in _ABSENT:
                self.missing.add(address)
                every = False
            else:
                raise CannotConnectError(f"reading {table} {numbers[0]}-{numbers[-1]} stopped at {number}: "
                                         f"{answer.outcome.name.lower()}" + (f", {answer.detail}" if answer.detail else ""))
        return every

    def _limit(self, table: str) -> int:
        return self._model.read_limit(self._tables[table].access)
