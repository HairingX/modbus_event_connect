"""Subscribers to a client's keys: who is told, and when a change is worth telling."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .point import Point
from .value import DataValue

_LOGGER = logging.getLogger(__name__)

ValueCallback = Callable[[str, DataValue | None, DataValue], None]
"""Called with the key, the previous value (None the first time) and the new value."""


@dataclass(eq=False)
class Subscriber:
    callback: ValueCallback
    polls: bool
    """Whether this subscriber also wants the key read on its schedule."""


class Subscriptions:
    """Who is subscribed to which key, and the last value each key reported."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._last_reported: dict[str, DataValue] = {}

    def add(self, key: str, callback: ValueCallback, *, polls: bool) -> Subscriber:
        subscriber = Subscriber(callback, polls)
        self._subscribers.setdefault(key, []).append(subscriber)
        return subscriber

    def remove(self, key: str, subscriber: Subscriber) -> None:
        subscribers = self._subscribers.get(key, [])
        if subscriber in subscribers:
            subscribers.remove(subscriber)
            if not subscribers:
                del self._subscribers[key]

    def keys(self) -> list[str]:
        return list(self._subscribers)

    def polled(self, key: str) -> bool:
        """Whether a subscriber wants `key` read on its schedule."""
        return any(subscriber.polls for subscriber in self._subscribers.get(key, []))

    def report(self, point: Point, data: DataValue) -> None:
        """Tell `point`'s subscribers about `data`, unless it is no news."""
        last = self._last_reported.get(point.key)
        if last is not None and not _worth_reporting(point, last, data):
            return
        self._last_reported[point.key] = data
        self.tell(point.key, last, data)

    def tell(self, key: str, old: DataValue | None, new: DataValue) -> None:
        """Tell every subscriber to `key` about a change, whether or not it is news."""
        for subscriber in list(self._subscribers.get(key, [])):
            tell(subscriber, key, old, new)

    def forget(self, key: str) -> None:
        """Forget what `key` last reported, so its next value is news."""
        self._last_reported.pop(key, None)


def tell(subscriber: Subscriber, key: str, old: DataValue | None, new: DataValue) -> None:
    """Call one subscriber; its error is logged and kept from the others."""
    try:
        subscriber.callback(key, old, new)
    except Exception:
        _LOGGER.exception("a subscriber to '%s' raised", key)


def _worth_reporting(point: Point, last: DataValue, new: DataValue) -> bool:
    """A change of quality always is; a change of value is when it clears the deadband."""
    if last.quality is not new.quality:
        return True
    if last.value == new.value:
        return False
    if point.deadband is None:
        return True
    before, after = last.value, new.value
    if isinstance(before, bool) or isinstance(after, bool) \
            or not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return True
    return abs(after - before) >= point.deadband
