"""Turns raw registers into a value and back: data type, scale, transform and limits."""
from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from typing import Any

from ._data_type import ByteOrder, DataTypeKind, WordOrder
from ._device import EncodedWrite
from ._errors import InvalidValueError
from ._key import is_state_type
from ._point import Point
from ._value import Quality, Value

_SIGNED_KINDS = frozenset({DataTypeKind.INT16, DataTypeKind.INT32, DataTypeKind.INT64})

_INT_RANGE: Mapping[DataTypeKind, tuple[int, int]] = {
    DataTypeKind.UINT16: (0, 0xFFFF),
    DataTypeKind.INT16: (-0x8000, 0x7FFF),
    DataTypeKind.UINT32: (0, 0xFFFFFFFF),
    DataTypeKind.INT32: (-0x80000000, 0x7FFFFFFF),
    DataTypeKind.UINT64: (0, 0xFFFFFFFFFFFFFFFF),
    DataTypeKind.INT64: (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    DataTypeKind.BCD16: (0, 9999),
    DataTypeKind.BCD32: (0, 99999999),
}

_RELATIVE_TOLERANCE = 1e-9


# ================================================================================= public API


def decode(point: Point[Any], registers: Sequence[int]) -> tuple[Value, Quality]:
    """Registers as the device sent them -> `(value, Quality.GOOD)` or `(None, Quality.NO_DATA)`.

    Raises:
        InvalidValueError: `registers` has the wrong count, or a value outside 0..0xFFFF."""
    expected = point.registers
    if len(registers) != expected:
        raise InvalidValueError(f"point {point.key!r} needs {expected} register(s), got {len(registers)}")
    for register in registers:
        if not 0 <= register <= 0xFFFF:
            raise InvalidValueError(f"point {point.key!r}: register {register} is outside 0..0xFFFF")

    data_type = point.data_type
    kind = data_type.kind
    if kind is DataTypeKind.BOOL:
        if not _is_reading(point, registers[0]):
            return (None, Quality.NO_DATA)
        return (registers[0] != 0, Quality.GOOD)
    if kind is DataTypeKind.BIT:
        assert data_type.bit_index is not None
        return (bool((registers[0] >> data_type.bit_index) & 1), Quality.GOOD)
    if kind is DataTypeKind.STRING:
        return _decode_string(point, registers)
    if data_type.is_float:
        return _as_key_type(point, *_decode_float(point, registers))
    return _as_key_type(point, *_decode_int(point, registers))


def encode(point: Point[Any], value: object) -> EncodedWrite:
    """`value`, in engineering units -> what to write, or `InvalidValueError` naming why it cannot be."""
    if point.write is None:
        raise InvalidValueError(f"point {point.key!r} has no write side")

    data_type = point.data_type
    kind = data_type.kind
    if kind is DataTypeKind.BOOL:
        raw = 1 if _as_bit_value(point, value) else 0
        if not _is_reading(point, raw):
            raise InvalidValueError(f"point {point.key!r}: {value!r} encodes to {raw}, which this point reads "
                                    f"as no data")
        return EncodedWrite(registers=(raw,))
    if kind is DataTypeKind.BIT:
        assert data_type.bit_index is not None
        return EncodedWrite(bit_index=data_type.bit_index, bit_value=_as_bit_value(point, value))
    if kind is DataTypeKind.STRING:
        return _encode_string(point, value)
    return _encode_numeric(point, _numeric_for_key_type(point, value))


# =========================================================================== register assembly


def _reorder(registers: Sequence[int], word_order: WordOrder, byte_order: ByteOrder) -> list[int]:
    """Wire order <-> canonical order (register[0] most significant); its own inverse."""
    ordered = list(reversed(registers)) if word_order is WordOrder.LOW_FIRST else list(registers)
    if byte_order is ByteOrder.LITTLE:
        ordered = [((value & 0xFF) << 8) | (value >> 8) for value in ordered]
    return ordered


def _combine(registers: Sequence[int], word_order: WordOrder, byte_order: ByteOrder) -> int:
    """Registers, in wire order, as one unsigned integer."""
    value = 0
    for register in _reorder(registers, word_order, byte_order):
        value = (value << 16) | register
    return value


def _split(value: int, count: int, word_order: WordOrder, byte_order: ByteOrder) -> tuple[int, ...]:
    """The inverse of `_combine`: an unsigned integer -> `count` registers in wire order."""
    canonical = tuple((value >> (16 * (count - 1 - i))) & 0xFFFF for i in range(count))
    return tuple(_reorder(canonical, word_order, byte_order))


def _string_bytes(registers: Sequence[int], byte_order: ByteOrder) -> bytes:
    """STRING assembly: word_order does not apply to text; byte_order picks the earlier byte."""
    out = bytearray()
    for register in registers:
        high, low = (register >> 8) & 0xFF, register & 0xFF
        out += bytes((high, low)) if byte_order is ByteOrder.BIG else bytes((low, high))
    return bytes(out)


def _string_registers(data: bytes, byte_order: ByteOrder) -> tuple[int, ...]:
    """The inverse of `_string_bytes`: byte pairs -> registers, still in register order."""
    registers: list[int] = []
    for i in range(0, len(data), 2):
        first, second = data[i], data[i + 1]
        registers.append((first << 8) | second if byte_order is ByteOrder.BIG else (second << 8) | first)
    return tuple(registers)


def _to_signed(value: int, bits: int) -> int:
    """An unsigned integer of `bits` bits -> its two's-complement signed value."""
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if value & sign_bit else value


def _to_unsigned(value: int, bits: int) -> int:
    """The inverse of `_to_signed`: a signed integer -> its unsigned `bits`-bit representation."""
    return value if value >= 0 else value + (1 << bits)


# ============================================================================ the numeric pipeline


def _inverse_int(scale: float) -> int | None:
    """`n` such that `scale == 1/n`, within tolerance; `None` if there is no such `n`."""
    if scale == 0:
        return None
    inverse = 1 / scale
    nearest = round(inverse)
    if nearest != 0 and abs(inverse - nearest) <= _RELATIVE_TOLERANCE * max(1.0, abs(inverse)):
        return nearest
    return None


def _scale(raw: int | float, scale: float, offset: float) -> float:
    # offset is added only when non-zero: "-0.0 + 0.0" is IEEE 754 positive zero, which would
    # silently flip the sign of a genuine -0.0 reading for a point with no offset at all.
    n = _inverse_int(scale)
    x = raw / n if n is not None else raw * scale
    return x if offset == 0 else x + offset


def _unscale(x: float, scale: float, offset: float) -> float:
    y = x if offset == 0 else x - offset
    n = _inverse_int(scale)
    return y * n if n is not None else y / scale


def _is_close_to_integer(x: float) -> bool:
    nearest = round(x)
    return abs(x - nearest) <= _RELATIVE_TOLERANCE * max(1.0, abs(x))


def _round_half_away_from_zero(x: float) -> int:
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


def _numeric_pipeline(point: Point[Any], raw: int | float, *, integer_raw: bool) -> Value:
    """raw -> scale, offset -> transform.read -> round to effective_precision."""
    if integer_raw and point.scale == 1 and point.offset == 0 and point.transform is None:
        # No conversion at all: return the raw int unchanged, so a U64 above 2**53 stays exact.
        assert isinstance(raw, int)
        return raw
    x = _scale(raw, point.scale, point.offset)
    if point.transform is not None:
        x = point.transform.read(x)
    precision = point.effective_precision
    if precision is not None:
        x = round(x, precision)
        if precision == 0:
            return int(x)
    return x


# ==================================================================================== decoding


def _decode_int(point: Point[Any], registers: Sequence[int]) -> tuple[Value, Quality]:
    data_type = point.data_type
    kind = data_type.kind
    combined = _combine(registers, point.word_order, point.byte_order)
    if kind in (DataTypeKind.BCD16, DataTypeKind.BCD32):
        raw = _bcd_unpack(combined, len(registers) * 4)
        if raw is None:
            return (None, Quality.NO_DATA)
    else:
        raw = _to_signed(combined, len(registers) * 16) if kind in _SIGNED_KINDS else combined

    if raw in point.no_data:
        return (None, Quality.NO_DATA)
    if point.raw_range is not None and not (point.raw_range[0] <= raw <= point.raw_range[1]):
        return (None, Quality.NO_DATA)
    return (_numeric_pipeline(point, raw, integer_raw=True), Quality.GOOD)


def _decode_float(point: Point[Any], registers: Sequence[int]) -> tuple[Value, Quality]:
    data_type = point.data_type
    width, fmt = (4, ">f") if data_type.kind is DataTypeKind.FLOAT32 else (8, ">d")
    combined = _combine(registers, point.word_order, point.byte_order)
    x = struct.unpack(fmt, combined.to_bytes(width, "big"))[0]
    if not math.isfinite(x):
        return (None, Quality.NO_DATA)
    return (_numeric_pipeline(point, x, integer_raw=False), Quality.GOOD)


def _decode_string(point: Point[Any], registers: Sequence[int]) -> tuple[Value, Quality]:
    data_type = point.data_type
    raw_bytes = _string_bytes(registers, point.byte_order)
    cut = raw_bytes.split(b"\x00", 1)[0]
    return (cut.decode(data_type.encoding, errors="replace"), Quality.GOOD)


def _is_reading(point: Point[Any], raw: int) -> bool:
    """Whether `raw` is a reading, not one of the point's "no reading" values."""
    if raw in point.no_data:
        return False
    return point.raw_range is None or point.raw_range[0] <= raw <= point.raw_range[1]


def _bcd_unpack(combined: int, nibble_count: int) -> int | None:
    """One decimal digit per nibble, most significant nibble first. `None` for any nibble > 9."""
    value = 0
    for i in range(nibble_count):
        nibble = (combined >> (4 * (nibble_count - 1 - i))) & 0xF
        if nibble > 9:
            return None
        value = value * 10 + nibble
    return value


def _bcd_pack(value: int, nibble_count: int) -> int:
    """The inverse of `_bcd_unpack`: a decimal integer -> one nibble per digit."""
    combined = 0
    for i in range(nibble_count):
        combined |= (value % 10) << (4 * i)
        value //= 10
    return combined


def _as_key_type(point: Point[Any], value: Value, quality: Quality) -> tuple[Value, Quality]:
    """A decoded number as the key's type: a float, or the state an integer names."""
    value_type: type[object] = point.key.type
    if value is None or isinstance(value, (bool, str)):
        return (value, quality)
    if is_state_type(value_type):
        try:
            return (value_type(value), quality)
        except ValueError:
            return (None, Quality.NO_DATA)
    if value_type is float:
        return (float(value), quality)
    return (value, quality)


# ==================================================================================== encoding


def _numeric_for_key_type(point: Point[Any], value: object) -> int | float:
    """`value` checked against the key's type: an int for an int or a state, a number for a float."""
    value_type: type[object] = point.key.type
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidValueError(f"point {point.key!r}: expected {value_type.__name__}, got {type(value).__name__}")
    if is_state_type(value_type):
        try:
            return int(value_type(value))
        except ValueError:
            raise InvalidValueError(f"point {point.key!r}: {value!r} is not a state of {value_type.__name__}; "
                                    f"expected one of {[m.name for m in value_type]}") from None
    if value_type is int and not isinstance(value, int):
        raise InvalidValueError(f"point {point.key!r}: expected int, got float {value!r}")
    return value


def _as_bit_value(point: Point[Any], value: object) -> bool:
    """BOOL / BIT accept `bool`, or the int 0/1 - nothing else, so `1.0` and `"1"` are refused."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise InvalidValueError(f"point {point.key!r}: expected bool or 0/1, got {value!r}")


def _check_limits(point: Point[Any], x: int | float) -> None:
    limits = point.limits
    if limits is None:
        return
    if limits.min is not None and x < limits.min:
        raise InvalidValueError(f"point {point.key!r}: {x} is below the minimum {limits.min}")
    if limits.max is not None and x > limits.max:
        raise InvalidValueError(f"point {point.key!r}: {x} is above the maximum {limits.max}")
    if limits.step is not None:
        base = limits.min if limits.min is not None else 0
        steps = (x - base) / limits.step
        if not _is_close_to_integer(steps):
            raise InvalidValueError(f"point {point.key!r}: {x} is not a multiple of step {limits.step} from {base}")


def _encode_numeric(point: Point[Any], value: Value) -> EncodedWrite:
    data_type = point.data_type
    kind = data_type.kind
    if isinstance(value, bool):
        raise InvalidValueError(f"point {point.key!r}: expected a number, got bool")
    if not isinstance(value, (int, float)):
        raise InvalidValueError(f"point {point.key!r}: expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidValueError(f"point {point.key!r}: {value!r} is not finite")

    _check_limits(point, value)

    passthrough = data_type.is_integer and point.scale == 1 and point.offset == 0 and point.transform is None
    if passthrough and isinstance(value, int):
        raw_for_kind: int | float = value
    else:
        x = float(value)
        if point.transform is not None:
            x = point.transform.write(x)
        if not math.isfinite(x):
            raise InvalidValueError(f"point {point.key!r}: {value!r} is not finite after its transform")
        raw_for_kind = _unscale(x, point.scale, point.offset)
        if not math.isfinite(raw_for_kind):
            raise InvalidValueError(f"point {point.key!r}: {value!r} does not fit this point's scale")

    if data_type.is_integer:
        raw = raw_for_kind if isinstance(raw_for_kind, int) else _round_half_away_from_zero(raw_for_kind)
        lo, hi = _INT_RANGE[kind]
        if not lo <= raw <= hi:
            raise InvalidValueError(f"point {point.key!r}: {value!r} encodes to {raw}, outside "
                              f"{kind.name}'s range {lo}..{hi}")
        if raw in point.no_data:
            raise InvalidValueError(f"point {point.key!r}: {value!r} encodes to {raw}, one of this "
                              f"point's no_data sentinels")
        if point.raw_range is not None and not point.raw_range[0] <= raw <= point.raw_range[1]:
            raise InvalidValueError(f"point {point.key!r}: {value!r} encodes to {raw}, outside "
                              f"raw_range {point.raw_range}")
        if kind in (DataTypeKind.BCD16, DataTypeKind.BCD32):
            combined = _bcd_pack(raw, data_type.registers * 4)
        else:
            combined = _to_unsigned(raw, data_type.registers * 16)
        return EncodedWrite(registers=_split(combined, data_type.registers, point.word_order, point.byte_order))

    fmt = ">f" if kind is DataTypeKind.FLOAT32 else ">d"
    try:
        packed = struct.pack(fmt, raw_for_kind)
    except OverflowError as err:
        raise InvalidValueError(f"point {point.key!r}: {value!r} overflows {kind.name}") from err
    combined = int.from_bytes(packed, "big")
    return EncodedWrite(registers=_split(combined, data_type.registers, point.word_order, point.byte_order))


def _encode_string(point: Point[Any], value: object) -> EncodedWrite:
    data_type = point.data_type
    if not isinstance(value, str):
        raise InvalidValueError(f"point {point.key!r}: expected str, got {type(value).__name__}")
    assert data_type.length is not None
    capacity = data_type.length * 2
    raw_bytes = value.encode(data_type.encoding)
    if len(raw_bytes) > capacity:
        raise InvalidValueError(f"point {point.key!r}: {value!r} needs {len(raw_bytes)} bytes, more "
                          f"than the {capacity} this point holds")
    padded = raw_bytes + b"\x00" * (capacity - len(raw_bytes))
    return EncodedWrite(registers=_string_registers(padded, point.byte_order))
