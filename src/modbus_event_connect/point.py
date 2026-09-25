"""What a device has: its points, declared - nothing here performs I/O or decodes a register."""
from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field, replace
from enum import Enum, auto
from types import MappingProxyType
from typing import ClassVar, Hashable

from .data_type import ByteOrder, DataType, DataTypeKind, WordOrder
from .unit import Unit

# ================================================================================== enums


class PollRate(Enum):
    """How fresh a point must be kept."""
    FAST = auto()
    MEDIUM = auto()
    SLOW = auto()
    RARE = auto()
    STATIC = auto()
    """Read by the scan, when triggered or on request - never on a timer."""


DEFAULT_INTERVALS: Mapping[PollRate, float | None] = MappingProxyType({
    PollRate.FAST: 10.0,
    PollRate.MEDIUM: 30.0,
    PollRate.SLOW: 60.0,
    PollRate.RARE: 900.0,
    PollRate.STATIC: None,
})
"""Seconds between reads per poll rate, used when a model does not set its own."""


class WriteKind(Enum):
    """What a write means."""
    STATE = auto()
    """A setting. Writes that queue up collapse to the newest value."""
    COMMAND = auto()
    """An action. Every write is sent, in order, however many there are."""


class Change(Enum):
    """Which change of a trigger point sets off its refresh."""
    ANY = auto()
    RISING = auto()
    """The value increased - for a boolean, went from False to True."""
    FALLING = auto()


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


# ============================================================================== selectors


class Labels:
    """Selects every point carrying all of these labels: `Labels(room=3)`."""
    __slots__ = ("pairs",)

    def __init__(self, **labels: str | int) -> None:
        if not labels:
            raise ValueError("Labels() needs at least one label to select by")
        self.pairs: tuple[tuple[str, str | int], ...] = tuple(sorted(labels.items()))

    def matches(self, labels: Mapping[str, str | int]) -> bool:
        return all(labels.get(name) == value for name, value in self.pairs)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Labels) and other.pairs == self.pairs

    def __hash__(self) -> int:
        return hash(self.pairs)

    def __repr__(self) -> str:
        return "Labels(" + ", ".join(f"{name}={value!r}" for name, value in self.pairs) + ")"


Selector = Labels | tuple[str, ...]
"""Which points: those with these labels, or these keys."""


def _selector(targets: Labels | Sequence[str]) -> Selector:
    if isinstance(targets, Labels):
        return targets
    if isinstance(targets, str):
        # A bare string is a sequence of characters - almost certainly a mistake.
        raise TypeError(f"give keys as a list or tuple, not the single string {targets!r}")
    keys = tuple(targets)
    if not keys:
        raise ValueError("an empty list of keys selects nothing")
    return keys


# ======================================================================= behaviour parts


@dataclass(frozen=True)
class Limits:
    """What may be written, in the units a user sees. Also the entity's min / max / step."""
    min: float | None = None
    max: float | None = None
    step: float | None = None

    def __post_init__(self) -> None:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min {self.min} is greater than max {self.max}")
        if self.step is not None and not self.step > 0:
            raise ValueError(f"step must be positive, got {self.step}")


@dataclass(frozen=True)
class Transform:
    """A conversion applied after scaling on read, and before it on write. Always a pair."""
    read: Callable[[float], float]
    write: Callable[[float], float]
    name: str = ""


class Transforms:
    """Transforms most devices need."""
    INVERT_BOOL = Transform(read=lambda v: 1 - v, write=lambda v: 1 - v, name="invert_bool")
    """The device says 0 for on."""
    SECONDS_AS_MINUTES = Transform(read=lambda s: s / 60, write=lambda m: m * 60, name="seconds_as_minutes")
    MINUTES_AS_HOURS = Transform(read=lambda m: m / 60, write=lambda h: h * 60, name="minutes_as_hours")
    HOURS_AS_DAYS = Transform(read=lambda h: h / 24, write=lambda d: d * 24, name="hours_as_days")


@dataclass(frozen=True)
class Pulse:
    """A command that returns to its idle value by itself: write, wait `after`, write `idle`."""
    idle: float | int | bool
    after: float

    def __post_init__(self) -> None:
        if not self.after > 0:
            raise ValueError(f"a pulse needs a positive duration, got {self.after}")


@dataclass(frozen=True)
class Refresh:
    """Read `targets` again `after` seconds after this point is written, or changes.

    With `until_stable`, keep re-reading them every `after` seconds while they still change,
    for at most that many seconds.
    """
    targets: Selector
    after: float | None = None
    """Seconds to wait. None waits, after a write, as long as the written point takes to read
    back, and after a change not at all."""
    until_stable: float | None = None
    when: Change = Change.ANY
    """Which change sets it off; only a change has one."""

    def __init__(self, targets: Labels | Sequence[str], after: float | None = None,
                 until_stable: float | None = None, *, when: Change = Change.ANY) -> None:
        object.__setattr__(self, "targets", _selector(targets))
        object.__setattr__(self, "after", after)
        object.__setattr__(self, "until_stable", until_stable)
        object.__setattr__(self, "when", when)
        if after is not None and after < 0:
            raise ValueError(f"after cannot be negative, got {after}")
        if until_stable is not None and not until_stable > 0:
            raise ValueError(f"until_stable must be positive, got {until_stable}")
        if until_stable is not None and after is not None and not after > 0:
            raise ValueError("until_stable needs a positive after to re-read at")


# ================================================================================== point


@dataclass(frozen=True, eq=False)
class Point:
    """One value a device has: a read side, a write side, or both, sharing type, scale and unit.

    `key` must never change once a consumer has stored it.
    """
    key: str
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
    no_data: Collection[int] = ()
    """Raw values meaning "no reading" - a missing sensor answering 0x7FFF, for instance."""
    raw_range: tuple[int, int] | None = None
    """Raw values outside this range read as no data."""
    limits: Limits | None = None
    unit: Unit | None = None
    poll_rate: PollRate = PollRate.MEDIUM
    poll_always: bool = False
    """Read even when no consumer asked for it - a value the device model itself depends on."""
    deadband: float | None = None
    """Changes smaller than this, from the last value reported, are not reported."""
    write_kind: WriteKind = WriteKind.STATE
    pulse: Pulse | None = None
    read_back_after: float | None = None
    """Seconds from a write until a read shows it, where this point differs from its device."""
    on_write: Refresh | None = None
    on_change: Refresh | None = None
    labels: Mapping[str, str | int] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "no_data", frozenset(self.no_data))
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))
        if self.on_change is not None and self.on_change.after is None:
            object.__setattr__(self, "on_change", replace(self.on_change, after=0.0))
        problems = list(_problems(self))
        if problems:
            raise ValueError(f"point {self.key!r}: " + "; ".join(problems))

    @property
    def readable(self) -> bool:
        return self.read is not None

    @property
    def writable(self) -> bool:
        return self.write is not None

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


def _problems(point: Point) -> list[str]:
    """Everything wrong with a point, so one error names every mistake at once."""
    return [*_shape_problems(point), *_value_problems(point), *_read_problems(point), *_write_problems(point)]


def _shape_problems(point: Point) -> list[str]:
    found: list[str] = []
    if not point.key or point.key != point.key.strip():
        found.append("the key must be a non-empty string without surrounding spaces")
    if point.read is None and point.write is None:
        found.append("it needs a read side, a write side, or both")
    if point.write is not None and not point.write.writable:
        found.append(f"{type(point.write).__name__} cannot be written")
    for side, access in (("read", point.read), ("write", point.write)):
        if access is not None and access.bits and point.data_type.kind is not DataTypeKind.BOOL:
            found.append(f"the {side} side is in {type(access).__name__}, where every address is "
                         f"one bit, so the data type must be BOOL, not {point.data_type!r}")
    if any(not name for name in point.labels):
        found.append("a label needs a name")
    return found


def _value_problems(point: Point) -> list[str]:
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
        if point.transform is not None:
            found.append(f"a transform applies to numbers, not to {data_type!r}")
        if point.deadband is not None:
            found.append(f"a deadband applies to numbers, not to {data_type!r}")
        if point.limits is not None:
            found.append(f"limits apply to numbers, not to {data_type!r}")
    return found


def _read_problems(point: Point) -> list[str]:
    found: list[str] = []
    data_type = point.data_type
    if point.no_data or point.raw_range is not None:
        if not point.readable:
            found.append("no_data and raw_range describe reads, but it has no read side")
        if not data_type.is_integer:
            found.append(f"no_data and raw_range compare raw integers, not {data_type!r}"
                         + (" (a float reads NaN and infinity as no data by itself)"
                            if data_type.is_float else ""))
    if point.raw_range is not None and point.raw_range[0] > point.raw_range[1]:
        found.append(f"raw_range {point.raw_range} is empty")
    if point.deadband is not None:
        if point.deadband < 0:
            found.append(f"deadband cannot be negative, got {point.deadband}")
        if not point.readable:
            found.append("a deadband filters reads, but it has no read side")
    if point.poll_always and not point.readable:
        found.append("poll_always=True asks for reads, but it has no read side")
    if point.on_change is not None:
        if not point.readable:
            found.append("on_change reacts to reads, but it has no read side")
        if point.on_change.until_stable is not None and not point.on_change.after:
            found.append("on_change re-reads until stable, which needs a positive after to re-read at")
    return found


def _write_problems(point: Point) -> list[str]:
    found: list[str] = []
    if point.read_back_after is not None:
        if not point.writable:
            found.append("read_back_after describes writes, but it has no write side")
        if not (math.isfinite(point.read_back_after) and point.read_back_after >= 0):
            found.append(f"read_back_after must be a finite number >= 0, got {point.read_back_after}")
    if point.limits is not None and not point.writable:
        found.append("limits describe writes, but it has no write side")
    if point.write_kind is WriteKind.COMMAND and not point.writable:
        found.append("a COMMAND is written, but it has no write side")
    if point.pulse is not None and point.write_kind is not WriteKind.COMMAND:
        found.append("only a COMMAND can pulse")
    if point.on_write is not None:
        if not point.writable:
            found.append("on_write re-reads after a write, but it has no write side")
        if point.on_write.when is not Change.ANY:
            found.append("on_write has no change to wait for; `when` belongs to on_change")
        after = point.on_write.after if point.on_write.after is not None else point.read_back_after
        if point.on_write.until_stable is not None and after is not None and not after > 0:
            found.append("on_write re-reads until stable, which needs a positive after to re-read at")
    return found
