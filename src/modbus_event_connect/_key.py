"""A point's key: the text a consumer stores, carrying the type of the point's value."""
from __future__ import annotations

from enum import IntEnum
from typing import Any, TypeGuard


class Key[T](str):
    """A point's key. At runtime it is its text: equal to it, hashed as it, stored as it.

    `Key("temperature", float)` names a point whose value is a float, and carries that type
    wherever the key goes. The type is one of `bool`, `int`, `float`, `str`, or an `IntEnum`
    naming the states of an integer.
    """
    type: type[T]
    """The type of the point's value."""

    def __new__(cls, text: str, type: type[T]) -> Key[T]:
        key = super().__new__(cls, text)
        key.type = type
        return key

    def __reduce__(self) -> tuple[Any, ...]:
        return (Key, (str(self), self.type))



def is_key(value: object) -> bool:
    """Whether `value` is a Key, not merely its text."""
    return isinstance(value, Key)


def is_state_type(value_type: type[object]) -> TypeGuard[type[IntEnum]]:
    """Whether `value_type` names the states of an integer: an `IntEnum`."""
    return issubclass(value_type, IntEnum)
