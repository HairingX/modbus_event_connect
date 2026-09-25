"""Measuring how long a device takes before a read shows what was written to it."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .._client import Client
from .._conversion import encode
from .._key import Key
from .._value import Quality


@dataclass(frozen=True)
class ReadBackTrial:
    """The writes made for one delay, and how many a read after that delay showed."""
    delay: float
    written: int
    """Writes the device accepted, each from a value it was seen to hold."""
    seen: int


@dataclass(frozen=True)
class ReadBackMeasurement:
    """How often a written value was read back, for each delay tried."""
    key: str
    trials: tuple[ReadBackTrial, ...]

    @property
    def recommended(self) -> float | None:
        """The shortest delay from which on every write was read back; None if even the longest missed one."""
        recommended: float | None = None
        for trial in sorted(self.trials, key=lambda t: t.delay, reverse=True):
            if trial.written == 0 or trial.seen < trial.written:
                break
            recommended = trial.delay
        return recommended

    def __str__(self) -> str:
        lines = [f"{self.key}: read back after a write"]
        lines.extend(f"  {trial.delay:>6g} s   {trial.seen} of {trial.written}" for trial in self.trials)
        recommended = self.recommended
        lines.append(f"  read_back_after={recommended:g}" if recommended is not None
                     else "  no delay tried read every write back; try longer ones")
        return "\n".join(lines)


async def measure_read_back[T](client: Client, key: Key[T], values: tuple[T, T], *,
                            delays: Sequence[float] = (1, 2, 3, 4, 5), repeats: int = 3,
                            sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> ReadBackMeasurement:
    """Write to `key` and count, for each delay, how often a read that long after shows the value.

    This writes to the device: choose a point you may change, and two values it may take. Each
    write starts from the other value, written and given the longest delay to show first, so a
    late earlier write cannot pass for a quick one. The value the point had is written back at
    the end.

    Raises:
        ValueError: the point cannot be read and written, has no good value to restore, or the
            values, delays or repeats cannot make a measurement.
        InvalidValueError: the point refuses one of the values, or the one to restore.
        ReadOnlyError: the client is read-only.

    Nothing is written when it raises.
    """
    if not (client.can_read(key) and client.can_write(key)):
        raise ValueError(f"{key!r} must be both readable and writable to measure its read-back")
    if values[0] == values[1]:
        raise ValueError("the two values must differ, or a write could not be told from none")
    if not delays or any(delay < 0 for delay in delays) or repeats < 1:
        raise ValueError("give at least one delay, none negative, and at least one repeat")
    original = client.value(key)
    restore = original.value if original is not None and original.quality is Quality.GOOD else None
    if restore is None:
        raise ValueError(f"{key!r} has no good value to write back afterwards")
    for value in (*values, restore):
        encode(client.points[key], value)
    longest = max(delays)

    async def shows(value: T) -> bool:
        await client.refresh([key])
        current = client.value(key)
        return current is not None and current.quality is Quality.GOOD and current.value == value

    trials: list[ReadBackTrial] = []
    try:
        for delay in delays:
            written = seen = 0
            for attempt in range(repeats):
                start, target = values if attempt % 2 == 0 else (values[1], values[0])
                if not await client.write(key, start):
                    continue
                await sleep(longest)
                if not await shows(start) or not await client.write(key, target):
                    continue
                written += 1
                await sleep(delay)
                if await shows(target):
                    seen += 1
            trials.append(ReadBackTrial(delay, written, seen))
    finally:
        await client.write(key, restore)
    return ReadBackMeasurement(key, tuple(trials))
