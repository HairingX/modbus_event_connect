"""What a device has: its points, declared - nothing here performs I/O or decodes a register.

How a point behaves - when it is read, what may be written, what a write or a change sets off -
is event_connect's; a point here adds where it lives in Modbus or micro_nabto and how its
registers become a value.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import KW_ONLY, dataclass
from datetime import date, datetime
from enum import IntEnum
from types import MappingProxyType
from typing import Any, ClassVar, Hashable

from event_connect import (
    DEFAULT_INTERVALS,
    VALUE_TYPES,
    Change,
    Labels,
    Limits,
    PollRate,
    Pulse,
    Refresh,
    Selector,
    Transform,
    Transforms,
    WriteKind,
    is_key,
    is_state_type,
)
from event_connect import Point as BasePoint

from ._data_type import ByteOrder, DataType, DataTypeKind, WordOrder, raw_bounds

__all__ = [
    "DEFAULT_INTERVALS",
    "Access",
    "Change",
    "Labels",
    "Limits",
    "Point",
    "PollRate",
    "Pulse",
    "Refresh",
    "Selector",
    "Transform",
    "Transforms",
    "WriteKind",
]


# ================================================================================= access


@dataclass(frozen=True)
class Access:
    """Where one side of a point lives; concrete kinds are defined per protocol."""
    address: int
    """The address, or a register number the protocol's options turn into one."""

    writable: ClassVar[bool] = False
    """Whether this space accepts writes."""
    bits: ClassVar[bool] = False
    """Whether each address holds one bit rather than a 16-bit register."""

    def __post_init__(self) -> None:
        if self.address < 0:
            raise ValueError(f"an address cannot be negative, got {self.address}")

    @property
    def space(self) -> Hashable:
        """Identifies the address space; two accesses can share a request only when it's equal."""
        return type(self)


# ================================================================================== point


@dataclass(frozen=True, eq=False)
class Point[T](BasePoint[T]):
    """One value a device has: a read side, a write side, or both, sharing type, scale and unit.

    `key` must never change once a consumer has stored it. Its type is the type of the value:
    `bool` for a boolean, `str` for text, `int` for an integer that stays one after scaling,
    `float` for any number, and an `IntEnum` for an integer naming states.
    """
    _: KW_ONLY
    read: Access | None = None
    write: Access | None = None
    data_type: DataType = DataType.UINT16
    word_order: WordOrder = WordOrder.HIGH_FIRST
    byte_order: ByteOrder = ByteOrder.BIG
    scale: float = 1
    """value = raw * scale + offset. Scale 0.1 reads a raw 215 as 21.5."""
    offset: float = 0
    precision: int | None = None
    """Decimals to round a read value to.

    By default, enough for `scale` and `offset`; none for a float or a transform.
    """
    transform: Transform | None = None
    codes: Mapping[int, IntEnum] | None = None
    """What each raw code means, for a key naming states: a code the table lacks reads as NO_DATA,
    and a state it lacks cannot be written. Devices that number the same states differently each
    map theirs onto one shared IntEnum. Several codes may name one state on a point that is only
    read. None: each state is its own raw code."""
    valid_raw: Collection[int] | None = None
    """The raw numbers - what the device sends, before scale and offset - that are values; any
    other reads as NO_DATA, and cannot be written. None: every number is a value.

    A `range` costs nothing however wide: `range(0, 0xFFFF)` is 0 to 0xFFFE, leaving out the
    0xFFFF a device sends when it has no reading; `range(0, 2)` is a switch's 0 and 1.
    For an integer or a BOOL register.
    """

    def __post_init__(self) -> None:
        if self.valid_raw is not None and not isinstance(self.valid_raw, range):
            object.__setattr__(self, "valid_raw", frozenset(self.valid_raw))
        if self.codes is not None:
            object.__setattr__(self, "codes", MappingProxyType(dict(self.codes)))
        super().__post_init__()

    @property
    def readable(self) -> bool:
        """Whether the point has a read side."""
        return self.read is not None

    @property
    def writable(self) -> bool:
        """Whether the point has a write side."""
        return self.write is not None

    @property
    def states(self) -> tuple[IntEnum, ...]:
        """The states this point can hold, in the order its IntEnum declares them: those its codes
        name, or every state; none for a key that names no states."""
        value_type: type[object] = self.key.type
        if not is_state_type(value_type):
            return ()
        named = set(self.codes.values()) if self.codes is not None else set(value_type)
        return tuple(state for state in value_type if state in named)

    @property
    def registers(self) -> int:
        """How many registers - or bits, in a bit space - one side of this point spans."""
        return self.data_type.registers

    @property
    def effective_precision(self) -> int | None:
        """The rounding applied to a read value; None means none."""
        if self.precision is not None:
            return self.precision
        if self.transform is not None or not self.data_type.is_integer:
            return None
        return max(_decimals(self.scale), _decimals(self.offset))

    def problems(self) -> list[str]:
        """Everything wrong with the point, as a point and as registers or bits."""
        return [*super().problems(), *_access_problems(self), *_type_problems(self),
                *_value_problems(self), *_valid_raw_problems(self)]

    def __repr__(self) -> str:
        sides = ", ".join(f"{name}={access!r}" for name, access in
                          (("read", self.read), ("write", self.write)) if access is not None)
        return f"Point({self.key!r}, {sides}, data_type={self.data_type!r})"


def _decimals(number: float) -> int:
    """Decimals needed to write `number` exactly as it was typed: 0.01 -> 2, 0.5 -> 1, 10 -> 0."""
    text = repr(float(number))
    if "e" in text or "E" in text:
        mantissa, exponent = text.lower().split("e")
        digits = len(mantissa.split(".")[1].rstrip("0")) if "." in mantissa else 0
        return max(0, digits - int(exponent))
    fraction = text.split(".")[1].rstrip("0") if "." in text else ""
    return len(fraction)


def _access_problems(point: Point[Any]) -> list[str]:
    found: list[str] = []
    if point.write is not None and not point.write.writable:
        found.append(f"{type(point.write).__name__} cannot be written")
    for side, access in (("read", point.read), ("write", point.write)):
        if access is not None and access.bits and point.data_type.kind is not DataTypeKind.BOOL:
            found.append(f"the {side} side is in {type(access).__name__}, where every address is "
                         f"one bit, so the data type must be BOOL, not {point.data_type!r}")
    return found


def _type_problems(point: Point[Any]) -> list[str]:
    """Whether the registers can hold the type the key names."""
    if not is_key(point.key):
        return []
    value_type: type[object] = point.key.type
    data_type = point.data_type
    if point.codes is not None and not is_state_type(value_type):
        return [f"codes name states, and {value_type.__name__} names none"]
    if is_state_type(value_type):
        found: list[str] = []
        if not data_type.is_integer:
            found.append(f"the states of {value_type.__name__} are integers, not {data_type!r}")
        if point.scale != 1 or point.offset != 0 or point.transform is not None:
            found.append(f"the states of {value_type.__name__} are the raw integer, or its codes: no scale, "
                         f"offset or transform")
        if point.limits is not None:
            found.append(f"the states of {value_type.__name__} are what may be written: no limits")
        if point.codes is not None:
            found += _code_problems(point.codes, value_type, data_type, writable=point.writable)
        return found
    if data_type.is_temporal:
        wanted = datetime if data_type.kind is DataTypeKind.DOS_DATETIME else date
        return [] if value_type is wanted else [f"{data_type!r} holds a {wanted.__name__}, not {value_type.__name__}"]
    if value_type is bool:
        return [] if data_type.is_boolean else [f"a bool is held by BOOL or a bit, not {data_type!r}"]
    if value_type is str:
        return [] if data_type.kind is DataTypeKind.STRING else [f"text is held by a STRING, not {data_type!r}"]
    if value_type is int:
        if data_type.is_integer and point.effective_precision == 0:
            return []
        return [f"an int needs an integer data type that stays whole after scale, offset and precision; "
                f"use float, or precision=0 (got {data_type!r}, scale {point.scale}, offset {point.offset})"]
    if value_type is float:
        return [] if data_type.is_numeric else [f"a float is held by a number, not {data_type!r}"]
    if value_type in (date, datetime):
        return [f"a {value_type.__name__} is held by DOS_DATE or DOS_DATETIME, not {data_type!r}"]
    if value_type in VALUE_TYPES:
        return [f"no register holds a {value_type.__name__}"]
    return []


def _code_problems(codes: Mapping[int, IntEnum], states: type[IntEnum], data_type: DataType, *,
                   writable: bool) -> list[str]:
    found: list[str] = []
    foreign = [state for state in codes.values() if not isinstance(state, states)]
    if foreign:
        found.append(f"codes name {foreign[0]!r}, which is not a state of {states.__name__}")
    if writable and len(set(codes.values())) != len(codes):
        found.append("two codes name the same state, so it could not be written")
    bounds = raw_bounds(data_type.kind)
    if bounds is not None and any(not bounds[0] <= code <= bounds[1] for code in codes):
        found.append(f"a code lies outside {data_type!r}'s range {bounds[0]}..{bounds[1]}")
    return found


def _value_problems(point: Point[Any]) -> list[str]:
    found: list[str] = []
    data_type = point.data_type
    if not math.isfinite(point.scale) or point.scale == 0:
        found.append(f"scale must be a finite, non-zero number, got {point.scale}")
    if not math.isfinite(point.offset):
        found.append(f"offset must be finite, got {point.offset}")
    if point.precision is not None and point.precision < 0:
        found.append(f"precision cannot be negative, got {point.precision}")
    if not data_type.is_numeric:
        if point.scale != 1 or point.offset != 0:
            found.append(f"scale and offset apply to numbers, not to {data_type!r}")
        if point.transform is not None and not data_type.is_boolean:
            found.append(f"a transform applies to numbers, a BOOL or a bit, not to {data_type!r}")
        if point.transform is not None and data_type.is_boolean and not _keeps_bits(point.transform):
            found.append(f"a transform on {data_type!r} must turn 0 and 1 into 0 and 1, both ways")
    return found


def _keeps_bits(transform: Transform) -> bool:
    try:
        return all(transform.read(b) in (0, 1) and transform.write(b) in (0, 1) for b in (0, 1))
    except ArithmeticError:
        return False


def _valid_raw_problems(point: Point[Any]) -> list[str]:
    valid = point.valid_raw
    if valid is None:
        return []
    data_type = point.data_type
    bounds = raw_bounds(data_type.kind)
    if bounds is None:
        return [f"valid_raw names raw integers, which {data_type!r} does not hold"
                + (" (a float reads NaN and infinity as no data by itself)" if data_type.is_float else "")]
    if len(valid) == 0:
        return ["valid_raw is empty, so nothing could ever be a value"]
    if any(access is not None and access.bits for access in (point.read, point.write)):
        bounds = (0, 1)
    low, high = (min(valid[0], valid[-1]), max(valid[0], valid[-1])) if isinstance(valid, range) else (min(valid), max(valid))
    if low < bounds[0] or high > bounds[1]:
        return [f"valid_raw reaches {low}..{high}, but {data_type!r} holds {bounds[0]}..{bounds[1]} here"]
    return []
