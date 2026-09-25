"""How a value is stored in registers: its data type, word order and byte order."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from types import MappingProxyType
from typing import ClassVar


class WordOrder(StrEnum):
    """Order of the 16-bit registers that make up a wider value."""
    HIGH_FIRST = auto()
    """register[0] holds the most significant word - the Modbus convention."""
    LOW_FIRST = auto()


class ByteOrder(StrEnum):
    """Order of the two bytes inside each 16-bit register."""
    BIG = auto()
    """High byte first - the Modbus convention."""
    LITTLE = auto()


class DataTypeKind(Enum):
    """What a data type is, before its parameters: which bit, how long, which states."""
    UINT16 = auto()
    INT16 = auto()
    UINT32 = auto()
    INT32 = auto()
    UINT64 = auto()
    INT64 = auto()
    FLOAT32 = auto()
    """IEEE 754 binary32."""
    FLOAT64 = auto()
    """IEEE 754 binary64."""
    BCD16 = auto()
    """Four decimal digits, one per nibble."""
    BCD32 = auto()
    BOOL = auto()
    """A whole register (non-zero is True), or a coil / discrete input."""
    BIT = auto()
    """One bit of a 16-bit register."""
    STRING = auto()
    ENUM = auto()
    """An integer mapped to named states."""


_REGISTERS: Mapping[DataTypeKind, int] = MappingProxyType({
    DataTypeKind.UINT16: 1, DataTypeKind.INT16: 1, DataTypeKind.BCD16: 1, DataTypeKind.BOOL: 1, DataTypeKind.BIT: 1,
    DataTypeKind.UINT32: 2, DataTypeKind.INT32: 2, DataTypeKind.FLOAT32: 2, DataTypeKind.BCD32: 2,
    DataTypeKind.UINT64: 4, DataTypeKind.INT64: 4, DataTypeKind.FLOAT64: 4,
})

_INTEGER_KINDS = frozenset({DataTypeKind.UINT16, DataTypeKind.INT16, DataTypeKind.UINT32, DataTypeKind.INT32,
                            DataTypeKind.UINT64, DataTypeKind.INT64, DataTypeKind.BCD16, DataTypeKind.BCD32})
_FLOAT_KINDS = frozenset({DataTypeKind.FLOAT32, DataTypeKind.FLOAT64})


@dataclass(frozen=True, eq=False)
class DataType:
    """How a point's value is stored in registers.

    Use the constants and constructors, e.g. `DataType.INT16`, not this class directly.
    """
    kind: DataTypeKind
    bit_index: int | None = None
    """BIT: which bit, 0 = least significant."""
    length: int | None = None
    """STRING: how many registers the text occupies."""
    encoding: str = "utf-8"
    """STRING: the text encoding."""
    mapping: Mapping[int, str] | None = None
    """ENUM: raw value -> state name."""
    base: DataTypeKind = DataTypeKind.UINT16
    """ENUM: the integer type the raw value is read as."""

    UINT16: ClassVar[DataType]
    INT16: ClassVar[DataType]
    UINT32: ClassVar[DataType]
    INT32: ClassVar[DataType]
    UINT64: ClassVar[DataType]
    INT64: ClassVar[DataType]
    FLOAT32: ClassVar[DataType]
    FLOAT64: ClassVar[DataType]
    BCD16: ClassVar[DataType]
    BCD32: ClassVar[DataType]
    BOOL: ClassVar[DataType]

    def __post_init__(self) -> None:
        if self.kind is DataTypeKind.BIT:
            if self.bit_index is None or not 0 <= self.bit_index <= 15:
                raise ValueError(f"a BIT data type needs a bit from 0 to 15, got {self.bit_index}")
        elif self.bit_index is not None:
            raise ValueError(f"only a BIT data type has a bit, not {self.kind.name}")
        if self.kind is DataTypeKind.STRING:
            if self.length is None or self.length < 1:
                raise ValueError(f"a STRING data type needs a length of at least one register, got {self.length}")
            "".encode(self.encoding)                     # raises LookupError for an unknown encoding
        elif self.length is not None:
            raise ValueError(f"only a STRING data type has a length, not {self.kind.name}")
        if self.kind is DataTypeKind.ENUM:
            if not self.mapping:
                raise ValueError("an ENUM data type needs a mapping with at least one state")
            if self.base not in _INTEGER_KINDS or self.base in (DataTypeKind.BCD16, DataTypeKind.BCD32):
                raise ValueError(f"an ENUM is read as a binary integer, not {self.base.name}")
            if len(set(self.mapping.values())) != len(self.mapping):
                raise ValueError("an ENUM maps two raw values to the same name; a write could not choose")
            object.__setattr__(self, "mapping", MappingProxyType(dict(self.mapping)))
        elif self.mapping is not None:
            raise ValueError(f"only an ENUM data type has a mapping, not {self.kind.name}")

    @staticmethod
    def bit(index: int) -> DataType:
        """Bit `index` of a 16-bit register, 0 being the least significant."""
        return DataType(DataTypeKind.BIT, bit_index=index)

    @staticmethod
    def string(length: int, encoding: str = "utf-8") -> DataType:
        """Text occupying `length` registers, two bytes each, cut at the first NUL."""
        return DataType(DataTypeKind.STRING, length=length, encoding=encoding)

    @staticmethod
    def enum(mapping: Mapping[int, str], base: DataTypeKind = DataTypeKind.UINT16) -> DataType:
        """Named states. A raw value missing from `mapping` reads as NO_DATA."""
        return DataType(DataTypeKind.ENUM, mapping=mapping, base=base)

    @property
    def registers(self) -> int:
        """How many 16-bit registers a value occupies. One for a coil or discrete input."""
        if self.kind is DataTypeKind.STRING:
            assert self.length is not None
            return self.length
        if self.kind is DataTypeKind.ENUM:
            return _REGISTERS[self.base]
        return _REGISTERS[self.kind]

    @property
    def is_numeric(self) -> bool:
        """Whether the value is a number that scaling, limits and transforms apply to."""
        return self.kind in _INTEGER_KINDS or self.kind in _FLOAT_KINDS

    @property
    def is_integer(self) -> bool:
        return self.kind in _INTEGER_KINDS

    @property
    def is_float(self) -> bool:
        return self.kind in _FLOAT_KINDS

    @property
    def is_boolean(self) -> bool:
        return self.kind in (DataTypeKind.BOOL, DataTypeKind.BIT)

    def __repr__(self) -> str:
        if self.kind is DataTypeKind.BIT: return f"DataType.bit({self.bit_index})"
        if self.kind is DataTypeKind.STRING: return f"DataType.string({self.length}, {self.encoding!r})"
        if self.kind is DataTypeKind.ENUM: return f"DataType.enum({dict(self.mapping or {})!r})"
        return f"DataType.{self.kind.name}"


for _kind in (DataTypeKind.UINT16, DataTypeKind.INT16, DataTypeKind.UINT32, DataTypeKind.INT32, DataTypeKind.UINT64,
              DataTypeKind.INT64, DataTypeKind.FLOAT32, DataTypeKind.FLOAT64, DataTypeKind.BCD16, DataTypeKind.BCD32,
              DataTypeKind.BOOL):
    setattr(DataType, _kind.name, DataType(_kind))
del _kind
