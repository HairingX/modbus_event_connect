"""A device as a consumer sees it: the scan, values with quality, events, polling and writes."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum

from . import conversion
from .clock import Clock, SystemClock
from .device import Device, Identity, Outcome, ReadResult
from .errors import CannotConnectError, NotConnectedError, ReadOnlyError, UnsupportedDeviceError
from .events import Subscriptions, ValueCallback, tell
from .model import Model, ModelSelector, ResolvedModel, resolve
from .point import Change, Labels, Point, PollRate, Selector
from .scheduler import Scheduler
from .value import DataValue, Quality, Value
from .writes import WriteQueue

_LOGGER = logging.getLogger(__name__)

class Status(StrEnum):
    """Values the client produces itself. They can be subscribed to like point keys."""
    CONNECTED = "status:connected"
    """True while the device answers."""
    WRITE_PENDING = "status:write_pending"
    """True from the first queued write until the last one has finished."""


_STATUS_KEYS: frozenset[str] = frozenset(status.value for status in Status)
_UNANSWERED = frozenset({Outcome.NO_ANSWER, Outcome.BUSY})


class _ScanAnswers:
    """Tally of the answers received during one scan."""

    def __init__(self) -> None:
        self.asked = 0
        self.unanswered = 0

    def note(self, answers: Iterable[ReadResult]) -> None:
        for answer in answers:
            self.asked += 1
            if answer.outcome in _UNANSWERED:
                self.unanswered += 1

    @property
    def any_answered(self) -> bool:
        return self.asked > self.unanswered

    def require_all_answered(self) -> None:
        """Raise CannotConnectError unless every read so far was answered."""
        if self.unanswered:
            raise CannotConnectError(f"{self.unanswered} of {self.asked} reads during the scan went "
                                     f"unanswered; nothing was changed")


class Client:
    """
    One device, scanned by `connect()` and read by `poll()` on the host's schedule.
    `model` is a model, or a selector choosing one from the device's identity.
    """

    def __init__(self, device: Device, model: Model | ModelSelector, *,
                 clock: Clock | None = None, read_only: bool = False) -> None:
        self._device = device
        self._select_model: ModelSelector = (lambda _identity: model) if isinstance(model, Model) else model
        self._clock: Clock = clock or SystemClock()
        self._read_only = read_only

        self._resolved: ResolvedModel | None = None
        self._scheduler: Scheduler | None = None
        self._values: dict[str, DataValue] = {}
        self._subscriptions = Subscriptions()
        self._polled_keys: set[str] = set()
        self._unavailable: dict[str, str] = {}
        self._interval_overrides: dict[PollRate | str, float | None] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._reported_offline = False
        self._status: dict[Status, DataValue] = {
            status: DataValue(False, Quality.GOOD, self._clock.now()) for status in Status}

        self._poll_lock = asyncio.Lock()
        self._writes = WriteQueue(device, answered=self._update_reachability, written=self._read_back,
                                  pending=lambda pending: self._set_status(Status.WRITE_PENDING, pending))

    # ================================================================================= scan

    async def connect(self) -> None:
        """
        Reach the device, choose its model, find what this unit has, and read every value once.

        Raises:
            CannotConnectError: the device could not be reached, or left a read unanswered.
            UnsupportedDeviceError: no model matches the device.
            ModelError: the chosen model contradicts itself.
        """
        identity = await self._device.connect()
        if identity is None:
            self._update_reachability(False)
            raise CannotConnectError("the device could not be reached")
        try:
            await self._scan(identity)
        except BaseException:
            await self._device.disconnect()
            raise

    async def rescan(self) -> None:
        """
        Find again what this unit has, on the open connection, and read every value once.

        Raises:
            NotConnectedError: the device has not been scanned.
            CannotConnectError: a read went unanswered; nothing was changed.
        """
        await self._scan(dict(self._require_model().identity))

    async def disconnect(self) -> None:
        """Cancel pending pulses and let go of the device."""
        self._writes.cancel_pulses()
        await self._device.disconnect()
        self._reported_offline = False
        self._set_status(Status.CONNECTED, False)

    async def _scan(self, handshake: Identity) -> None:
        """
        Build the model, availability and first values; commit only if every read was answered.
        An unanswered read never reports a register missing, so it cannot be trusted.
        """
        model = self._select_model(handshake)
        if model is None:
            raise UnsupportedDeviceError(f"no model for a device that reports {dict(handshake)!r}")
        self._device.configure(model.options)
        answers = _ScanAnswers()
        try:
            resolved = resolve(model, await self._identify(model, handshake, answers))
            scanned: dict[str, ReadResult] = {}
            unavailable = await self._run_scan_steps(model, resolved, answers, scanned)
            readable = [p for p in resolved.points.values() if p.readable and p.key not in unavailable]
            unread = [p for p in readable if p.key not in scanned]
            first_answers: Mapping[str, ReadResult] = await self._device.read(unread) if unread else {}
            answers.note(first_answers.values())
            answers.require_all_answered()
        except CannotConnectError:
            self._update_reachability(answers.any_answered)
            raise
        self._commit(model, resolved, unavailable)
        self._publish(readable, {**scanned, **first_answers})

    async def _identify(self, model: Model, handshake: Identity, answers: _ScanAnswers) -> Identity:
        """The handshake's identity, with what the model's identity points read added to it."""
        identity: dict[str, int | float | str | bool | None] = dict(handshake)
        if model.identity_points:
            raw = await self._device.read(model.identity_points)
            answers.note(raw.values())
            answers.require_all_answered()
            for point in model.identity_points:
                data = self._data_value(point, raw[point.key], previous=None)
                if data.is_good:
                    identity[point.key] = data.value
        return identity

    async def _run_scan_steps(self, model: Model, resolved: ResolvedModel, answers: _ScanAnswers,
                              scanned: dict[str, ReadResult]) -> dict[str, str]:
        """What the model's scan steps find missing, key by key with the reason.

        Every answer they get is kept in `scanned`, and a point is read at most once.
        """
        unavailable: dict[str, str] = {}

        async def read(targets: Selector | Sequence[str]) -> Mapping[str, DataValue]:
            points = [p for p in _select(resolved, targets) if p.readable]
            unread = [p for p in points if p.key not in scanned]
            raw: Mapping[str, ReadResult] = {}
            if unread:
                raw = await self._device.read(unread)
                answers.note(raw.values())
                scanned.update((key, answer) for key, answer in raw.items() if answer.outcome not in _UNANSWERED)
            return {p.key: self._data_value(p, scanned[p.key] if p.key in scanned else raw[p.key], previous=None)
                    for p in points}

        def mark(targets: Selector | Sequence[str], available: bool, reason: str) -> None:
            _record_availability(unavailable, _select(resolved, targets), available, reason)

        scan = _ScanContext(resolved.identity, read, mark)
        for step in model.scan_steps:
            await step(scan)
        answers.require_all_answered()
        return unavailable

    def _commit(self, model: Model, resolved: ResolvedModel, unavailable: dict[str, str]) -> None:
        scheduler = Scheduler(self._clock, model.poll_intervals, min_poll_interval=model.min_poll_interval)
        scheduler.set_points(resolved.points.values())
        for target, seconds in self._interval_overrides.items():
            if isinstance(target, PollRate) or target in resolved.points:
                scheduler.set_poll_interval(target, seconds)

        self._resolved, self._scheduler, self._unavailable = resolved, scheduler, unavailable
        for key in [k for k in self._values if k not in resolved.points]:
            del self._values[key]
            self._subscriptions.forget(key)
        for key in self._subscriptions.keys():
            if key not in _STATUS_KEYS and key not in resolved.points:
                _LOGGER.warning("'%s' is subscribed to, but the %s model does not have it", key, model.name)
        for key in resolved.points:
            self._update_polling(key)
        self._update_reachability(True)

    # ========================================================================= the picture

    @property
    def model(self) -> Model:
        return self._require_model().model

    @property
    def points(self) -> Mapping[str, Point]:
        """The points this unit has: declared by its model and not recorded as missing."""
        resolved = self._require_model()
        return {key: point for key, point in resolved.points.items() if self._is_available(key)}

    @property
    def keys(self) -> tuple[str, ...]:
        """The keys of `points`."""
        return tuple(self.points)

    def instances(self, label: str) -> tuple[int, ...]:
        """The numbers of the instances of `label` of which this unit has at least one point."""
        resolved = self._require_model()
        return tuple(n for n in resolved.instances.get(label, ())
                     if any(self._is_available(p.key) for p in resolved.select(Labels(**{label: n}))))

    def select(self, targets: Selector | Sequence[str]) -> tuple[Point, ...]:
        """The points `targets` selects, among those this unit has."""
        return tuple(p for p in _select(self._require_model(), targets) if self._is_available(p.key))

    def has(self, key: str) -> bool:
        """Whether this unit has `key`."""
        if key in _STATUS_KEYS:
            return True
        return self._resolved is not None and key in self._resolved.points and self._is_available(key)

    def can_read(self, key: str) -> bool:
        return self.has(key) and self._point(key).readable

    def can_write(self, key: str) -> bool:
        return self.has(key) and self._point(key).writable

    @property
    def unavailable_reasons(self) -> Mapping[str, str]:
        """The keys recorded as missing on this unit, with the reason."""
        return dict(self._unavailable)

    # ============================================================================== values

    def value(self, key: str) -> DataValue | None:
        """The current value of `key`, or None if it was never read."""
        if key in _STATUS_KEYS:
            return self._status[Status(key)]
        return self._values.get(key)

    @property
    def values(self) -> Mapping[str, DataValue]:
        """The current value of each key this unit has, among those read."""
        return {key: value for key, value in self._values.items() if self._is_available(key)}

    @property
    def connected(self) -> bool:
        """Whether the device answered the most recent exchange."""
        return bool(self._status[Status.CONNECTED].value)

    @property
    def write_pending(self) -> bool:
        return bool(self._status[Status.WRITE_PENDING].value)

    def consecutive_failures(self, key: str) -> int:
        """Consecutive failed reads of `key`."""
        return self._consecutive_failures.get(key, 0)

    # ============================================================================ interest

    def subscribe(self, key: str, callback: ValueCallback, *, poll: bool = True) -> Callable[[], None]:
        """
        Call `callback` on every change of `key`'s value or quality, starting with the current
        value. With `poll`, the point is also read on its schedule. Returns the unsubscriber.

        Raises:
            KeyError: after the scan, the model has no such key.
        """
        if self._resolved is not None and key not in self._resolved.points and key not in _STATUS_KEYS:
            raise KeyError(f"the {self._resolved.model.name} model has no point {key!r}")
        subscriber = self._subscriptions.add(key, callback, polls=poll)
        self._update_polling(key)
        current = self.value(key)
        if current is not None:
            tell(subscriber, key, None, current)

        def unsubscribe() -> None:
            self._subscriptions.remove(key, subscriber)
            self._update_polling(key)
        return unsubscribe

    def set_polling(self, key: str, enabled: bool = True) -> None:
        """Poll `key` on its schedule even without a subscriber; False stops that."""
        if enabled:
            self._polled_keys.add(key)
        else:
            self._polled_keys.discard(key)
        self._update_polling(key)

    def set_poll_interval(self, target: PollRate | str, seconds: float | None) -> float | None:
        """
        Override how often a poll rate or a key is read; None restores the model's interval.
        Returns the interval in effect, which is never below the model's floor.
        """
        self._interval_overrides[target] = seconds
        if self._scheduler is None:
            return seconds
        return self._scheduler.set_poll_interval(target, seconds)

    # ======================================================================== availability

    def set_available(self, targets: Selector | Sequence[str] | str, available: bool, *,
                      reason: str = "") -> None:
        """Record whether this unit has the selected points."""
        points = [self._point(targets)] if isinstance(targets, str) else _select(self._require_model(), targets)
        _record_availability(self._unavailable, points, available, reason)
        for point in points:
            self._update_polling(point.key)

    def _is_available(self, key: str) -> bool:
        return key not in self._unavailable

    # ============================================================================= reading

    async def poll(self) -> None:
        """Read whatever is due. A call made during a pass waits for it instead of starting another."""
        if self._poll_lock.locked():
            async with self._poll_lock:
                return
        async with self._poll_lock:
            keys = self._require_scheduler().due()
            if keys:
                resolved = self._require_model()
                await self._read_and_publish([resolved.points[k] for k in keys])

    def seconds_until_next_poll(self) -> float | None:
        """Seconds until `poll()` has something to read, 0 if it has now; None if it never will."""
        due = self._require_scheduler().next_due()
        return None if due is None else max(0.0, due - self._clock.monotonic())

    async def refresh(self, targets: PollRate | Selector | Sequence[str] | None = None, *,
                      after: float = 0.0) -> None:
        """Read a poll rate's points, a selection, or everything - now, or `after` seconds from now."""
        scheduler = self._require_scheduler()
        resolved = self._require_model()
        if isinstance(targets, PollRate):
            points = [p for p in resolved.points.values() if p.poll_rate is targets]
        elif targets is None:
            points = list(resolved.points.values())
        else:
            points = list(_select(resolved, targets))
        keys = [p.key for p in points if p.readable and self._is_available(p.key)]
        if keys:
            scheduler.refresh(keys, after=after, force=True)
        if after == 0:
            await self.poll()

    async def _read_and_publish(self, points: Sequence[Point]) -> None:
        answers = await self._device.read(points)
        # Before publishing: a recovery makes everything due, and must not undo these reads.
        self._update_reachability(any(a.outcome is not Outcome.NO_ANSWER for a in answers.values()))
        self._publish(points, answers)

    def _publish(self, points: Sequence[Point], answers: Mapping[str, ReadResult]) -> None:
        scheduler = self._require_scheduler()
        for point in points:
            answer = answers[point.key]
            previous = self._values.get(point.key)
            data = self._data_value(point, answer, previous)
            if data.quality is Quality.MISSING:
                self._unavailable[point.key] = answer.detail or answer.outcome.name.lower()
                self._update_polling(point.key)
            success = answer.outcome is Outcome.OK and data.quality is not Quality.STALE
            self._consecutive_failures[point.key] = 0 if success else self.consecutive_failures(point.key) + 1
            scheduler.record(point.key, data, success=success)
            self._store(point, data, previous)

    def _data_value(self, point: Point, answer: ReadResult, previous: DataValue | None) -> DataValue:
        now = self._clock.now()
        if answer.outcome is Outcome.OK:
            try:
                value, quality = conversion.decode(point, answer.registers)
            except conversion.InvalidValueError as err:
                _LOGGER.error("'%s' answered registers that do not fit it: %s", point.key, err)
                return _stale(previous, now)
            return DataValue(value, quality, now)
        if answer.outcome in (Outcome.MISSING, Outcome.UNSUPPORTED):
            return DataValue(None, Quality.MISSING, now)
        if answer.outcome is Outcome.OFFLINE:
            return DataValue(None, Quality.OFFLINE, now)
        return _stale(previous, now)

    def _store(self, point: Point, data: DataValue, previous: DataValue | None) -> None:
        self._values[point.key] = data
        self._subscriptions.report(point, data)
        trigger = point.on_change
        if trigger is not None and previous is not None and _changed(previous, data, trigger.when):
            keys = [p.key for p in _select(self._require_model(), trigger.targets)
                    if p.readable and self._is_available(p.key)]
            if keys:
                self._require_scheduler().refresh(keys, after=trigger.after or 0.0, until_stable=trigger.until_stable)

    def _update_reachability(self, answered: bool) -> None:
        """Update reachability from whether the device answered, logging each change once."""
        if answered == self.connected:
            return
        self._set_status(Status.CONNECTED, answered)
        name = self._resolved.model.name if self._resolved is not None else "The device"
        if not answered:
            self._reported_offline = True
            _LOGGER.info("%s stopped answering", name)
            return
        if self._scheduler is not None:
            self._scheduler.reset()
        if self._reported_offline:
            self._reported_offline = False
            _LOGGER.info("%s answers again", name)

    # ============================================================================= writing

    async def write(self, key: str, value: Value) -> bool:
        """
        Write `value` to `key` and return whether the device accepted it. Writes go out in
        order; a queued setting overtaken by a newer one returns the newer one's outcome.

        Raises:
            ReadOnlyError: the client is read-only.
            NotConnectedError: the device has not been scanned.
            KeyError: this unit has no such key.
            ValueError: the point cannot be written.
            InvalidValueError: the point refuses the value.
        """
        point = self._writable_point(key)
        conversion.encode(point, value)
        return await self._writes.write(point, value)

    async def write_sequence(self, writes: Sequence[tuple[str, Value]]) -> bool:
        """
        Write several values in order as one operation, stopping at the first refusal.
        Every value is checked before anything is sent. Returns whether all were accepted.
        """
        encoded = [(point, conversion.encode(point, value))
                   for point, value in ((self._writable_point(key), value) for key, value in writes)]
        return await self._writes.write_sequence(encoded)

    def _writable_point(self, key: str) -> Point:
        if self._read_only:
            raise ReadOnlyError(f"refused to write '{key}': the client is read-only")
        self._require_model()
        if not self.has(key):
            raise KeyError(f"this unit has no point {key!r}")
        point = self._point(key)
        if not point.writable:
            raise ValueError(f"'{key}' cannot be written")
        return point

    def _read_back(self, point: Point) -> None:
        """Schedule the read-back of the written point and of the points its write disturbs."""
        scheduler = self._require_scheduler()
        resolved = self._require_model()
        after = resolved.model.read_back_delay(point)
        if point.readable:
            scheduler.refresh([point.key], after=after)
        effect = point.on_write
        if effect is not None:
            keys = [p.key for p in _select(resolved, effect.targets)
                    if p.readable and self._is_available(p.key)]
            if keys:
                scheduler.refresh(keys, after=after, until_stable=effect.until_stable)

    # ============================================================================ internals

    def _require_model(self) -> ResolvedModel:
        if self._resolved is None:
            raise NotConnectedError("the device has not been scanned; call connect() first")
        return self._resolved

    def _require_scheduler(self) -> Scheduler:
        if self._scheduler is None:
            raise NotConnectedError("the device has not been scanned; call connect() first")
        return self._scheduler

    def _point(self, key: str) -> Point:
        return self._require_model().point(key)

    def _should_poll(self, point: Point) -> bool:
        """Whether anything wants `point` read: the model, its role as a trigger, or a consumer."""
        return (point.poll_always
                or point.on_change is not None
                or point.key in self._polled_keys
                or self._subscriptions.polled(point.key))

    def _update_polling(self, key: str) -> None:
        """Tell the scheduler whether `key` takes part in scheduling."""
        if self._resolved is None or self._scheduler is None or key not in self._resolved.points:
            return
        point = self._resolved.points[key]
        if point.readable:
            self._scheduler.set_polled(key, self._should_poll(point) and self._is_available(key))

    def _set_status(self, status: Status, value: bool) -> None:
        current = self._status[status]
        if current.value == value:
            return
        data = DataValue(value, Quality.GOOD, self._clock.now())
        self._status[status] = data
        self._subscriptions.tell(status, current, data)


class _ScanContext:
    """What a scan step may do: read without side effects, and record availability."""

    def __init__(self, identity: Identity,
                 read: Callable[[Selector | Sequence[str]], Awaitable[Mapping[str, DataValue]]],
                 mark: Callable[[Selector | Sequence[str], bool, str], None]) -> None:
        self._identity = identity
        self._read = read
        self._mark = mark

    @property
    def identity(self) -> Identity:
        return self._identity

    async def read(self, targets: Selector | Sequence[str]) -> Mapping[str, DataValue]:
        """Read the selected points; one this scan has read already is not read again.

        Nobody is notified until the whole scan has been answered.
        """
        return await self._read(targets)

    def set_available(self, targets: Selector | Sequence[str], available: bool, *,
                      reason: str = "") -> None:
        self._mark(targets, available, reason)


def _select(resolved: ResolvedModel, targets: Selector | Sequence[str]) -> tuple[Point, ...]:
    if isinstance(targets, Labels):
        return resolved.select(targets)
    if isinstance(targets, str):
        raise TypeError(f"give keys as a list or tuple, not the single string {targets!r}")
    return resolved.select(tuple(targets))


def _record_availability(record: dict[str, str], points: Iterable[Point], available: bool, reason: str) -> None:
    for point in points:
        if available:
            record.pop(point.key, None)
        else:
            record[point.key] = reason


def _stale(previous: DataValue | None, now: datetime) -> DataValue:
    """The last good value, marked STALE and keeping the time it was good."""
    if previous is not None and previous.quality in (Quality.GOOD, Quality.STALE):
        return DataValue(previous.value, Quality.STALE, previous.timestamp)
    return DataValue(None, Quality.STALE, now)


def _changed(old: DataValue, new: DataValue, when: Change) -> bool:
    """Whether a change from `old` to `new` matches `when`. Only good values compare."""
    if not (old.is_good and new.is_good) or old.value == new.value:
        return False
    if when is Change.ANY:
        return True
    before, after = old.value, new.value
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return False
    return after > before if when is Change.RISING else after < before
