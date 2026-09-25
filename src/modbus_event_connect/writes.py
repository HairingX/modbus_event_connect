"""The writes to one device: sent one at a time and in order."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import conversion
from .device import Device, EncodedWrite, Outcome
from .point import Point, WriteKind
from .value import Value

_LOGGER = logging.getLogger(__name__)


@dataclass(eq=False)
class _Write:
    """One queued write. A `superseded` write is never sent; its `result` is its successor's."""
    point: Point
    value: Value
    result: asyncio.Future[bool] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    superseded: bool = False
    sending: bool = False
    returns_to_idle: bool = False
    followers: list[asyncio.Future[bool]] = field(default_factory=list[asyncio.Future[bool]])
    """Results of the writes this one superseded."""


class WriteQueue:
    """The writes to one device, sent one at a time and in order.

    A queued setting overtaken by a newer one is never sent, and returns the newer one's outcome;
    a command is always sent. A pulse writes its idle value back after its time.
    """

    def __init__(self, device: Device, *, answered: Callable[[bool], None],
                 written: Callable[[Point], None], pending: Callable[[bool], None]) -> None:
        """Args:
            answered: told after every write whether the device answered it.
            written: told after every accepted write, to read it back.
            pending: told when the first write is queued and when the last one has finished.
        """
        self._device = device
        self._answered = answered
        self._written = written
        self._pending = pending
        self._lock = asyncio.Lock()
        self._latest_setting: dict[str, _Write] = {}
        self._in_flight = 0
        self._pulses: set[asyncio.Task[None]] = set()

    async def write(self, point: Point, value: Value) -> bool:
        """Write `value`, already checked against `point`; returns whether the device accepted it."""
        return await self._enqueue(_Write(point, value))

    async def write_sequence(self, writes: Sequence[tuple[Point, EncodedWrite]]) -> bool:
        """Send `writes` in order as one operation, stopping at the first refusal."""
        self._begin()
        try:
            async with self._lock:
                for point, value in writes:
                    result = await self._device.write(point, value)
                    self._answered(result.outcome is not Outcome.NO_ANSWER)
                    if not result.ok:
                        _LOGGER.warning("write sequence stopped at '%s': %s", point.key, result.outcome.name)
                        return False
                    self._written(point)
                return True
        finally:
            self._end()

    def cancel_pulses(self) -> None:
        for task in list(self._pulses):
            task.cancel()
        self._pulses.clear()

    async def _enqueue(self, write: _Write) -> bool:
        key = write.point.key
        if write.point.write_kind is WriteKind.STATE:
            earlier = self._latest_setting.get(key)
            if earlier is not None and not earlier.sending and not earlier.superseded:
                earlier.superseded = True
                write.followers.append(earlier.result)
                write.followers.extend(earlier.followers)
                earlier.followers.clear()
            self._latest_setting[key] = write
        self._begin()
        try:
            async with self._lock:
                if not write.superseded:
                    write.sending = True
                    try:
                        ok = await self._send(write)
                    except BaseException as err:
                        for future in write.followers:
                            if not future.done():
                                future.set_exception(err)
                        raise
                    for future in write.followers:
                        if not future.done():
                            future.set_result(ok)
                    return ok
            # Waited for outside the lock: the write that overtook this one needs the lock.
            return await write.result
        finally:
            if self._latest_setting.get(key) is write:
                del self._latest_setting[key]
            self._end()

    async def _send(self, write: _Write) -> bool:
        point = write.point
        result = await self._device.write(point, conversion.encode(point, write.value))
        self._answered(result.outcome is not Outcome.NO_ANSWER)
        if not result.ok:
            _LOGGER.warning("'%s' was not written: %s %s", point.key, result.outcome.name, result.detail)
            return False
        self._written(point)
        if point.pulse is not None and not write.returns_to_idle:
            self._start_pulse(point)
        return True

    def _start_pulse(self, point: Point) -> None:
        pulse = point.pulse
        assert pulse is not None

        async def back_to_idle() -> None:
            await asyncio.sleep(pulse.after)
            await self._enqueue(_Write(point, pulse.idle, returns_to_idle=True))
        task = asyncio.get_running_loop().create_task(back_to_idle())
        self._pulses.add(task)
        task.add_done_callback(self._pulses.discard)

    def _begin(self) -> None:
        self._in_flight += 1
        if self._in_flight == 1:
            self._pending(True)

    def _end(self) -> None:
        self._in_flight -= 1
        if self._in_flight == 0:
            self._pending(False)
