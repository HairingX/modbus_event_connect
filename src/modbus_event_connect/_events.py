"""Subscribers to a client's keys: who is told, and when a change is worth telling."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._key import Key
from ._point import Point
from ._value import DataValue

_LOGGER = logging.getLogger(__name__)

type ValueCallback[T] = Callable[[Key[T], DataValue[T] | None, DataValue[T]], None]
"""Called with the key, the previous value (None the first time) and the new value."""


@dataclass(eq=False)
class Subscriber:
    key: Key[Any]
    """The key as the subscriber gave it, with the type it expects."""
    callback: ValueCallback[Any]
    polls: bool
    """Whether this subscriber also wants the key read on its schedule."""


class Subscriptions:
    """Who is subscribed to which key, and the last value each key reported."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._last_reported: dict[str, DataValue[Any]] = {}

    def add(self, key: Key[Any], callback: ValueCallback[Any], *, polls: bool) -> Subscriber:
        subscriber = Subscriber(key, callback, polls)
        self._subscribers.setdefault(key, []).append(subscriber)
        return subscriber

    def remove(self, key: str, subscriber: Subscriber) -> None:
        subscribers = self._subscribers.get(key, [])
        if subscriber in subscribers:
            subscribers.remove(subscriber)
            if not subscribers:
                del self._subscribers[key]

    def keys(self) -> list[Key[Any]]:
        """Every key subscribed to, as its first subscriber gave it."""
        return [subscribers[0].key for subscribers in self._subscribers.values()]

    def polled(self, key: str) -> bool:
        """Whether a subscriber wants `key` read on its schedule."""
        return any(subscriber.polls for subscriber in self._subscribers.get(key, []))

    def report(self, point: Point[Any], data: DataValue[Any]) -> None:
        """Tell `point`'s subscribers about `data`, unless it is no news."""
        last = self._last_reported.get(point.key)
        if last is not None and not _worth_reporting(point, last, data):
            return
        self._last_reported[point.key] = data
        for subscriber in list(self._subscribers.get(point.key, [])):
            # A subscriber whose key names another type than the point's would be handed values
            # it cannot take; the client reports it once, when it learns the model.
            if subscriber.key.type is point.key.type:
                tell(subscriber, last, data)

    def forget(self, key: str) -> None:
        """Forget what `key` last reported, so its next value is news."""
        self._last_reported.pop(key, None)


def tell(subscriber: Subscriber, old: DataValue[Any] | None, new: DataValue[Any]) -> None:
    """Call one subscriber with its own key; its error is logged and kept from the others."""
    try:
        subscriber.callback(subscriber.key, old, new)
    except Exception:
        _LOGGER.exception("a subscriber to '%s' raised", subscriber.key)


def _worth_reporting(point: Point[Any], last: DataValue[Any], new: DataValue[Any]) -> bool:
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
