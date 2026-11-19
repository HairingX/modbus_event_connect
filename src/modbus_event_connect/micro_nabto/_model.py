"""A model of a micro_nabto unit: its points, and how it is talked to over micro_nabto."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from event_connect import Fixed, Wait

from . import _wire as wire
from ._access import DatapointRegister, SetpointRegister
from .._data_type import DataTypeKind
from .._model import Model
from .._point import Access, Point

MAX_REGISTERS_PER_READ = (wire.MAX_DATAGRAM - wire.EMPTY_DATAPOINT_READ) // wire.DATAPOINT_ITEM
"""The most registers one read can ask for: its request, at five bytes a register, fits one UDP
datagram. A device may answer fewer."""

_MAX_OBJECT = 0xFF
_MAX_STATUS = 0xFF
"""A write's status is the first byte of its answer."""
_MAX_ADDRESS: Mapping[tuple[str, type[Access]], int] = {
    ("read", DatapointRegister): 0xFFFF_FFFF,
    ("read", SetpointRegister): 0xFFFF,
    ("write", SetpointRegister): 0xFFFF_FFFF,
}
"""SetpointRegister reads carry a two-byte address; datapoint reads and setpoint writes four."""


@dataclass(frozen=True, kw_only=True)
class RereadZeros:
    """How registers answered with only zeros, where they held values, are read again before the
    zeros are taken; until then their last values are kept."""
    within: float
    """Seconds after the zeros until they are taken."""
    wait: Wait
    """When to read them again: the first time this long after the zeros, each next time this long
    after the time before."""
    at_once: bool
    """Whether they are read again at once, before anything else."""
    halves: bool
    """Whether, at `within`, they are read in two halves, whose values are taken if they bring
    any: a device may answer only zeros when asked for too many registers at once."""

    def __post_init__(self) -> None:
        if not self.within >= 0:
            raise ValueError(f"within cannot be negative, got {self.within}")


TAKE_ZEROS = RereadZeros(within=0.0, wait=Fixed(0.0), at_once=False, halves=False)
"""Zeros are taken as they come: for a device known to answer zeros only where they are."""


@dataclass(frozen=True, eq=False, kw_only=True)
class MicroNabtoModel(Model):
    """A model of a unit reached over micro_nabto: its points, and how it is talked to.

    Raises:
        ModelError: at construction, listing every problem; besides the model's own, a
            `max_registers` outside what one datagram carries, a wait that is not a finite
            number of seconds it can wait, and `write_taken` empty or holding a status no answer
            can carry.
    """
    max_registers: int
    """Most registers the unit is asked for in one read: 1 to `MAX_REGISTERS_PER_READ`."""
    answer_wait: float
    """Seconds an answer is waited for once the unit has said it has the request; never shorter
    than the connection's timeout."""
    later_answer_wait: float
    """Seconds a read sent again and answered with only zeros, where values were, is kept open
    for a later answer."""
    zeros: RereadZeros
    """How an answer of only zeros, where values were, is read again before the zeros are taken."""
    write_taken: frozenset[int]
    """The statuses, 0 to 255, the unit answers a setpoint write it took with; any other status
    means it did not take the write."""

    def declaration_problems(self, points: Sequence[Point[Any]]) -> list[str]:
        """What is wrong with the model over micro_nabto: a count, a wait or a write status it
        cannot have."""
        found: list[str] = []
        if not 1 <= self.max_registers <= MAX_REGISTERS_PER_READ:
            found.append(f"max_registers must be 1-{MAX_REGISTERS_PER_READ}, got {self.max_registers}")
        if not (math.isfinite(self.answer_wait) and self.answer_wait > 0):
            found.append(f"answer_wait must be a finite number above 0, got {self.answer_wait}")
        if not (math.isfinite(self.later_answer_wait) and self.later_answer_wait >= 0):
            found.append(f"later_answer_wait must be a finite number >= 0, got {self.later_answer_wait}")
        if not self.write_taken:
            found.append("write_taken must name at least one status")
        outside = sorted(status for status in self.write_taken if not 0 <= status <= _MAX_STATUS)
        if outside:
            found.append(f"write_taken must hold statuses 0-{_MAX_STATUS}, got {outside}")
        return found

    def address(self, access: Access) -> int:
        """The address sent for `access`: the one it carries."""
        return access.address

    def problems(self, point: Point[Any]) -> list[str]:
        """What is wrong with `point` over micro_nabto: a side outside its two spaces or past their
        addresses, a read larger than one request of this unit may be, or a bit to write."""
        found: list[str] = []
        for side, access in (("read", point.read), ("write", point.write)):
            if access is None:
                continue
            if not isinstance(access, DatapointRegister | SetpointRegister):
                found.append(f"the {side} side is {type(access).__name__}, not a micro_nabto space")
                continue
            limit = _MAX_ADDRESS.get((side, type(access)))
            if limit is not None and access.address + point.registers - 1 > limit:
                found.append(f"the {side} side ends past address {limit}")
            if access.obj > _MAX_OBJECT:
                found.append(f"the {side} side's object {access.obj} is more than {_MAX_OBJECT}")
            if side == "read" and point.registers > self.max_registers:
                found.append(f"the read side spans {point.registers}, more than the {self.max_registers} "
                             f"the unit is asked for in one request")
        if point.write is not None and point.data_type.kind is DataTypeKind.BIT:
            found.append("a single bit cannot be written over micro_nabto")
        return found
