"""The writes to one device: sent one at a time and in order."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ._clock import Clock
from ._conversion import encode
from ._device import Device, EncodedWrite, Outcome, WriteResult
from ._key import Key
from ._point import Point, WriteKind

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Write[T]:
    """One write of a sequence: `value` to the point `key` names."""
    key: Key[T]
    value: T


@dataclass(eq=False)
class _Write:
    """One queued write. A `superseded` write is not sent again; its `result` is its successor's."""
    point: Point[Any]
    value: object
    result: asyncio.Future[bool] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    superseded: bool = False
    returns_to_idle: bool = False
    followers: list[asyncio.Future[bool]] = field(default_factory=list[asyncio.Future[bool]])
    """Results of the writes this one superseded."""


class WriteQueue:
    """The writes to one device, sent one at a time and in order.

    A write the device answers `BUSY` is sent again until it is taken or `retry_for` seconds have
    passed. A setting overtaken by a newer one before the device took it is not sent again, and
    returns the newer one's outcome; a command is always sent. A pulse writes its idle value back
    after its time.
    """

    def __init__(self, device: Device, *, clock: Clock, sleep: Callable[[float], Awaitable[None]],
                 retry_for: float, retry_pause: float, answered: Callable[[bool], None],
                 written: Callable[[Point[Any], object], None], pending: Callable[[bool], None]) -> None:
        """Args:
            retry_for: seconds after its first sending that a write is no longer sent again.
            retry_pause: seconds between sendings of a write the device answered `BUSY`.
            answered: told after every sending whether the device answered it.
            written: told the point and the value of every accepted write.
            pending: told when the first write is queued and when the last one has finished.
        """
        if retry_for < 0 or retry_pause < 0:
            raise ValueError("retry_for and retry_pause cannot be negative")
        self._device = device
        self._clock = clock
        self._sleep = sleep
        self._retry_for = retry_for
        self._retry_pause = retry_pause
        self._answered = answered
        self._written = written
        self._pending = pending
        self._lock = asyncio.Lock()
        self._latest_setting: dict[str, _Write] = {}
        self._in_flight = 0
        self._writing: Counter[str] = Counter()
        self._changes: Counter[str] = Counter()
        self._pulses: set[asyncio.Task[None]] = set()

    async def write(self, point: Point[Any], value: object) -> bool:
        """Write `value`, already checked against `point`; returns whether the device accepted it."""
        return await self._enqueue(_Write(point, value))

    async def write_sequence(self, writes: Sequence[tuple[Point[Any], object]]) -> bool:
        """Send `writes`, already checked, in order as one operation, stopping at the first refusal."""
        keys = [point.key for point, _ in writes]
        self._begin(keys)
        try:
            async with self._lock:
                for point, value in writes:
                    result = await self._until_taken(point, encode(point, value), lambda: False)
                    assert result is not None
                    if not result.ok:
                        _LOGGER.warning("write sequence stopped at '%s': %s", point.key, result.outcome.name)
                        return False
                    self._written(point, value)
                return True
        finally:
            self._end(keys)

    def cancel_pulses(self) -> None:
        for task in list(self._pulses):
            task.cancel()
        self._pulses.clear()

    def marks(self) -> Mapping[str, int]:
        """Where the writes stand now, for `disturbed()` to compare against later."""
        return dict(self._changes)

    def disturbed(self, key: str, since: Mapping[str, int]) -> bool:
        """Whether a write to `key` is under way, or one has begun or ended, since `since` was taken."""
        return self._writing[key] > 0 or self._changes[key] != since.get(key, 0)

    async def _enqueue(self, write: _Write) -> bool:
        key = write.point.key
        if write.point.write_kind is WriteKind.STATE:
            earlier = self._latest_setting.get(key)
            if earlier is not None and not earlier.superseded:
                earlier.superseded = True
                write.followers.append(earlier.result)
                write.followers.extend(earlier.followers)
                earlier.followers.clear()
            self._latest_setting[key] = write
        self._begin([key])
        try:
            async with self._lock:
                if not write.superseded:
                    try:
                        ok = await self._send(write)
                    except BaseException as err:
                        for future in write.followers:
                            if not future.done():
                                future.set_exception(err)
                        raise
                    if ok is not None:
                        for future in write.followers:
                            if not future.done():
                                future.set_result(ok)
                        return ok
            # Waited for outside the lock: the write that overtook this one needs the lock.
            return await write.result
        finally:
            # A write overtaken while it was sent ends by itself; the newer one must not answer it.
            if not write.result.done():
                write.result.cancel()
            if self._latest_setting.get(key) is write:
                del self._latest_setting[key]
            self._end([key])

    async def _send(self, write: _Write) -> bool | None:
        """Whether the device took `write`; None when a newer setting overtook it first."""
        point = write.point
        result = await self._until_taken(point, encode(point, write.value), lambda: write.superseded)
        if result is None:
            return None
        if not result.ok:
            _LOGGER.warning("'%s' was not written: %s %s", point.key, result.outcome.name, result.detail)
            return False
        self._written(point, write.value)
        if point.pulse is not None and not write.returns_to_idle:
            self._start_pulse(point)
        return True

    async def _until_taken(self, point: Point[Any], value: EncodedWrite,
                           overtaken: Callable[[], bool]) -> WriteResult | None:
        """Write, and send again while the device answers `BUSY`; None once `overtaken()`."""
        started = self._clock.monotonic()
        while True:
            result = await self._device.write(point, value)
            self._answered(result.outcome is not Outcome.NO_ANSWER)
            waited = self._clock.monotonic() - started
            if result.outcome is not Outcome.BUSY or waited >= self._retry_for:
                return result
            _LOGGER.debug("'%s' was not taken %.1f s after it was first sent: %s", point.key, waited, result.detail)
            await self._sleep(self._retry_pause)
            if overtaken():
                return None

    def _start_pulse(self, point: Point[Any]) -> None:
        pulse = point.pulse
        assert pulse is not None

        async def back_to_idle() -> None:
            await asyncio.sleep(pulse.after)
            await self._enqueue(_Write(point, pulse.idle, returns_to_idle=True))
        task = asyncio.get_running_loop().create_task(back_to_idle())
        self._pulses.add(task)
        task.add_done_callback(self._pulses.discard)

    def _begin(self, keys: Sequence[str]) -> None:
        for key in keys:
            self._writing[key] += 1
            self._changes[key] += 1
        self._in_flight += 1
        if self._in_flight == 1:
            self._pending(True)

    def _end(self, keys: Sequence[str]) -> None:
        for key in keys:
            self._writing[key] -= 1
            self._changes[key] += 1
        self._in_flight -= 1
        if self._in_flight == 0:
            self._pending(False)
