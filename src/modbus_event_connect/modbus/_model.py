"""A model of a Modbus unit: its points, and how it is talked to over Modbus."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from event_connect import Retry

from ._access import (
    MAX_ADDRESS,
    MAX_BITS_PER_READ,
    MAX_REGISTERS_PER_READ,
    MAX_REGISTERS_PER_WRITE,
    SPACES,
    BitWrite,
    HoldingRegister,
    RegisterNumbering,
    SingleWrite,
)
from .._data_type import DataTypeKind
from .._model import Model
from .._point import Access, Point


@dataclass(frozen=True, eq=False, kw_only=True)
class ModbusModel(Model):
    """A model of a unit reached over Modbus: its points, and how it is talked to.

    Raises:
        ModelError: at construction, listing every problem; besides the model's own, a count
            outside what Modbus allows, an answer time that is not a finite number above 0, and a
            `bit_write` missing where a point writes one bit of a holding register, or given
            where none does.
    """
    numbering: RegisterNumbering
    """How the model's manual numbers registers."""
    max_registers: int
    """Most registers the unit accepts in one read: 1 to the 125 Modbus allows."""
    max_bits: int
    """Most coils or discrete inputs the unit accepts in one read: 1 to the 2000 Modbus allows."""
    single_write: SingleWrite
    """How one holding register is written."""
    bit_write: BitWrite | None
    """How one bit inside a holding register is written; None for a model with no point that
    writes one."""
    busy: Retry
    """How a read answered "busy" (0x06) is tried again; a write so answered is sent again as
    `write_retry` says."""
    answers_within: float
    """Seconds the unit takes at most to answer a request; a request waits this, and the network's
    margin the device is given."""

    def declaration_problems(self, points: Sequence[Point[Any]]) -> list[str]:
        """What is wrong with the model over Modbus: counts and an answer time it cannot have, and
        `bit_write` missing where a point writes one bit of a holding register, or given where
        none does."""
        found: list[str] = []
        if not 1 <= self.max_registers <= MAX_REGISTERS_PER_READ:
            found.append(f"max_registers must be 1-{MAX_REGISTERS_PER_READ}, got {self.max_registers}")
        if not 1 <= self.max_bits <= MAX_BITS_PER_READ:
            found.append(f"max_bits must be 1-{MAX_BITS_PER_READ}, got {self.max_bits}")
        if not (math.isfinite(self.answers_within) and self.answers_within > 0):
            found.append(f"answers_within must be a finite number above 0, got {self.answers_within}")
        bit_writers = list(dict.fromkeys(repr(str(point.key)) for point in points if _writes_a_bit(point)))
        if bit_writers and self.bit_write is None:
            found.append(f"{', '.join(bit_writers)} {'writes' if len(bit_writers) == 1 else 'write'} one bit of "
                         f"a holding register, so the model needs a bit_write")
        if not bit_writers and self.bit_write is not None:
            found.append(f"bit_write is {self.bit_write.name}, but no point writes one bit of a holding register")
        return found

    def read_limit(self, access: Access) -> int:
        """`max_bits` for a coil or discrete input, `max_registers` for a register."""
        return self.max_bits if access.bits else self.max_registers

    def numbers(self, access: Access) -> tuple[range, ...]:
        """The numbers `numbering` gives the space of `access`, lowest first."""
        ranges = sorted(self.numbering.ranges(type(access)), key=lambda numbers: numbers.first)
        return tuple(range(numbers.first, numbers.last + 1) for numbers in ranges)

    def address(self, access: Access) -> int:
        """The address sent for `access`, as `numbering` gives it.

        Raises:
            ValueError: it has none.
        """
        return self.numbering.address(access)

    def problems(self, point: Point[Any]) -> list[str]:
        """What is wrong with `point` over Modbus: a side outside the four tables or the address
        space, or larger than one request of this unit may be."""
        found: list[str] = []
        for side, access in (("read", point.read), ("write", point.write)):
            if access is None:
                continue
            if type(access) not in SPACES:
                found.append(f"the {side} side is {type(access).__name__}, which is not a Modbus space")
                continue
            try:
                start = self.address(access)
            except ValueError as err:
                found.append(f"the {side} side: {err}")
                continue
            span = 1 if access.bits else point.registers
            if start + span - 1 > MAX_ADDRESS:
                found.append(f"the {side} side runs past the end of the address space")
            limit = self.max_bits if access.bits else self.max_registers
            if span > limit:
                found.append(f"the {side} side spans {span}, more than the {limit} the unit "
                             f"accepts in one request")
        if point.write is not None and not point.write.bits and point.registers > MAX_REGISTERS_PER_WRITE:
            found.append(f"a write of {point.registers} registers exceeds the Modbus limit of "
                         f"{MAX_REGISTERS_PER_WRITE}")
        return found


def _writes_a_bit(point: Point[Any]) -> bool:
    """Whether `point` writes one bit of a holding register."""
    return isinstance(point.write, HoldingRegister) and point.data_type.kind is DataTypeKind.BIT
