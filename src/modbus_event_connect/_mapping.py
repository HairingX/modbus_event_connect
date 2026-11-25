"""Mapping a unit: which registers of each address space it holds, found and read as raw words."""
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

_ABSENT = frozenset({Outcome.MISSING, Outcome.UNSUPPORTED})
"""Answers that say the unit has nothing at an address."""

ZERO_REREADS = 30
"""The most times a request answered with only zeros, where values were, is read again. A device
takes such zeros as its model's rule says, after a few readings again; this only keeps one that
never does from holding the mapping."""

Read = Callable[[Sequence[Point[Any]]], Awaitable[Mapping[str, ReadResult]]]
"""Reads points through the device, as the client does."""

Sleep = Callable[[float], Awaitable[None]]
"""Waits so many seconds, on the client's clock."""


@dataclass(frozen=True)
class _Space:
    """One address space a model reads or writes: an access in it, and the highest address its
    points read or write."""
    access: Access
    high: int

    def point(self, number: int) -> Point[Any]:
        """A point reading the one raw register, or bit, at `number`."""
        access = dataclasses.replace(self.access, address=number)
        key = f"{access.space}_{number}"
        if access.bits:
            return Point(Key(key, bool), read=access, data_type=DataType.BOOL, poll_rate=PollRate.STATIC)
        return Point(Key(key, int), read=access, poll_rate=PollRate.STATIC)


def _spaces(model: Model) -> dict[str, _Space]:
    """Each space the model's points read or write, whatever the unit and the level, by name, in
    the order they first appear."""
    found: dict[str, _Space] = {}
    for point in declared_points(model):
        for access in (point.read, point.write):
            if access is None:
                continue
            last = access.address + (1 if access.bits else point.registers) - 1
            known = found.get(access.space)
            found[access.space] = _Space(access, last) if known is None else _Space(
                known.access, max(known.high, last))
    return found


def areas(model: Model, limit: int) -> tuple[Area, ...]:
    """For each space `model` reads or writes, every address from 0 up to `limit` - 1, or up to the
    highest address its points read or write where that is higher.

    Raises:
        ValueError: `limit` is below 1.
    """
    if limit < 1:
        raise ValueError(f"the limit is at least 1, got {limit}")
    return tuple(Area(name, 0, max(limit, space.high + 1)) for name, space in _spaces(model).items())


class Mapper:
    """Reads registers of one model's spaces as raw words, through `read`, stopping at the first
    request the unit answers busy, leaves unanswered, or answers so that it cannot be read."""

    def __init__(self, model: Model, read: Read, sleep: Sleep) -> None:
        self._model = model
        self._read = read
        self._sleep = sleep
        self._spaces = _spaces(model)
        self.words: dict[RawAddress, int] = {}
        """Every word read."""
        self.missing: set[RawAddress] = set()
        """Every address the unit said it has nothing at."""

    async def map(self, limit: int) -> None:
        """Read every address of `areas(model, limit)`: each ends with a word or as missing.

        Raises:
            CannotConnectError: a request was answered busy, left unanswered, or could not be read.
        """
        for area in areas(self._model, limit):
            await self._gallop(area.space, list(range(area.start, area.end)))

    async def reread(self, addresses: Iterable[RawAddress]) -> None:
        """Read `addresses` again, as many in one request as the model reads.

        Raises:
            ValueError: an address of a space the model does not read.
            CannotConnectError: a request was answered busy, left unanswered, or could not be read.
        """
        by_space: dict[str, list[int]] = {}
        for address in addresses:
            if address.space not in self._spaces:
                raise ValueError(f"the {self._model.name} model reads no space {address.space!r}")
            by_space.setdefault(address.space, []).append(address.number)
        for name, numbers in by_space.items():
            size = self._limit(name)
            ordered = sorted(numbers)
            for start in range(0, len(ordered), size):
                await self._ask(name, ordered[start:start + size])

    async def _gallop(self, space: str, numbers: Sequence[int]) -> None:
        """Read `numbers` in requests as large as the model reads; after a request that held an
        absent address, from one address, doubling while every address answers, so that a run
        of absent addresses costs one request each rather than the two a halving pays for each."""
        largest = self._limit(space)
        size, start = largest, 0
        while start < len(numbers):
            part = numbers[start:start + size]
            start += len(part)
            size = min(size * 2, largest) if await self._ask(space, part) else 1

    async def _ask(self, space: str, numbers: Sequence[int]) -> bool:
        """Read `numbers` of `space` with the device's own request; whether every one answered.

        Registers answered with only zeros where they held values are read again when the device
        says, until it takes the zeros or values come, so that zeros the user's change brought
        are taken and those of a gateway that holds nothing yet are not.

        Raises:
            CannotConnectError: the unit answered busy, did not answer, or answered so that the
                answer could not be read.
        """
        maker = self._spaces[space]
        points = [maker.point(number) for number in numbers]
        answers = await self._read(points)
        for _ in range(ZERO_REREADS):
            waits = [a.retry_after for a in answers.values() if a.outcome is Outcome.BUSY and a.retry_after is not None]
            if not waits:
                break
            await self._sleep(max(waits))
            answers = await self._read(points)
        every = True
        for number, point in zip(numbers, points):
            answer = answers[point.key]
            address = RawAddress(space, number)
            if answer.outcome is Outcome.OK:
                self.words[address] = answer.registers[0]
            elif answer.outcome in _ABSENT:
                self.missing.add(address)
                every = False
            else:
                detail = f", {answer.detail}" if answer.detail else ""
                raise CannotConnectError(f"reading {space} {numbers[0]}-{numbers[-1]} stopped at {number}: "
                                         f"{answer.outcome.name.lower()}{detail}")
        return every

    def _limit(self, space: str) -> int:
        return self._model.read_limit(self._spaces[space].access)
