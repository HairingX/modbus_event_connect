"""A device as a consumer sees it: the scan, values with quality, events, polling and writes."""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from event_connect import (
    Area,
    CannotConnectError,
    Clock,
    DataValue,
    Identity,
    InvalidValueError,
    Key,
    Labels,
    Level,
    ModelError,
    NotConnectedError,
    Passes,
    PointsCallback,
    PollRate,
    Quality,
    Reachability,
    ReadOnlyError,
    Scheduler,
    Selector,
    Snapshot,
    Status,
    StatusBoard,
    StatusCallback,
    Subscriptions,
    SystemClock,
    UnsupportedDeviceError,
    Value,
    ValueCallback,
    Write,
    WriteKind,
    WriteLog,
    changed,
    is_key,
    selector,
    should_poll,
    stale,
    tell,
)

from ._conversion import decode, encode
from ._device import Device, Outcome, ReadResult
from ._mapping import Mapper, areas
from ._model import InstanceScanStep, Model, ModelSelector, RepeatedSection, ResolvedModel, resolve
from ._point import Point
from ._writes import WriteQueue

_LOGGER = logging.getLogger(__name__)

_UNANSWERED = frozenset({Outcome.NO_ANSWER, Outcome.BUSY})

_PAUSE_AFTER_FAILURE = 1.0  # guess
"""Seconds `run()` waits after a poll that raised, so that a failure that repeats is not retried
without a pause."""

_WRITES_KEPT = 5
"""How many of the latest writes to each point are kept: enough to see a value set, set again,
and read back."""

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
    One device, scanned by `connect()` and read by `run()`, or by `poll()` on the host's schedule.
    `model` is a model, or a selector choosing one of the device's models from its identity.
    """

    def __init__(self, device: Device, model: Model | ModelSelector, *, offline_after_intervals: int,
                 level: Level, read_only: bool = False, clock: Clock | None = None) -> None:
        """Args:
            offline_after_intervals: how many reads of the chosen model's fastest poll rate the
                device may leave unanswered before `Status.CONNECTED` turns False.
            level: which of the model's points the unit has: those of a certainty it admits, and
                at `Level.TEST` each candidate in place of the point it replaces.

        Raises:
            ValueError: `offline_after_intervals` is below 1.
        """
        self._device = device
        self._select_model: ModelSelector = (lambda _identity: model) if isinstance(model, Model) else model
        known = (model,) if isinstance(model, Model) else tuple(device.models)
        self._level = level
        self._known_keys: frozenset[str] = frozenset().union(*(known_model.keys_at(level) for known_model in known))
        self._clock: Clock = clock or SystemClock()
        self._read_only = read_only
        self._offline_after_intervals = offline_after_intervals
        self._connected = False
        """Whether `connect()` succeeded since the last `disconnect()`."""
        self._wake = asyncio.Event()
        """Set when something may be due sooner than `run()` planned for."""

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
        self._scheduled_polling = True
        self._checks_requested: float | None = None
        """When a refresh or a missing register asked for the checks, which runs them without the
        schedule."""
        self._model: Model | None = None
        """The model chosen last; None before the first scan chooses one."""
        self._reachability = self._fresh_reachability()
        self._statuses = StatusBoard(self._clock)

        self._passes = Passes()
        self._write_log = WriteLog(self._clock, per_key=_WRITES_KEPT)
        self._writes = WriteQueue(device, clock=self._clock,
                                  retry=lambda: self._require_model().model.write_retry,
                                  statuses=self._statuses, log=self._write_log,
                                  answered=self._update_reachability, written=self._taken)

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
        self._connected = True
        self._wake.set()

    async def disconnect(self) -> None:
        """Cancel pending pulses and let go of the device: `Status.CONNECTED` turns False, and
        nothing is read or written until `connect()`."""
        self._connected = False
        self._writes.cancel_pulses()
        await self._device.disconnect()
        self._reachability = self._fresh_reachability()
        self._statuses.set(Status.CONNECTED, False)
        self._wake.set()

    async def _scan(self, handshake: Identity) -> None:
        """
        Choose the model, run the scan of the whole unit and of each instance, and read every
        value once; commit only if every read was answered. An unanswered read never reports a
        register missing, so it cannot be trusted.
        """
        model = self._select_model(handshake)
        if model is None:
            raise UnsupportedDeviceError(f"no model for a device that reports {dict(handshake)!r}", handshake)
        self._device.use(model)
        if model is not self._model:
            self._model = model
            self._reachability = self._fresh_reachability()
        marks = self._writes.marks()
        answers = _ScanAnswers()
        try:
            resolved = resolve(model, await self._identify(model, handshake, answers), self._level)
            _LOGGER.debug("%s chosen for a device that reports %r", model.name, dict(resolved.identity))
            scanned: dict[str, ReadResult] = {}
            found: dict[_Scope, dict[str, str]] = {}
            reads: dict[_Scope, set[str]] = {}
            for scope in _scopes(resolved):
                found[scope], reads[scope] = await self._run_scan(resolved, scope, answers, scanned)
            unavailable = _union(found.values())
            readable = [p for p in resolved.used.values() if p.readable and p.key not in unavailable]
            unread = [p for p in readable if p.key not in scanned]
            first_answers: Mapping[str, ReadResult] = await self._device.read(unread) if unread else {}
            answers.note(first_answers.values())
            answers.require_all_answered()
        except CannotConnectError:
            self._update_reachability(answers.any_answered)
            raise
        self._commit(model, resolved, found,
                     {scope: self._states([resolved.points[k] for k in keys], scanned)
                      for scope, keys in reads.items()})
        self._publish(readable, {**scanned, **first_answers}, since=marks, scanning=True)

    async def _rescan(self, scope: tuple[str, int]) -> None:
        """Scan one instance again, and read what it now has.

        Raises:
            CannotConnectError: a read went unanswered; nothing was changed.
        """
        resolved = self._require_model()
        label, number = scope
        marks = self._writes.marks()
        answers = _ScanAnswers()
        scanned: dict[str, ReadResult] = {}
        marked, read_keys = await self._run_scan(resolved, scope, answers, scanned)
        instance = _select(resolved, Labels(**{label: number}))
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
        self._publish(readable, {**scanned, **first_answers}, since=marks, scanning=True)

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
        scheduler = model.timing.scheduler(self._clock)
        scheduler.set_points(resolved.points.values())
        scheduler.set_scheduled(self._scheduled_polling)

        self._resolved, self._scheduler = resolved, scheduler
        self._missing_by_scan, self._missing_by_read = marks, {}
        self._recompute_unavailable()
        self._scanned_labels = frozenset(scope[0] for scope in marks if scope is not None)
        # Spread evenly over the interval, so that each poll checks a little rather than all at once.
        checked = [scope for scope in marks if reads[scope]]
        interval = _check_interval(model, scheduler)
        now = self._clock.monotonic()
        self._checks = {scope: _Check(reads[scope],
                                      now + interval * (index + 1) / len(checked) if interval else math.inf)
                        for index, scope in enumerate(checked)}
        for key in [k for k in self._values if k not in resolved.used]:
            del self._values[key]
            self._subscriptions.forget(key)
        for key in self._subscriptions.keys():
            if key not in resolved.used:
                _LOGGER.warning("'%s' is subscribed to, but the %s model does not have it at the %s level",
                                key, model.name, self._level)
            elif resolved.points[key].key.type is not key.type:
                _LOGGER.error("'%s' is subscribed to as a %s, but the %s model's point holds a %s; it is not "
                              "reported", key, key.type.__name__, model.name, resolved.points[key].key.type.__name__)
        for key in resolved.points:
            self._update_polling(key)
        self._update_reachability(True)

    # ========================================================================= the picture

    @property
    def level(self) -> Level:
        """Which of the model's points the unit has, by their certainty, and whether candidates are
        tried."""
        return self._level

    @property
    def model(self) -> Model:
        """The model chosen for the device.

        Raises:
            NotConnectedError: the device has not been scanned.
        """
        return self._require_model().model

    @property
    def identity(self) -> Identity:
        """What the device reported about itself when it connected, with what the model's identity
        points read."""
        return self._require_model().identity

    @property
    def points(self) -> Mapping[Key[Any], Point[Any]]:
        """The points this unit has, by key: declared by its model, used at the client's level, and
        not recorded as missing."""
        resolved = self._require_model()
        return {point.key: point for key, point in resolved.points.items() if self._is_available(key)}

    def instances(self, label: str) -> tuple[int, ...]:
        """The numbers of the instances of `label` of which this unit has at least one point."""
        resolved = self._require_model()
        return tuple(n for n in resolved.instances.get(label, ())
                     if any(self._is_available(p.key) for p in _select(resolved, Labels(**{label: n}))))

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
        """The keys recorded as missing on this unit, with the reason; a key the level leaves out
        is not among them."""
        resolved = self._require_model()
        return {resolved.points[key].key: reason for key, reason in self._unavailable.items()
                if key not in resolved.left_out}

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
        return self._statuses.value(status)

    def consecutive_failures(self, key: str) -> int:
        """Consecutive failed reads of `key`, as its schedule counts them.

        Raises:
            NotConnectedError: the device has not been scanned.
            KeyError: this unit has no readable point `key`.
        """
        return self._require_scheduler().consecutive_failures(key)

    @property
    def silent(self) -> bool:
        """Whether the device left the latest request unanswered."""
        return self._reachability.silent

    @property
    def silent_for(self) -> float:
        """Seconds since the device first left a request unanswered since its last answer; 0 while
        it answers."""
        return self._reachability.silent_for

    def diagnostics(self) -> Mapping[str, object]:
        """What a bug report needs, with no address: the device's counters - requests, failures by
        outcome, latency; the `level`; the `writes` sent to each key, the latest few, the newest
        last, each with how it ended and the first answered read of the key after it; and, once
        the device has been scanned, the `certainty` of each point the unit has, and the
        `candidates` among them, each with the key it replaces or None."""
        found: dict[str, object] = {**self._device.diagnostics(), "level": str(self._level),
                                    "writes": self._write_log.diagnostics()}
        if self._resolved is not None:
            points = self.points
            found["certainty"] = {str(key): str(point.certainty) for key, point in points.items()}
            found["candidates"] = {str(key): _replaced(point) for key, point in points.items()
                                   if point.candidate is not None}
        return found

    # ============================================================================ interest

    def subscribe[T](self, key: Key[T], callback: ValueCallback[T], *, poll: bool = True) -> Callable[[], None]:
        """
        Call `callback` on every change of `key`'s value or quality, starting with the current
        value. With `poll`, the point is also read on its schedule. Returns the unsubscriber.

        Raises:
            KeyError: no model the client may choose has `key` at the client's level; after the
                scan, the chosen model has no such key at it.
            TypeError: the model gives the point another type than `key` does.
        """
        if key not in self._known_keys:
            raise KeyError(f"no model of this client has a point {key!r} at the {self._level} level")
        if self._resolved is not None and key not in self._resolved.used:
            raise KeyError(f"the {self._resolved.model.name} model has no point {key!r} at the {self._level} level")
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
        return self._statuses.subscribe(status, callback)

    def subscribe_points(self, callback: PointsCallback) -> Callable[[], None]:
        """Call `callback` whenever the points this unit has change, with the keys gained and lost.

        A scan checked again at the model's `scan_check` rate, or a register a read finds missing,
        can change them;
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

    def set_scheduled_polling(self, enabled: bool) -> None:
        """Whether `poll()` reads on the schedule: every poll rate, and the checks for whether the
        unit has changed. Off, it reads only what `refresh()` and writes ask for."""
        self._scheduled_polling = enabled
        if self._scheduler is not None:
            self._scheduler.set_scheduled(enabled)
        self._wake.set()

    # ======================================================================== availability

    def _is_available(self, key: str) -> bool:
        return key not in self._unavailable and (self._resolved is None or key not in self._resolved.left_out)

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

    async def run(self) -> NoReturn:
        """Poll whenever something is due, until cancelled; it never ends by itself.

        While the client is not connected - before `connect()`, or after `disconnect()` - it
        waits until it is. A poll that raises is logged, and polling goes on after a pause: the
        first failure in a row at error level with its traceback, the ones after it at debug
        level, and the first poll that succeeds after them at info level.
        """
        failing = False
        while True:
            if self._connected:
                try:
                    await self.poll()
                except Exception as error:
                    if failing:
                        _LOGGER.debug("%s could not be polled: %r", self._reachability.name, error)
                    else:
                        _LOGGER.exception("%s could not be polled; polling goes on", self._reachability.name)
                    failing = True
                else:
                    if failing:
                        _LOGGER.info("%s is polled again", self._reachability.name)
                    failing = False
            # Cleared only now: whatever the poll scheduled, the wait below is planned with.
            self._wake.clear()
            if not self._connected:
                await self._doze(None)
            else:
                await self._doze(_PAUSE_AFTER_FAILURE if failing else self.seconds_until_next_poll())

    async def _doze(self, seconds: float | None) -> None:
        """Wait `seconds`, or until woken; None waits until woken."""
        waits: set[asyncio.Future[Any]] = {asyncio.ensure_future(self._wake.wait())}
        if seconds is not None:
            waits.add(asyncio.ensure_future(self._clock.sleep(seconds)))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()

    async def poll(self) -> None:
        """
        Read whatever is due, and read again what each scan read when that is due, to find out
        whether the unit has changed. A call made during a pass waits for it, then makes a pass
        of its own only if something is still due.

        Raises:
            NotConnectedError: the client is not connected.
            UnsupportedDeviceError: the unit changed, and no model matches it any more.
        """
        self._require_connected()
        await self._passes.run(self._pass, due=self._anything_due)

    async def _pass(self) -> None:
        before = self._available_points()
        try:
            marks = self._writes.marks()
            keys = [key for key in self._require_scheduler().due() if not self._writes.disturbed(key, marks)]
            if keys:
                resolved = self._require_model()
                await self._read_and_publish([resolved.points[k] for k in keys])
            requested = self._checks_requested
            if requested is not None and requested <= self._clock.monotonic():
                self._checks_requested = None
                await self._run_checks()
            elif self._scheduled_polling:
                await self._run_checks()
        finally:
            self._tell_points(before)

    def _anything_due(self) -> bool:
        if self._scheduler is None:
            return True
        requested = self._checks_requested
        return bool(self._scheduler.due()) or (requested is not None and requested <= self._clock.monotonic())

    async def _run_checks(self) -> None:
        """Read again what each due scan read, and scan again where that has changed."""
        now = self._clock.monotonic()
        interval = _check_interval(self._require_model().model, self._require_scheduler())
        for scope in [scope for scope, check in self._checks.items() if check.due <= now]:
            check = self._checks[scope]
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
        candidates = ([check.due for check in self._checks.values() if math.isfinite(check.due)]
                      if self._scheduled_polling else [])
        if self._checks_requested is not None:
            candidates.append(self._checks_requested)
        due = self._require_scheduler().next_due()
        if due is not None:
            candidates.append(due)
        return max(0.0, min(candidates) - self._clock.monotonic()) if candidates else None

    async def refresh(self, targets: PollRate | Selector | Sequence[str] | None = None, *,
                      after: float = 0.0) -> None:
        """
        Read a poll rate's points, a selection, or everything - now, or `after` seconds from now.
        The model's `scan_check` rate also checks again whether the unit has changed.

        Raises:
            NotConnectedError: the client is not connected.
        """
        self._require_connected()
        scheduler = self._require_scheduler()
        resolved = self._require_model()
        if isinstance(targets, PollRate):
            if targets is resolved.model.scan_check:
                due = self._clock.monotonic() + after
                for check in self._checks.values():
                    check.due = min(check.due, due)
                self._checks_requested = due if self._checks_requested is None else min(self._checks_requested, due)
            points = [p for p in resolved.points.values() if p.poll_rate is targets]
        elif targets is None:
            points = list(resolved.points.values())
        else:
            points = list(_select(resolved, targets))
        keys = [p.key for p in points if p.readable and self._is_available(p.key)]
        if keys:
            scheduler.refresh(keys, after=after, force=True)
        self._wake.set()
        if after == 0:
            await self.poll()

    def mapping_areas(self, *, limit: int) -> tuple[Area, ...]:
        """The areas `map_areas(limit=limit)` reads: for each address space the model's points read
        or write, every number of it that has an address, from its lowest for `limit` numbers, or
        up to the highest number they read or write where that is higher.

        Raises:
            NotConnectedError: the device has not been scanned.
            ValueError: `limit` is below 1.
        """
        return areas(self._require_model().model, limit)

    async def map_areas(self, *, limit: int) -> Snapshot:
        """Read every address of `mapping_areas(limit=limit)` once: each ends in the snapshot with
        its raw word, or as missing where the unit has nothing. Nothing else is read, and nothing
        is written.

        It reads in requests as large as the model reads; after a request that held an address
        the unit has nothing at, from one address at a time again, doubling while every one
        answers. It runs as a polling pass of its own, through the device's requests, paced and
        tried again when busy as every read is. Registers answered with only zeros where they held
        values are read again when the device says, until it takes the zeros or values come.

        Raises:
            NotConnectedError: the client is not connected.
            ValueError: `limit` is below 1.
            CannotConnectError: a request was answered busy after its tries, left unanswered, or
                answered in a way that could not be read; the read stops there.
        """
        self._require_connected()
        model = self._require_model().model
        areas(model, limit)
        mapper = Mapper(model, self._read_raw, self._clock.sleep)
        await self._passes.run(lambda: mapper.map(limit), due=lambda: True)
        return Snapshot(self._clock.now(), mapper.words, mapper.missing)

    async def read_mapped(self, found: Snapshot) -> Snapshot:
        """Read again, as raw words, every register `found` holds a word for, as many in one
        request as the model reads, as a polling pass of its own.

        Raises:
            NotConnectedError: the client is not connected.
            ValueError: `found` holds a register of a space the model does not read.
            CannotConnectError: a request was answered busy after its tries, left unanswered, or
                answered in a way that could not be read; the read stops there.
        """
        self._require_connected()
        mapper = Mapper(self._require_model().model, self._read_raw, self._clock.sleep)
        await self._passes.run(lambda: mapper.reread(found.values), due=lambda: True)
        return Snapshot(self._clock.now(), mapper.words, mapper.missing)

    async def _read_raw(self, points: Sequence[Point[Any]]) -> Mapping[str, ReadResult]:
        answers = await self._device.read(points)
        self._update_reachability(any(a.outcome is not Outcome.NO_ANSWER for a in answers.values()))
        return answers

    async def _read_and_publish(self, points: Sequence[Point[Any]]) -> None:
        marks = self._writes.marks()
        answers = await self._device.read(points)
        # Before publishing: a recovery makes everything due, and must not undo these reads.
        self._update_reachability(any(a.outcome is not Outcome.NO_ANSWER for a in answers.values()))
        self._publish(points, answers, since=marks)

    def _publish(self, points: Sequence[Point[Any]], answers: Mapping[str, ReadResult], *,
                 since: Mapping[str, int], scanning: bool = False) -> None:
        """Store and tell what was read. A register found missing is no longer read; outside a scan,
        what decides whether the unit has it is checked at once.

        A point written to since the `since` marks were taken is left as it is, still due: what
        was read may be from before the write.
        """
        scheduler = self._require_scheduler()
        for point in points:
            if self._writes.disturbed(point.key, since):
                continue
            answer = answers[point.key]
            previous = self._values.get(point.key)
            data = self._data_value(point, answer, previous)
            self._write_log.read(point.key, data)
            if data.quality is Quality.MISSING:
                reason = answer.detail or answer.outcome.name.lower()
                self._missing_by_read[point.key] = self._unavailable[point.key] = reason
                self._update_polling(point.key)
                check = self._checks.get(self._scope_of(point))
                if not scanning and check is not None:
                    now = self._clock.monotonic()
                    check.due = min(check.due, now)
                    # The read asked for it, so it runs without scheduled polling too.
                    self._checks_requested = now if self._checks_requested is None else min(self._checks_requested, now)
            success = answer.outcome is Outcome.OK and data.quality is not Quality.STALE
            scheduler.record(point.key, data, success=success, retry_after=answer.retry_after)
            self._store(point, data, previous)

    def _data_value(self, point: Point[Any], answer: ReadResult, previous: DataValue[Any] | None) -> DataValue[Any]:
        now = self._clock.now()
        if answer.outcome is Outcome.OK:
            try:
                value, quality = decode(point, answer.registers)
            except InvalidValueError as err:
                _LOGGER.error("'%s' answered registers that do not fit it: %s", point.key, err)
                return stale(previous, now)
            return DataValue(value, quality, now, raw=tuple(answer.registers))
        if answer.outcome in (Outcome.MISSING, Outcome.UNSUPPORTED):
            return DataValue(None, Quality.MISSING, now)
        if answer.outcome is Outcome.OFFLINE:
            return DataValue(None, Quality.OFFLINE, now)
        return stale(previous, now)

    def _store(self, point: Point[Any], data: DataValue[Any], previous: DataValue[Any] | None) -> None:
        self._values[point.key] = data
        self._subscriptions.report(point, data)
        trigger = point.on_change
        if trigger is not None and previous is not None and changed(previous, data, trigger.when):
            keys = [p.key for p in _select(self._require_model(), trigger.targets)
                    if p.readable and self._is_available(p.key)]
            if keys:
                self._require_scheduler().refresh(keys, after=trigger.after or 0.0, until_stable=trigger.until_stable)

    def _update_reachability(self, answered: bool) -> None:
        """Update reachability from whether the device answered.

        The device counts as not answering only once it has been silent for
        `offline_after_intervals` of the model's fastest interval, so one lost answer does not make
        it unavailable.
        """
        if answered:
            silent = self._reachability.silent
            self._reachability.answered(self._scheduler)
            if silent:
                self._wake.set()                    # what failed while it was silent is due at once
        else:
            self._reachability.unanswered()
        self._statuses.set(Status.CONNECTED, self._reachability.reachable)

    def _fresh_reachability(self) -> Reachability:
        """Reachability as of now, for the model chosen last.

        Before a model is chosen, nothing has answered, so the device does not yet count as
        answering whatever silence its model allows.
        """
        if self._model is None:
            return Reachability(self._clock, offline_after_intervals=self._offline_after_intervals,
                                fastest_interval=None)
        return Reachability(self._clock, offline_after_intervals=self._offline_after_intervals,
                            fastest_interval=self._model.timing.fastest_interval, name=self._model.name)

    # ============================================================================= writing

    def _as_written(self, point: Point[Any], value: object) -> DataValue[Any]:
        encoded = encode(point, value)
        if not encoded.registers:
            return DataValue(value, Quality.GOOD, self._clock.now())
        written, quality = decode(point, encoded.registers)
        return DataValue(written, quality, self._clock.now(), raw=encoded.registers)

    async def write[T](self, key: Key[T], value: T) -> bool:
        """
        Write `value` to `key` and return whether the device accepted it. Writes go out in
        order. A write the device answers `BUSY` is sent again as the model's `write_retry` says;
        a setting overtaken by a newer one before the device took it is not sent again, and
        returns the newer one's outcome.

        `key` is not read while it is being written. Once the device takes a setting, `key` holds
        the value written until it is read back.

        Raises:
            ReadOnlyError: the client is read-only.
            NotConnectedError: the client is not connected.
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

        Raises:
            NotConnectedError: the client is not connected.
        """
        checked: list[tuple[Point[Any], object]] = []
        for write in writes:
            point = self._writable_point(write.key)
            encode(point, write.value)
            checked.append((point, write.value))
        return await self._writes.write_sequence(checked)

    def _writable_point(self, key: Key[Any]) -> Point[Any]:
        if self._read_only:
            raise ReadOnlyError(f"refused to write '{key}': the client is read-only")
        self._require_connected()
        if not self.has(key):
            raise KeyError(f"this unit has no point {key!r}")
        self._check_type(key)
        point = self._point(key)
        if not point.writable:
            raise ValueError(f"'{key}' cannot be written")
        return point

    def _taken(self, point: Point[Any], value: object) -> None:
        """Hold the setting the device took, as it will read it, and schedule the read-back of the
        written point and of the points its write disturbs."""
        if point.write_kind is WriteKind.STATE and point.readable and self._is_available(point.key):
            self._store(point, self._as_written(point, value), self._values.get(point.key))
        scheduler = self._require_scheduler()
        resolved = self._require_model()
        timing = resolved.model.timing
        if point.readable:
            scheduler.refresh([point.key], after=timing.written_point_delay(point))
        effect = point.on_write
        if effect is not None:
            keys = [p.key for p in _select(resolved, effect.targets)
                    if p.readable and self._is_available(p.key)]
            if keys:
                scheduler.refresh(keys, after=timing.on_write_delay(point), until_stable=effect.until_stable)
        self._wake.set()

    # ============================================================================ internals

    def _require_connected(self) -> None:
        if not self._connected:
            raise NotConnectedError("the client is not connected; call connect() first")

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

    def _update_polling(self, key: str) -> None:
        """Tell the scheduler whether `key` takes part in scheduling."""
        if self._resolved is None or self._scheduler is None or key not in self._resolved.points:
            return
        point = self._resolved.points[key]
        if point.readable:
            self._scheduler.set_polled(key, should_poll(point, self._subscriptions, self._polled_keys)
                                       and self._is_available(key))
            self._wake.set()


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
    """The points of `resolved` that `targets` selects.

    Raises:
        TypeError: a single string.
        ValueError: no keys.
        KeyError: naming every key the model does not have.
    """
    return resolved.select(selector(targets))


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
    """The scan of the repeated section labelled `label`, which must have one."""
    return next(section.scan for section in model.sections
                if isinstance(section, RepeatedSection) and section.label == label and section.scan is not None)


def _check_interval(model: Model, scheduler: Scheduler) -> float | None:
    """Seconds between the checks of what each scan read: those of the model's `scan_check` rate;
    None when they are never checked on a timer."""
    return scheduler.rate_interval(model.scan_check) if model.scan_check is not None else None


def _replaced(point: Point[Any]) -> str | None:
    """The key the candidate `point` replaces; None for one that replaces none."""
    replaces = point.candidate.replaces if point.candidate is not None else None
    return str(replaces) if replaces is not None else None


def _union(marks: Iterable[Mapping[str, str]]) -> dict[str, str]:
    """Every key in any of `marks`, with the reason the first gives."""
    found: dict[str, str] = {}
    for marked in marks:
        for key, reason in marked.items():
            found.setdefault(key, reason)
    return found
