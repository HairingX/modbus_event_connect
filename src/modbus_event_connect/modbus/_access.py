"""Modbus address spaces, and what a model states in Modbus terms."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, ClassVar, Literal

from .._device import ProtocolOptions
from .._point import Access, Point

MAX_ADDRESS = 0xFFFF

MAX_REGISTERS_PER_READ = 125
"""The Modbus limit for reading registers (0x03, 0x04). Devices may accept fewer."""
MAX_BITS_PER_READ = 2000
"""The Modbus limit for reading coils and discrete inputs (0x01, 0x02)."""
MAX_REGISTERS_PER_WRITE = 123
"""The Modbus limit for Write Multiple Registers (0x10)."""


@dataclass(frozen=True)
class InputRegister(Access):
    """InputRegister register - function code 0x04, read-only, 16 bits."""


@dataclass(frozen=True)
class HoldingRegister(Access):
    """HoldingRegister register - 0x03 to read, 0x06 / 0x10 / 0x16 to write, 16 bits."""
    writable: ClassVar[bool] = True


@dataclass(frozen=True)
class DiscreteInput(Access):
    """DiscreteInput input - 0x02, read-only, one bit."""
    bits: ClassVar[bool] = True


@dataclass(frozen=True)
class Coil(Access):
    """Coil - 0x01 to read, 0x05 / 0x0F to write, one bit."""
    writable: ClassVar[bool] = True
    bits: ClassVar[bool] = True


_TABLES: tuple[type[Access], ...] = (Coil, DiscreteInput, InputRegister, HoldingRegister)


@dataclass(frozen=True)
class NumberRange:
    """Register numbers `first` to `last` of one table, where number `first` is `address`."""
    first: int
    last: int
    address: int

    def __post_init__(self) -> None:
        if not 0 <= self.first <= self.last:
            raise ValueError(f"a range runs from first to last, got {self.first}-{self.last}")
        if self.address < 0 or self.address + self.last - self.first > MAX_ADDRESS:
            raise ValueError(f"numbers {self.first}-{self.last} from address {self.address} "
                             f"do not fit in addresses 0-{MAX_ADDRESS}")

    def __contains__(self, number: object) -> bool:
        return isinstance(number, int) and self.first <= number <= self.last

    def __str__(self) -> str:
        return f"{self.first}-{self.last} from address {self.address}"


class RegisterNumbering:
    """How a manual numbers the registers of each table, as ranges of numbers.

    A register whose number is in none of its table's ranges has no address.
    """

    __slots__ = ("_ranges",)

    def __init__(self, *, coils: Sequence[NumberRange] = (), discrete_inputs: Sequence[NumberRange] = (),
                 input_registers: Sequence[NumberRange] = (),
                 holding_registers: Sequence[NumberRange] = ()) -> None:
        """Raises:
            ValueError: two ranges of one table share a number.
        """
        self._ranges: Mapping[type[Access], tuple[NumberRange, ...]] = {
            Coil: tuple(coils), DiscreteInput: tuple(discrete_inputs),
            InputRegister: tuple(input_registers), HoldingRegister: tuple(holding_registers)}
        for table, ranges in self._ranges.items():
            ordered = sorted(ranges, key=lambda r: r.first)
            for before, after in zip(ordered, ordered[1:]):
                if after.first <= before.last:
                    raise ValueError(f"{table.__name__} ranges {before.first}-{before.last} and "
                                     f"{after.first}-{after.last} share numbers")

    def ranges(self, table: type[Access]) -> tuple[NumberRange, ...]:
        return self._ranges.get(table, ())

    def address(self, access: Access) -> int:
        """The address of the register `access` numbers. Raises ValueError if it has none."""
        ranges = self.ranges(type(access))
        for numbers in ranges:
            if access.address in numbers:
                return numbers.address + access.address - numbers.first
        listed = "; ".join(str(numbers) for numbers in ranges) or "none"
        raise ValueError(f"{access!r} is in none of the {type(access).__name__} ranges ({listed})")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RegisterNumbering) and other._ranges == self._ranges

    def __hash__(self) -> int:
        return hash(tuple(self._ranges.items()))

    def __repr__(self) -> str:
        return f"RegisterNumbering({dict((t.__name__, r) for t, r in self._ranges.items() if r)})"


def plain(*, first_address: Literal[0, 1]) -> RegisterNumbering:
    """Register numbers without a table digit, register 1 being `first_address` in every table.

    With 1, each number is the address itself, 0 to 65535. With 0, numbers run 1 to 65536, as
    the Modbus Application Protocol specification (V1.1b3, 4.4) numbers its data model.
    """
    if first_address not in (0, 1):
        raise ValueError(f"register 1 is address 0 or 1, not {first_address}")
    first = 1 - first_address
    numbers = [NumberRange(first, first + MAX_ADDRESS, address=0)]
    return RegisterNumbering(coils=numbers, discrete_inputs=numbers, input_registers=numbers,
                             holding_registers=numbers)


def modicon(*, digits: Literal[5, 6], first_address: Literal[0, 1]) -> RegisterNumbering:
    """Modicon reference numbers: 0xxxx coils, 1xxxx discrete inputs, 3xxxx input registers and
    4xxxx holding registers, the first number of each table being `first_address`.

    Modicon's protocol reference guide (PI-MBUS-300) writes five digits, 40001 being address 0.
    Kepware's Modbus drivers also write six, 400001 to 465536, and take 40001 as address 1 when
    their "Zero-Based Addressing" setting is off. Numbers whose address would pass 65535 are left
    out.
    """
    if digits not in (5, 6):
        raise ValueError(f"Modicon reference numbers have 5 or 6 digits, not {digits}")
    if first_address not in (0, 1):
        raise ValueError(f"a Modicon table starts at address 0 or 1, not {first_address}")
    table_size = 9999 if digits == 5 else MAX_ADDRESS + 1
    count = min(table_size, MAX_ADDRESS + 1 - first_address)

    def table(leading_digit: int) -> list[NumberRange]:
        first = leading_digit * 10 ** (digits - 1) + 1
        return [NumberRange(first, first + count - 1, address=first_address)]
    return RegisterNumbering(coils=table(0), discrete_inputs=table(1), input_registers=table(3),
                             holding_registers=table(4))


class SingleWrite(Enum):
    """How one holding register is written."""
    FC06 = auto()
    """Write Single Register. The default."""
    FC16 = auto()
    """Write Multiple Registers with a count of one - for devices that only implement 0x10."""


class BitWrite(Enum):
    """How one bit inside a holding register is written."""
    MASK = auto()
    """Mask Write Register (0x16): atomic on the device, neighbouring bits untouched. The default."""
    READ_MODIFY_WRITE = auto()
    """Read, change the bit, write back - for devices without 0x16; not atomic against concurrent changes."""


@dataclass(frozen=True)
class ModbusOptions(ProtocolOptions):
    """What a model states about its device in Modbus terms."""
    numbering: RegisterNumbering
    """How the model numbers registers."""
    max_registers: int = MAX_REGISTERS_PER_READ
    """Most registers the device accepts in one read."""
    max_bits: int = MAX_BITS_PER_READ
    """Most coils or discrete inputs the device accepts in one read."""
    single_write: SingleWrite = SingleWrite.FC06
    bit_write: BitWrite = BitWrite.MASK

    def __post_init__(self) -> None:
        if not 1 <= self.max_registers <= MAX_REGISTERS_PER_READ:
            raise ValueError(f"max_registers must be 1-{MAX_REGISTERS_PER_READ}, got {self.max_registers}")
        if not 1 <= self.max_bits <= MAX_BITS_PER_READ:
            raise ValueError(f"max_bits must be 1-{MAX_BITS_PER_READ}, got {self.max_bits}")

    def address(self, access: Access) -> int:
        """The address sent for `access`. Raises ValueError if it has none."""
        return self.numbering.address(access)

    def problems(self, point: Point[Any]) -> list[str]:
        found: list[str] = []
        for side, access in (("read", point.read), ("write", point.write)):
            if access is None:
                continue
            if type(access) not in _TABLES:
                found.append(f"the {side} side is {type(access).__name__}, which is not a Modbus table")
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
                found.append(f"the {side} side spans {span}, more than the {limit} the device "
                             f"accepts in one request")
        if point.write is not None and not point.write.bits and point.registers > MAX_REGISTERS_PER_WRITE:
            found.append(f"a write of {point.registers} registers exceeds the Modbus limit of "
                         f"{MAX_REGISTERS_PER_WRITE}")
        return found
