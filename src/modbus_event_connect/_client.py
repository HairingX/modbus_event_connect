"""A device as a consumer sees it: the scan, values with quality, events, polling and writes."""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Any

from ._clock import Clock, SystemClock
from ._conversion import decode, encode
from ._device import Device, Identity, Outcome, ReadResult
from ._errors import (
    CannotConnectError,
    InvalidValueError,
    ModelError,
    NotConnectedError,
    ReadOnlyError,
    UnsupportedDeviceError,
)
from ._events import Subscriptions, ValueCallback, tell
from ._key import Key, is_key
from ._model import InstanceScanStep, Model, ModelSelector, RepeatedSection, ResolvedModel, resolve
from ._point import Change, Labels, Point, PollRate, Selector
from ._scheduler import Scheduler
from ._value import DataValue, Quality, Value
from ._writes import Write, WriteQueue

_LOGGER = logging.getLogger(__name__)

class Status(Enum):
    """What the client itself knows about its device, apart from the device's points."""
    CONNECTED = auto()
    """True while the device answers."""
    WRITE_PENDING = auto()
    """True from the first queued write until the last one has finished."""


StatusCallback = Callable[[Status, DataValue[bool] | None, DataValue[bool]], None]
"""Called with the status, its previous value (None the first time) and the new value."""

PointsCallback = Callable[[frozenset[Key[Any]], frozenset[Key[Any]]], None]
"""Called with the keys the unit has gained and the keys it has lost, once its points changed."""

_UNANSWERED = frozenset({Outcome.NO_ANSWER, Outcome.BUSY})

_DEFINITIVE = frozenset({Outcome.OK, Outcome.MISSING, Outcome.UNSUPPORTED, Outcome.OFFLINE})
"""Answers that say what the unit has. Any other leaves a check's outcome unknown."""

type _Scope = tuple[str, int] | None
"""What one scan covers: an instance of a repeated section, as its label and number, or the whole
unit, as None."""


@dataclass
class _Check:
    """What one scan read, as it read it, and when to read it again."""
    read: dict[str, tuple[Quality, Value]]
    due: float
    """Monotonic time; infinite when checks are switched off."""


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
        self._values: dict[str, DataValue[Any]] = {}
        self._subscriptions = Subscriptions()
        self._polled_keys: set[str] = set()
        # Missing keys with their reason, kept apart by who found them, so that scanning one
        # instance again replaces what its own scan found and nothing else.
        self._missing_by_scan: dict[_Scope, dict[str, str]] = {}
        self._missing_by_read: dict[str, str] = {}
        self._unavailable: dict[str, str] = {}
        self._checks: dict[_Scope, _Check] = {}
        self._scanned_labels: frozenset[str] = frozenset()
        self._points_callbacks: list[PointsCallback] = []
        self._interval_overrides: dict[PollRate | str, float | None] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._reported_offline = False
        self._status: dict[Status, DataValue[bool]] = {
            status: DataValue(False, Quality.GOOD, self._clock.now()) for status in Status}
        self._status_callbacks: dict[Status, list[StatusCallback]] = {status: [] for status in Status}

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

    async def disconnect(self) -> None:
        """Cancel pending pulses and let go of the device."""
        self._writes.cancel_pulses()
        await self._device.disconnect()
        self._reported_offline = False
        self._set_status(Status.CONNECTED, False)

    async def _scan(self, handshake: Identity) -> None:
        """
        Choose the model, run the scan of the whole unit and of each instance, and read every
        value once; commit only if every read was answered. An unanswered read never reports a
        register missing, so it cannot be trusted.
        """
        model = self._select_model(handshake)
        if model is None:
            raise UnsupportedDeviceError(f"no model for a device that reports {dict(handshake)!r}")
        self._device.configure(model.options)
        answers = _ScanAnswers()
        try:
            resolved = resolve(model, await self._identify(model, handshake, answers))
            scanned: dict[str, ReadResult] = {}
            marks: dict[_Scope, dict[str, str]] = {}
            reads: dict[_Scope, set[str]] = {}
            for scope in _scopes(resolved):
                marks[scope], reads[scope] = await self._run_scan(resolved, scope, answers, scanned)
            unavailable = _union(marks.values())
            readable = [p for p in resolved.points.values() if p.readable and p.key not in unavailable]
            unread = [p for p in readable if p.key not in scanned]
            first_answers: Mapping[str, ReadResult] = await self._device.read(unread) if unread else {}
            answers.note(first_answers.values())
            answers.require_all_answered()
        except CannotConnectError:
            self._update_reachability(answers.any_answered)
            raise
        self._commit(model, resolved, marks,
                     {scope: self._states([resolved.points[k] for k in keys], scanned)
                      for scope, keys in reads.items()})
        self._publish(readable, {**scanned, **first_answers}, scanning=True)

    async def _rescan(self, scope: tuple[str, int]) -> None:
        """Scan one instance again, and read what it now has.

        Raises:
            CannotConnectError: a read went unanswered; nothing was changed.
        """
        resolved = self._require_model()
        label, number = scope
        answers = _ScanAnswers()
        scanned: dict[str, ReadResult] = {}
        marked, read_keys = await self._run_scan(resolved, scope, answers, scanned)
        instance = resolved.select(Labels(**{label: number}))
        self._missing_by_scan[scope] = marked
        for point in instance:
            self._missing_by_read.pop(point.key, None)       # tried again with the instance
        self._recompute_unavailable()
        due = self._checks[scope].due
        if read_keys:
            self._checks[scope] = _Check(self._states([resolved.points[k] for k in read_keys], scanned), due)
        else:
            del self._checks[scope]
        readable = [p for p in instance if p.readable and self._is_available(p.key)]
        unread = [p for p in readable if p.key not in scanned]
        first_answers: Mapping[str, ReadResult] = await self._device.read(unread) if unread else {}
        for point in instance:
            self._update_polling(point.key)
        self._publish(readable, {**scanned, **first_answers}, scanning=True)

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

    async def _run_scan(self, resolved: ResolvedModel, scope: _Scope, answers: _ScanAnswers,
                        scanned: dict[str, ReadResult]) -> tuple[dict[str, str], set[str]]:
        """What one scan finds missing, key by key with the reason, and every key it read.

        Every answer it gets is kept in `scanned`, and a point is read at most once per `scanned`.

        Raises:
            CannotConnectError: a read went unanswered.
            ModelError: the scan of an instance marked a point of another.
        """
        marked: dict[str, str] = {}
        read_keys: set[str] = set()

        async def read(targets: Selector | Sequence[str]) -> Mapping[str, DataValue[Any]]:
            points = [p for p in _select(resolved, targets) if p.readable]
            read_keys.update(p.key for p in points)
            unread = [p for p in points if p.key not in scanned]
            raw: Mapping[str, ReadResult] = {}
            if unread:
                raw = await self._device.read(unread)
                answers.note(raw.values())
                scanned.update((key, answer) for key, answer in raw.items() if answer.outcome not in _UNANSWERED)
            return {p.key: self._data_value(p, scanned[p.key] if p.key in scanned else raw[p.key], previous=None)
                    for p in points}

        def mark(targets: Selector | Sequence[str], available: bool, reason: str) -> None:
            points = _select(resolved, targets)
            if scope is not None:
                label, number = scope
                foreign = [p.key for p in points if p.labels.get(label) != number]
                if foreign:
                    # Scanned again by itself, an instance could otherwise undo what another found.
                    raise ModelError(f"the scan of {label} {number} marked {foreign[0]!r}, which is not its own")
            _record_availability(marked, points, available, reason)

        scan = _ScanContext(resolved.identity, read, mark)
        if scope is None:
            for step in resolved.model.scan_steps:
                await step(scan)
        else:
            await _instance_scan(resolved.model, scope[0])(scan, scope[1])
        answers.require_all_answered()
        return marked, read_keys

    def _states(self, points: Iterable[Point[Any]],
                answers: Mapping[str, ReadResult]) -> dict[str, tuple[Quality, Value]]:
        """What each point's answer says, as a check compares it."""
        found: dict[str, tuple[Quality, Value]] = {}
        for point in points:
            data = self._data_value(point, answers[point.key], previous=None)
            found[point.key] = (data.quality, data.value)
        return found

    def _commit(self, model: Model, resolved: ResolvedModel, marks: dict[_Scope, dict[str, str]],
                reads: dict[_Scope, dict[str, tuple[Quality, Value]]]) -> None:
        scheduler = Scheduler(self._clock, model.poll_intervals, min_poll_interval=model.min_poll_interval)
        scheduler.set_points(resolved.points.values())
        for target, seconds in self._interval_overrides.items():
            if isinstance(target, PollRate) or target in resolved.points:
                scheduler.set_poll_interval(target, seconds)

        self._resolved, self._scheduler = resolved, scheduler
        self._missing_by_scan, self._missing_by_read = marks, {}
        self._recompute_unavailable()
        self._scanned_labels = frozenset(scope[0] for scope in marks if scope is not None)
        # Spread evenly over the interval, so that each poll checks a little rather than all at once.
        checked = [scope for scope in marks if reads[scope]]
        interval = scheduler.rate_interval(PollRate.SCAN)
        now = self._clock.monotonic()
        self._checks = {scope: _Check(reads[scope],
                                      now + interval * (index + 1) / len(checked) if interval else math.inf)
                        for index, scope in enumerate(checked)}
        for key in [k for k in self._values if k not in resolved.points]:
            del self._values[key]
            self._subscriptions.forget(key)
        for key in self._subscriptions.keys():
            if key not in resolved.points:
                _LOGGER.warning("'%s' is subscribed to, but the %s model does not have it", key, model.name)
            elif resolved.points[key].key.type is not key.type:
                _LOGGER.error("'%s' is subscribed to as a %s, but the %s model's point holds a %s; it is not "
                              "reported", key, key.type.__name__, model.name, resolved.points[key].key.type.__name__)
        for key in resolved.points:
            self._update_polling(key)
        self._update_reachability(True)

    # ========================================================================= the picture

    @property
    def model(self) -> Model:
        return self._require_model().model

    @property
    def points(self) -> Mapping[Key[Any], Point[Any]]:
        """The points this unit has, by key: declared by its model and not recorded as missing."""
        resolved = self._require_model()
        return {point.key: point for key, point in resolved.points.items() if self._is_available(key)}

    def instances(self, label: str) -> tuple[int, ...]:
        """The numbers of the instances of `label` of which this unit has at least one point."""
        resolved = self._require_model()
        return tuple(n for n in resolved.instances.get(label, ())
                     if any(self._is_available(p.key) for p in resolved.select(Labels(**{label: n}))))

    def select(self, targets: Selector | Sequence[str]) -> tuple[Point[Any], ...]:
        """The points `targets` selects, among those this unit has."""
        return tuple(p for p in _select(self._require_model(), targets) if self._is_available(p.key))

    def has(self, key: str) -> bool:
        """Whether this unit has `key`."""
        return self._resolved is not None and key in self._resolved.points and self._is_available(key)

    def can_read(self, key: str) -> bool:
        return self.has(key) and self._point(key).readable

    def can_write(self, key: str) -> bool:
        return self.has(key) and self._point(key).writable

    @property
    def unavailable_reasons(self) -> Mapping[Key[Any], str]:
        """The keys recorded as missing on this unit, with the reason."""
        resolved = self._require_model()
        return {resolved.points[key].key: reason for key, reason in self._unavailable.items()}

    # ============================================================================== values

    def value[T](self, key: Key[T]) -> DataValue[T] | None:
        """The current value of `key`, or None if it was never read.

        Raises:
            TypeError: the model gives the point another type than `key` does.
        """
        self._check_type(key)
        return self._values.get(key)

    @property
    def values(self) -> Mapping[Key[Any], DataValue[Any]]:
        """The current value of each key this unit has, among those read."""
        resolved = self._require_model()
        return {resolved.points[key].key: value for key, value in self._values.items() if self._is_available(key)}

    def status(self, status: Status) -> DataValue[bool]:
        """The current value of `status`: True or False, from the moment the client exists."""
        return self._status[status]

    def consecutive_failures(self, key: str) -> int:
        """Consecutive failed reads of `key`."""
        return self._consecutive_failures.get(key, 0)

    # ============================================================================ interest

    def subscribe[T](self, key: Key[T], callback: ValueCallback[T], *, poll: bool = True) -> Callable[[], None]:
        """
        Call `callback` on every change of `key`'s value or quality, starting with the current
        value. With `poll`, the point is also read on its schedule. Returns the unsubscriber.

        Raises:
            KeyError: after the scan, the model has no such key.
            TypeError: the model gives the point another type than `key` does.
        """
        if self._resolved is not None and key not in self._resolved.points:
            raise KeyError(f"the {self._resolved.model.name} model has no point {key!r}")
        self._check_type(key)
        subscriber = self._subscriptions.add(key, callback, polls=poll)
        self._update_polling(key)
        current = self.value(key)
        if current is not None:
            tell(subscriber, None, current)

        def unsubscribe() -> None:
            self._subscriptions.remove(key, subscriber)
            self._update_polling(key)
        return unsubscribe

    def subscribe_status(self, status: Status, callback: StatusCallback) -> Callable[[], None]:
        """Call `callback` on every change of `status`, starting with its current value.

        Returns the unsubscriber.
        """
        callbacks = self._status_callbacks[status]
        callbacks.append(callback)
        _tell_status(callback, status, None, self._status[status])

        def unsubscribe() -> None:
            if callback in callbacks:
                callbacks.remove(callback)
        return unsubscribe

    def subscribe_points(self, callback: PointsCallback) -> Callable[[], None]:
        """Call `callback` whenever the points this unit has change, with the keys gained and lost.

        A scan checked at `PollRate.SCAN`, or a register a read finds missing, can change them;
        a key lost is also told to its subscribers as `MISSING`. Returns the unsubscriber.
        """
        self._points_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._points_callbacks:
                self._points_callbacks.remove(callback)
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

        `PollRate.SCAN` sets how often what each scan read is read again; it applies from each
        scan's next check.
        """
        self._interval_overrides[target] = seconds
        if self._scheduler is None:
            return seconds
        return self._scheduler.set_poll_interval(target, seconds)

    # ======================================================================== availability

    def _is_available(self, key: str) -> bool:
        return key not in self._unavailable

    def _recompute_unavailable(self) -> None:
        self._unavailable = {**_union(self._missing_by_scan.values()), **self._missing_by_read}

    def _available_points(self) -> dict[str, Key[Any]]:
        if self._resolved is None:
            return {}
        return {key: point.key for key, point in self._resolved.points.items() if self._is_available(key)}

    def _tell_points(self, before: Mapping[str, Key[Any]]) -> None:
        """Tell what the unit gained and lost since `before`; a lost key's subscribers see MISSING."""
        after = self._available_points()
        added = frozenset(after[key] for key in after.keys() - before.keys())
        removed = frozenset(before[key] for key in before.keys() - after.keys())
        if not added and not removed:
            return
        resolved = self._require_model()
        now = self._clock.now()
        for key in removed:
            current = self._values.get(key)
            if key in resolved.points and (current is None or current.quality is not Quality.MISSING):
                self._store(resolved.points[key], DataValue(None, Quality.MISSING, now), current)
        for callback in list(self._points_callbacks):
            try:
                callback(added, removed)
            except Exception:
                _LOGGER.exception("A points callback failed")

    def _scope_of(self, point: Point[Any]) -> _Scope:
        """The scan that decides whether this unit has `point`."""
        for label in self._scanned_labels:
            number = point.labels.get(label)
            if isinstance(number, int):
                return (label, number)
        return None

    # ============================================================================= reading

    async def poll(self) -> None:
        """
        Read whatever is due, and read again what each scan read when that is due, to find out
        whether the unit has changed. A call made during a pass waits for it instead of starting
        another.

        Raises:
            UnsupportedDeviceError: the unit changed, and no model matches it any more.
        """
        if self._poll_lock.locked():
            async with self._poll_lock:
                return
        async with self._poll_lock:
            before = self._available_points()
            try:
                keys = self._require_scheduler().due()
                if keys:
                    resolved = self._require_model()
                    await self._read_and_publish([resolved.points[k] for k in keys])
                await self._run_checks()
            finally:
                self._tell_points(before)

    async def _run_checks(self) -> None:
        """Read again what each due scan read, and scan again where that has changed."""
        now = self._clock.monotonic()
        interval = self._require_scheduler().rate_interval(PollRate.SCAN)
        for scope in [scope for scope, check in self._checks.items() if check.due <= now]:
            check = self._checks.get(scope)
            if check is None:
                continue                                   # a scan of the whole unit replaced it
            check.due = now + interval if interval is not None else math.inf
            resolved = self._require_model()
            points = [resolved.points[key] for key in check.read]
            answers = await self._device.read(points)
            self._update_reachability(any(a.outcome is not Outcome.NO_ANSWER for a in answers.values()))
            if any(answers[point.key].outcome not in _DEFINITIVE for point in points):
                continue                                   # unknown: what the scan found stands
            if self._states(points, answers) == check.read:
                continue
            try:
                if scope is None:
                    _LOGGER.info("%s has changed; scanning all of it again", resolved.model.name)
                    await self._scan(dict(resolved.identity))
                    return
                _LOGGER.info("%s %s %d has changed; scanning it again", resolved.model.name, *scope)
                await self._rescan(scope)
            except CannotConnectError as err:
                _LOGGER.info("Scanning again went unanswered; checked again later: %s", err)

    def seconds_until_next_poll(self) -> float | None:
        """Seconds until `poll()` has something to do, 0 if it has now; None if it never will."""
        candidates = [check.due for check in self._checks.values() if math.isfinite(check.due)]
        due = self._require_scheduler().next_due()
        if due is not None:
            candidates.append(due)
        return max(0.0, min(candidates) - self._clock.monotonic()) if candidates else None

    async def refresh(self, targets: PollRate | Selector | Sequence[str] | None = None, *,
                      after: float = 0.0) -> None:
        """
        Read a poll rate's points, a selection, or everything - now, or `after` seconds from now.
        `PollRate.SCAN` checks again whether the unit has changed.
        """
        scheduler = self._require_scheduler()
        resolved = self._require_model()
        if targets is PollRate.SCAN:
            due = self._clock.monotonic() + after
            for check in self._checks.values():
                check.due = min(check.due, due)
            if after == 0:
                await self.poll()
            return
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

    async def _read_and_publish(self, points: Sequence[Point[Any]]) -> None:
        answers = await self._device.read(points)
        # Before publishing: a recovery makes everything due, and must not undo these reads.
        self._update_reachability(any(a.outcome is not Outcome.NO_ANSWER for a in answers.values()))
        self._publish(points, answers)

    def _publish(self, points: Sequence[Point[Any]], answers: Mapping[str, ReadResult], *,
                 scanning: bool = False) -> None:
        """Store and tell what was read. A register found missing is no longer read; outside a scan,
        what decides whether the unit has it is checked at once."""
        scheduler = self._require_scheduler()
        for point in points:
            answer = answers[point.key]
            previous = self._values.get(point.key)
            data = self._data_value(point, answer, previous)
            if data.quality is Quality.MISSING:
                reason = answer.detail or answer.outcome.name.lower()
                self._missing_by_read[point.key] = self._unavailable[point.key] = reason
                self._update_polling(point.key)
                check = self._checks.get(self._scope_of(point))
                if not scanning and check is not None:
                    check.due = min(check.due, self._clock.monotonic())
            success = answer.outcome is Outcome.OK and data.quality is not Quality.STALE
            self._consecutive_failures[point.key] = 0 if success else self.consecutive_failures(point.key) + 1
            scheduler.record(point.key, data, success=success)
            self._store(point, data, previous)

    def _data_value(self, point: Point[Any], answer: ReadResult, previous: DataValue[Any] | None) -> DataValue[Any]:
        now = self._clock.now()
        if answer.outcome is Outcome.OK:
            try:
                value, quality = decode(point, answer.registers)
            except InvalidValueError as err:
                _LOGGER.error("'%s' answered registers that do not fit it: %s", point.key, err)
                return _stale(previous, now)
            return DataValue(value, quality, now, raw=tuple(answer.registers))
        if answer.outcome in (Outcome.MISSING, Outcome.UNSUPPORTED):
            return DataValue(None, Quality.MISSING, now)
        if answer.outcome is Outcome.OFFLINE:
            return DataValue(None, Quality.OFFLINE, now)
        return _stale(previous, now)

    def _store(self, point: Point[Any], data: DataValue[Any], previous: DataValue[Any] | None) -> None:
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
        if answered == self._status[Status.CONNECTED].value:
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

    async def write[T](self, key: Key[T], value: T) -> bool:
        """
        Write `value` to `key` and return whether the device accepted it. Writes go out in
        order; a queued setting overtaken by a newer one returns the newer one's outcome.

        Raises:
            ReadOnlyError: the client is read-only.
            NotConnectedError: the device has not been scanned.
            KeyError: this unit has no such key.
            ValueError: the point cannot be written.
            InvalidValueError: the point refuses the value.
            TypeError: the model gives the point another type than `key` does.
        """
        point = self._writable_point(key)
        encode(point, value)
        return await self._writes.write(point, value)

    async def write_sequence(self, writes: Sequence[Write[Any]]) -> bool:
        """
        Write several values in order as one operation, stopping at the first refusal.
        Every value is checked before anything is sent. Returns whether all were accepted.
        """
        encoded = [(point, encode(point, write.value))
                   for point, write in ((self._writable_point(write.key), write) for write in writes)]
        return await self._writes.write_sequence(encoded)

    def _writable_point(self, key: Key[Any]) -> Point[Any]:
        if self._read_only:
            raise ReadOnlyError(f"refused to write '{key}': the client is read-only")
        self._require_model()
        if not self.has(key):
            raise KeyError(f"this unit has no point {key!r}")
        self._check_type(key)
        point = self._point(key)
        if not point.writable:
            raise ValueError(f"'{key}' cannot be written")
        return point

    def _read_back(self, point: Point[Any]) -> None:
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

    def _point(self, key: str) -> Point[Any]:
        return self._require_model().point(key)

    def _check_type(self, key: Key[Any]) -> None:
        """Raise TypeError unless `key` is a Key of the type the model gives its point."""
        if not is_key(key):
            raise TypeError(f"{key!r} is a {type(key).__name__}; use the Key the model declares")
        point = self._resolved.points.get(key) if self._resolved is not None else None
        if point is not None and point.key.type is not key.type:
            raise TypeError(f"{key!r} names a {key.type.__name__}, but the "
                            f"{self._require_model().model.name} model's point holds a {point.key.type.__name__}")

    def _should_poll(self, point: Point[Any]) -> bool:
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
        for callback in list(self._status_callbacks[status]):
            _tell_status(callback, status, current, data)


class _ScanContext:
    """What a scan step may do: read without side effects, and record availability."""

    def __init__(self, identity: Identity,
                 read: Callable[[Selector | Sequence[str]], Awaitable[Mapping[str, DataValue[Any]]]],
                 mark: Callable[[Selector | Sequence[str], bool, str], None]) -> None:
        self._identity = identity
        self._read = read
        self._mark = mark

    @property
    def identity(self) -> Identity:
        return self._identity

    async def read(self, targets: Selector | Sequence[str]) -> Mapping[str, DataValue[Any]]:
        """Read the selected points; one this scan has read already is not read again.

        Nobody is notified until the whole scan has been answered.
        """
        return await self._read(targets)

    def set_available(self, targets: Selector | Sequence[str], available: bool, *,
                      reason: str = "") -> None:
        self._mark(targets, available, reason)


def _select(resolved: ResolvedModel, targets: Selector | Sequence[str]) -> tuple[Point[Any], ...]:
    if isinstance(targets, Labels):
        return resolved.select(targets)
    if isinstance(targets, str):
        raise TypeError(f"give keys as a list or tuple, not the single string {targets!r}")
    return resolved.select(tuple(targets))


def _record_availability(record: dict[str, str], points: Iterable[Point[Any]], available: bool, reason: str) -> None:
    for point in points:
        if available:
            record.pop(point.key, None)
        else:
            record[point.key] = reason


def _scopes(resolved: ResolvedModel) -> list[_Scope]:
    """Every scan of this unit, in the order they run: the whole unit's, then each instance's."""
    scopes: list[_Scope] = [None]
    for section in resolved.model.sections:
        if isinstance(section, RepeatedSection) and section.scan is not None:
            scopes.extend((section.label, number) for number in resolved.instances.get(section.label, ()))
    return scopes


def _instance_scan(model: Model, label: str) -> InstanceScanStep:
    """The scan of the repeated section labelled `label`."""
    for section in model.sections:
        if isinstance(section, RepeatedSection) and section.label == label and section.scan is not None:
            return section.scan
    raise KeyError(f"no repeated section labelled {label!r} has a scan")


def _union(marks: Iterable[Mapping[str, str]]) -> dict[str, str]:
    """Every key in any of `marks`, with the reason the first gives."""
    found: dict[str, str] = {}
    for marked in marks:
        for key, reason in marked.items():
            found.setdefault(key, reason)
    return found


def _stale(previous: DataValue[Any] | None, now: datetime) -> DataValue[Any]:
    """The last good value, marked STALE and keeping the time it was good."""
    if previous is not None and previous.quality in (Quality.GOOD, Quality.STALE):
        return DataValue(previous.value, Quality.STALE, previous.timestamp)
    return DataValue(None, Quality.STALE, now)


def _changed(old: DataValue[Any], new: DataValue[Any], when: Change) -> bool:
    """Whether a change from `old` to `new` matches `when`. Only good values compare."""
    if not (old.is_good and new.is_good) or old.value == new.value:
        return False
    if when is Change.ANY:
        return True
    before, after = old.value, new.value
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return False
    return after > before if when is Change.RISING else after < before


def _tell_status(callback: StatusCallback, status: Status, old: DataValue[Any] | None, new: DataValue[Any]) -> None:
    """Call one status subscriber; its error is logged and kept from the others."""
    try:
        callback(status, old, new)
    except Exception:
        _LOGGER.exception("a subscriber to %s raised", status)
