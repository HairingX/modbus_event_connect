"""Conversion between raw registers and values, pinned with hand-computed register vectors for
every numeric kind and word/byte order."""
from __future__ import annotations

import itertools
import math
import struct
from enum import IntEnum
from typing import Any

import pytest

from modbus_event_connect._conversion import decode, encode
from modbus_event_connect._data_type import ByteOrder, DataType, DataTypeKind, WordOrder
from modbus_event_connect._device import EncodedWrite
from modbus_event_connect._errors import InvalidValueError
from modbus_event_connect._key import Key
from modbus_event_connect._point import Limits, Point, Transforms
from modbus_event_connect._value import Quality, Value
from modbus_event_connect.modbus._access import (
    Coil,
    DiscreteInput,
    HoldingRegister,
    InputRegister,
)

# ================================================================================== point builders


def _key(data_type: DataType, value_type: type[Any] | None) -> Key[Any]:
    """`value_type`, or the one type every point of `data_type` can hold."""
    if value_type is None:
        value_type = bool if data_type.is_boolean else str if data_type.kind is DataTypeKind.STRING else float
    return Key("p", value_type)


def _rw_point(data_type: DataType, value_type: type[Any] | None = None, **kwargs: Any) -> Point[Any]:
    """A point with both a read and a write side, on ordinary (non-bit) Modbus tables."""
    return Point(_key(data_type, value_type), read=InputRegister(0), write=HoldingRegister(0), data_type=data_type,
                 **kwargs)


def _ro_point(data_type: DataType, value_type: type[Any] | None = None, **kwargs: Any) -> Point[Any]:
    return Point(_key(data_type, value_type), read=InputRegister(0), data_type=data_type, **kwargs)


def _string_point(length: int, *, byte_order: ByteOrder = ByteOrder.BIG, encoding: str = "utf-8") -> Point[Any]:
    return Point(Key("p", str), read=InputRegister(0), write=HoldingRegister(0), data_type=DataType.string(length, encoding), byte_order=byte_order)


class Mode(IntEnum):
    OFF = 0
    ON = 1
    AUTO = 2


class Signed(IntEnum):
    ERROR = -1
    OFF = 0


def _state_point[T: IntEnum](states: type[T], data_type: DataType = DataType.UINT16) -> Point[T]:
    return Point(Key("p", states), read=InputRegister(0), write=HoldingRegister(0), data_type=data_type)


def _regs_from_bytes(data: bytes, byte_order: ByteOrder) -> tuple[int, ...]:
    """Turns raw bytes into registers, byte_order deciding which byte of each pair is high."""
    assert len(data) % 2 == 0
    regs: list[int] = []
    for i in range(0, len(data), 2):
        a, b = data[i], data[i + 1]
        regs.append((a << 8) | b if byte_order is ByteOrder.BIG else (b << 8) | a)
    return tuple(regs)


_ORDERS: list[tuple[WordOrder, ByteOrder]] = list(itertools.product(WordOrder, ByteOrder))

# ======================================================== register assembly: fixed hand vectors
# Hand-computed wire orderings for r0=0x1234, r1=0x5678 (and one/four registers), by word/byte order.

_WIRE_1: dict[tuple[WordOrder, ByteOrder], tuple[int, ...]] = {
    (WordOrder.HIGH_FIRST, ByteOrder.BIG): (0x1234,),
    (WordOrder.LOW_FIRST, ByteOrder.BIG): (0x1234,),
    (WordOrder.HIGH_FIRST, ByteOrder.LITTLE): (0x3412,),
    (WordOrder.LOW_FIRST, ByteOrder.LITTLE): (0x3412,),
}

_WIRE_2: dict[tuple[WordOrder, ByteOrder], tuple[int, ...]] = {
    (WordOrder.HIGH_FIRST, ByteOrder.BIG): (0x1234, 0x5678),
    (WordOrder.LOW_FIRST, ByteOrder.BIG): (0x5678, 0x1234),
    (WordOrder.HIGH_FIRST, ByteOrder.LITTLE): (0x3412, 0x7856),
    (WordOrder.LOW_FIRST, ByteOrder.LITTLE): (0x7856, 0x3412),
}

_WIRE_4: dict[tuple[WordOrder, ByteOrder], tuple[int, ...]] = {
    (WordOrder.HIGH_FIRST, ByteOrder.BIG): (0x1122, 0x3344, 0x5566, 0x7788),
    (WordOrder.LOW_FIRST, ByteOrder.BIG): (0x7788, 0x5566, 0x3344, 0x1122),
    (WordOrder.HIGH_FIRST, ByteOrder.LITTLE): (0x2211, 0x4433, 0x6655, 0x8877),
    (WordOrder.LOW_FIRST, ByteOrder.LITTLE): (0x8877, 0x6655, 0x4433, 0x2211),
}

# IEEE 754: 21.0 is binary32 0x41A80000, 1.0 is binary64 0x3FF0000000000000.
_WIRE_F32: dict[tuple[WordOrder, ByteOrder], tuple[int, ...]] = {
    (WordOrder.HIGH_FIRST, ByteOrder.BIG): (0x41A8, 0x0000),
    (WordOrder.LOW_FIRST, ByteOrder.BIG): (0x0000, 0x41A8),
    (WordOrder.HIGH_FIRST, ByteOrder.LITTLE): (0xA841, 0x0000),
    (WordOrder.LOW_FIRST, ByteOrder.LITTLE): (0x0000, 0xA841),
}
_WIRE_F64: dict[tuple[WordOrder, ByteOrder], tuple[int, ...]] = {
    (WordOrder.HIGH_FIRST, ByteOrder.BIG): (0x3FF0, 0x0000, 0x0000, 0x0000),
    (WordOrder.LOW_FIRST, ByteOrder.BIG): (0x0000, 0x0000, 0x0000, 0x3FF0),
    (WordOrder.HIGH_FIRST, ByteOrder.LITTLE): (0xF03F, 0x0000, 0x0000, 0x0000),
    (WordOrder.LOW_FIRST, ByteOrder.LITTLE): (0x0000, 0x0000, 0x0000, 0xF03F),
}

# BCD16/32 reuse the U16/U32 hand vectors' bit patterns, read as decimal nibbles.
_MATRIX: list[tuple[DataType, dict[tuple[WordOrder, ByteOrder], tuple[int, ...]], float | int]] = [
    (DataType.UINT16, _WIRE_1, 0x1234),
    (DataType.INT16, _WIRE_1, 0x1234),
    (DataType.UINT32, _WIRE_2, 0x12345678),
    (DataType.INT32, _WIRE_2, 0x12345678),
    (DataType.UINT64, _WIRE_4, 0x1122334455667788),
    (DataType.INT64, _WIRE_4, 0x1122334455667788),
    (DataType.BCD16, _WIRE_1, 1234),
    (DataType.BCD32, _WIRE_2, 12345678),
    (DataType.FLOAT32, _WIRE_F32, 21.0),
    (DataType.FLOAT64, _WIRE_F64, 1.0),
]
_MATRIX_IDS = ["U16", "S16", "U32", "S32", "U64", "S64", "BCD16", "BCD32", "F32", "F64"]


@pytest.mark.parametrize("data_type,wire,expected", _MATRIX, ids=_MATRIX_IDS)
@pytest.mark.parametrize("word_order,byte_order", _ORDERS, ids=lambda o: o.name if isinstance(o, WordOrder | ByteOrder) else str(o))
def test_decode_assembles_registers_for_every_numeric_kind_and_order(
    word_order: WordOrder, byte_order: ByteOrder,
    data_type: DataType, wire: dict[tuple[WordOrder, ByteOrder], tuple[int, ...]], expected: float | int,
) -> None:
    point = _rw_point(data_type, int if data_type.is_integer else float, word_order=word_order, byte_order=byte_order)
    assert decode(point, wire[(word_order, byte_order)]) == (expected, Quality.GOOD)


@pytest.mark.parametrize("data_type,wire,expected", _MATRIX, ids=_MATRIX_IDS)
@pytest.mark.parametrize("word_order,byte_order", _ORDERS, ids=lambda o: o.name if isinstance(o, WordOrder | ByteOrder) else str(o))
def test_encode_produces_the_wire_registers_for_every_numeric_kind_and_order(
    word_order: WordOrder, byte_order: ByteOrder,
    data_type: DataType, wire: dict[tuple[WordOrder, ByteOrder], tuple[int, ...]], expected: float | int,
) -> None:
    point = _rw_point(data_type, word_order=word_order, byte_order=byte_order)
    assert encode(point, expected).registers == wire[(word_order, byte_order)]


# ------------------------------------------------------------------------------ sign extension


def test_s16_decodes_a_negative_two_complement_value() -> None:
    assert decode(_rw_point(DataType.INT16), [0x8001]) == (-32767, Quality.GOOD)


def test_s32_decodes_a_negative_two_complement_value() -> None:
    assert decode(_rw_point(DataType.INT32), [0xFFFF, 0xFFFE]) == (-2, Quality.GOOD)


def test_s64_decodes_a_negative_two_complement_value() -> None:
    assert decode(_rw_point(DataType.INT64), [0x8000, 0x0000, 0x0000, 0x0000]) == (-(2**63), Quality.GOOD)


def test_s16_encodes_a_negative_value_as_two_complement() -> None:
    assert encode(_rw_point(DataType.INT16), -32767).registers == (0x8001,)


def test_s32_encodes_a_negative_value_as_two_complement() -> None:
    assert encode(_rw_point(DataType.INT32), -2).registers == (0xFFFF, 0xFFFE)


def test_s64_encodes_a_negative_value_as_two_complement() -> None:
    assert encode(_rw_point(DataType.INT64), -(2**63)).registers == (0x8000, 0x0000, 0x0000, 0x0000)


# ============================================================== integer round trip at boundaries

_INTEGER_CODECS = [DataType.UINT16, DataType.INT16, DataType.UINT32, DataType.INT32, DataType.UINT64, DataType.INT64, DataType.BCD16, DataType.BCD32]
_INTEGER_IDS = ["U16", "S16", "U32", "S32", "U64", "S64", "BCD16", "BCD32"]

_INT_RANGE: dict[DataTypeKind, tuple[int, int]] = {
    DataTypeKind.UINT16: (0, 0xFFFF),
    DataTypeKind.INT16: (-0x8000, 0x7FFF),
    DataTypeKind.UINT32: (0, 0xFFFFFFFF),
    DataTypeKind.INT32: (-0x80000000, 0x7FFFFFFF),
    DataTypeKind.UINT64: (0, 0xFFFFFFFFFFFFFFFF),
    DataTypeKind.INT64: (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    DataTypeKind.BCD16: (0, 9999),
    DataTypeKind.BCD32: (0, 99999999),
}


def _boundary_values(data_type: DataType) -> tuple[int, ...]:
    lo, hi = _INT_RANGE[data_type.kind]
    values = {lo, hi, 0, 1}
    if lo < 0:
        values.add(-1)
    return tuple(sorted(values))


@pytest.mark.parametrize("data_type", _INTEGER_CODECS, ids=_INTEGER_IDS)
@pytest.mark.parametrize("word_order,byte_order", _ORDERS, ids=lambda o: o.name if isinstance(o, WordOrder | ByteOrder) else str(o))
def test_integer_round_trip_at_boundary_values(word_order: WordOrder, byte_order: ByteOrder, data_type: DataType) -> None:
    point = _rw_point(data_type, int, word_order=word_order, byte_order=byte_order)
    for value in _boundary_values(data_type):
        registers = encode(point, value).registers
        assert decode(point, registers) == (value, Quality.GOOD)


@pytest.mark.parametrize("data_type", [DataType.FLOAT32, DataType.FLOAT64], ids=["F32", "F64"])
@pytest.mark.parametrize("word_order,byte_order", _ORDERS, ids=lambda o: o.name if isinstance(o, WordOrder | ByteOrder) else str(o))
def test_float_round_trip_at_boundary_and_precise_values(word_order: WordOrder, byte_order: ByteOrder, data_type: DataType) -> None:
    point = _rw_point(data_type, word_order=word_order, byte_order=byte_order)
    for value in (0.0, 1.0, -1.0, 21.5, 100.125):
        registers = encode(point, value).registers
        decoded, quality = decode(point, registers)
        assert quality is Quality.GOOD
        assert decoded == value


def test_u64_extreme_value_round_trips_with_no_float_loss() -> None:
    point = _rw_point(DataType.UINT64, int)
    value = 2**64 - 1
    decoded, quality = decode(point, encode(point, value).registers)
    assert quality is Quality.GOOD
    assert decoded == value
    assert isinstance(decoded, int)


@pytest.mark.parametrize("value", [-(2**63), 2**63 - 1], ids=["min", "max"])
def test_s64_extreme_values_round_trip_with_no_float_loss(value: int) -> None:
    point = _rw_point(DataType.INT64, int)
    decoded, quality = decode(point, encode(point, value).registers)
    assert quality is Quality.GOOD
    assert decoded == value
    assert isinstance(decoded, int)


# ============================================================================= integer range limits


@pytest.mark.parametrize("data_type", _INTEGER_CODECS, ids=_INTEGER_IDS)
def test_extreme_integer_values_encode_and_one_past_them_is_refused(data_type: DataType) -> None:
    point = _rw_point(data_type)
    lo, hi = _INT_RANGE[data_type.kind]
    encode(point, lo)
    encode(point, hi)
    with pytest.raises(InvalidValueError):
        encode(point, hi + 1)
    with pytest.raises(InvalidValueError):
        encode(point, lo - 1)


# ============================================================================================ BCD


def test_bcd16_valid_value_round_trips() -> None:
    point = _rw_point(DataType.BCD16)
    assert decode(point, [0x9999]) == (9999, Quality.GOOD)
    assert encode(point, 9999).registers == (0x9999,)


def test_bcd16_invalid_nibble_reads_as_no_data() -> None:
    assert decode(_rw_point(DataType.BCD16), [0x123A]) == (None, Quality.NO_DATA)


def test_bcd32_invalid_nibble_reads_as_no_data() -> None:
    assert decode(_rw_point(DataType.BCD32), [0x1234, 0x567F]) == (None, Quality.NO_DATA)


def test_bcd16_write_out_of_range_is_refused() -> None:
    with pytest.raises(InvalidValueError):
        encode(_rw_point(DataType.BCD16), 10000)


def test_bcd32_write_out_of_range_is_refused() -> None:
    with pytest.raises(InvalidValueError):
        encode(_rw_point(DataType.BCD32), 100000000)


# =========================================================================================== IEEE


def test_f32_21_decodes_from_the_documented_registers() -> None:
    assert decode(_rw_point(DataType.FLOAT32), [0x41A8, 0x0000]) == (21.0, Quality.GOOD)


def test_f32_21_encodes_to_the_documented_registers() -> None:
    assert encode(_rw_point(DataType.FLOAT32), 21.0).registers == (0x41A8, 0x0000)


def test_f64_vector_decodes_and_encodes_exactly() -> None:
    point = _rw_point(DataType.FLOAT64)
    value = 3.14159265358979
    registers = encode(point, value).registers
    assert decode(point, registers) == (value, Quality.GOOD)


@pytest.mark.parametrize("data_type", [DataType.FLOAT32, DataType.FLOAT64], ids=["F32", "F64"])
def test_negative_zero_round_trips_with_its_sign(data_type: DataType) -> None:
    point = _rw_point(data_type)
    registers = encode(point, -0.0).registers
    decoded, quality = decode(point, registers)
    assert quality is Quality.GOOD
    assert isinstance(decoded, float)
    assert decoded == 0.0
    assert math.copysign(1.0, decoded) == -1.0


@pytest.mark.parametrize("fmt,data_type", [(">f", DataType.FLOAT32), (">d", DataType.FLOAT64)], ids=["F32", "F64"])
def test_nan_and_infinity_read_as_no_data(fmt: str, data_type: DataType) -> None:
    point = _rw_point(data_type)
    for raw in (float("nan"), float("inf"), float("-inf")):
        packed = struct.pack(fmt, raw)
        registers = [int.from_bytes(packed[i:i + 2], "big") for i in range(0, len(packed), 2)]
        assert decode(point, registers) == (None, Quality.NO_DATA)


@pytest.mark.parametrize("data_type", [DataType.FLOAT32, DataType.FLOAT64], ids=["F32", "F64"])
def test_nan_and_infinity_are_refused_on_write(data_type: DataType) -> None:
    point = _rw_point(data_type)
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(InvalidValueError):
            encode(point, value)


def test_f32_overflow_on_write_is_refused() -> None:
    with pytest.raises(InvalidValueError):
        encode(_rw_point(DataType.FLOAT32), 1e40)


# ======================================================================================= BOOL / BIT


def test_bool_any_nonzero_register_is_true() -> None:
    point = _ro_point(DataType.BOOL)
    assert decode(point, [1]) == (True, Quality.GOOD)
    assert decode(point, [42]) == (True, Quality.GOOD)
    assert decode(point, [0]) == (False, Quality.GOOD)


def test_bool_reads_the_single_bit_of_a_coil_or_discrete_input() -> None:
    coil = Point(Key("p", bool), read=Coil(0), write=Coil(0), data_type=DataType.BOOL)
    discrete = Point(Key("p", bool), read=DiscreteInput(0), data_type=DataType.BOOL)
    assert decode(coil, [1]) == (True, Quality.GOOD)
    assert decode(coil, [0]) == (False, Quality.GOOD)
    assert decode(discrete, [1]) == (True, Quality.GOOD)
    assert decode(discrete, [0]) == (False, Quality.GOOD)


def test_bool_encodes_to_a_single_register() -> None:
    point = Point(Key("p", bool), read=Coil(0), write=Coil(0), data_type=DataType.BOOL)
    assert encode(point, True).registers == (1,)
    assert encode(point, False).registers == (0,)


@pytest.mark.parametrize("bit", range(16))
def test_bit_decodes_the_given_bit_of_the_register(bit: int) -> None:
    point = _rw_point(DataType.bit(bit))
    assert decode(point, [1 << bit]) == (True, Quality.GOOD)
    assert decode(point, [0xFFFF ^ (1 << bit)]) == (False, Quality.GOOD)


@pytest.mark.parametrize("bit", range(16))
def test_bit_encodes_the_given_bit(bit: int) -> None:
    point = _rw_point(DataType.bit(bit))
    assert encode(point, True) == EncodedWrite(bit_index=bit, bit_value=True)
    assert encode(point, False) == EncodedWrite(bit_index=bit, bit_value=False)


@pytest.mark.parametrize("value", [True, 1], ids=["True", "1"])
def test_bool_write_accepts_true_and_one(value: Value) -> None:
    encode(Point(Key("p", bool), read=Coil(0), write=Coil(0), data_type=DataType.BOOL), value)  # must not raise


@pytest.mark.parametrize("value", [2, 1.0, "1"], ids=["2", "1.0", "str-1"])
def test_bool_write_refuses_anything_but_bool_or_zero_or_one(value: Value) -> None:
    point = Point(Key("p", bool), read=Coil(0), write=Coil(0), data_type=DataType.BOOL)
    with pytest.raises(InvalidValueError):
        encode(point, value)


# ============================================================================================ STRING


def test_string_decode_cuts_at_the_first_nul() -> None:
    point = _string_point(3)  # 6 bytes
    registers = _regs_from_bytes(b"hi\x00xy\x00", ByteOrder.BIG)
    assert decode(point, registers) == ("hi", Quality.GOOD)


def test_string_encode_pads_with_nul() -> None:
    point = _string_point(3)
    result = encode(point, "hi")
    assert result.registers == _regs_from_bytes(b"hi\x00\x00\x00\x00", ByteOrder.BIG)


def test_string_exact_length_fits_without_padding() -> None:
    point = _string_point(2)  # 4 bytes
    result = encode(point, "abcd")
    assert result.registers == _regs_from_bytes(b"abcd", ByteOrder.BIG)
    assert decode(point, result.registers) == ("abcd", Quality.GOOD)


def test_string_too_long_is_refused() -> None:
    with pytest.raises(InvalidValueError):
        encode(_string_point(2), "abcde")


def test_string_honours_little_byte_order() -> None:
    point = _string_point(2, byte_order=ByteOrder.LITTLE)
    result = encode(point, "ab")
    assert result.registers == _regs_from_bytes(b"ab\x00\x00", ByteOrder.LITTLE)
    assert decode(point, result.registers) == ("ab", Quality.GOOD)


def test_string_decode_does_not_raise_on_invalid_utf8() -> None:
    point = _string_point(2)
    registers = _regs_from_bytes(b"\xff\xfe\x00\x00", ByteOrder.BIG)
    value, quality = decode(point, registers)
    assert quality is Quality.GOOD
    assert value == b"\xff\xfe".decode("utf-8", errors="replace")


# =========================================================================================== states


def test_a_state_decodes_to_its_member() -> None:
    value, quality = decode(_state_point(Mode), [1])
    assert (value, quality) == (Mode.ON, Quality.GOOD) and type(value) is Mode


def test_a_number_no_state_names_is_no_data() -> None:
    assert decode(_state_point(Mode), [5]) == (None, Quality.NO_DATA)


def test_a_state_encodes_to_its_number() -> None:
    assert encode(_state_point(Mode), Mode.AUTO).registers == (2,)


@pytest.mark.parametrize("value", [5, True, "AUTO", 2.5, 1.0])
def test_a_state_write_refuses_anything_but_a_state(value: object) -> None:
    with pytest.raises(InvalidValueError):
        encode(_state_point(Mode), value)


def test_a_state_can_be_a_signed_integer() -> None:
    point = _state_point(Signed, DataType.INT16)
    assert decode(point, [0xFFFF]) == (Signed.ERROR, Quality.GOOD)
    assert encode(point, Signed.ERROR).registers == (0xFFFF,)


# ==================================================================================== no_data / raw_range


def _switch() -> Point[bool]:
    """A 0/1 switch in a whole register, answering 255 when it has no value."""
    return Point(Key("switch", bool), read=HoldingRegister(0), write=HoldingRegister(0), data_type=DataType.BOOL,
                 raw_range=(0, 1))


@pytest.mark.parametrize("raw,value", [(0, False), (1, True)])
def test_a_bool_register_reads_its_values(raw: int, value: bool) -> None:
    assert decode(_switch(), [raw]) == (value, Quality.GOOD)


@pytest.mark.parametrize("raw", [255, 2])
def test_a_bool_register_outside_its_raw_range_is_no_data(raw: int) -> None:
    assert decode(_switch(), [raw]) == (None, Quality.NO_DATA)


def test_a_bool_register_writes_zero_or_one() -> None:
    assert (encode(_switch(), True).registers, encode(_switch(), False).registers) == ((1,), (0,))


def test_a_bool_whose_value_is_its_no_data_sentinel_is_refused() -> None:
    point = Point(Key("p", bool), read=HoldingRegister(0), write=HoldingRegister(0), data_type=DataType.BOOL,
                  no_data=(0,))
    with pytest.raises(InvalidValueError, match="no data"):
        encode(point, False)


def test_no_data_sentinel_reads_as_no_data() -> None:
    point = Point(Key("p", int), read=InputRegister(0), write=HoldingRegister(0), data_type=DataType.INT16, no_data=(0x7FFF,))
    assert decode(point, [0x7FFF]) == (None, Quality.NO_DATA)


def test_raw_range_excludes_values_outside_it_on_read() -> None:
    point = Point(Key("p", int), read=InputRegister(0), write=HoldingRegister(0), data_type=DataType.INT16, raw_range=(0, 1000))
    assert decode(point, [1001]) == (None, Quality.NO_DATA)
    assert decode(point, [1000]) == (1000, Quality.GOOD)  # inclusive bound


def test_write_refuses_a_value_that_encodes_to_the_no_data_sentinel() -> None:
    point = Point(Key("p", int), read=InputRegister(0), write=HoldingRegister(0), data_type=DataType.INT16, no_data=(0x7FFF,))
    with pytest.raises(InvalidValueError):
        encode(point, 0x7FFF)


def test_write_refuses_a_value_outside_raw_range() -> None:
    point = Point(Key("p", int), read=InputRegister(0), write=HoldingRegister(0), data_type=DataType.INT16, raw_range=(0, 1000))
    with pytest.raises(InvalidValueError):
        encode(point, 1001)


# ==================================================================== scale, offset, precision


def test_scale_of_one_tenth_gives_an_exact_result() -> None:
    assert decode(_ro_point(DataType.INT16, scale=0.1), [215]) == (21.5, Quality.GOOD)


def test_scale_of_one_hundredth_gives_an_exact_result() -> None:
    assert decode(_ro_point(DataType.INT16, scale=0.01), [12345]) == (123.45, Quality.GOOD)


def test_offset_is_added_after_scaling() -> None:
    assert decode(_ro_point(DataType.INT16, scale=1, offset=5), [10]) == (15, Quality.GOOD)


def test_explicit_precision_overrides_the_scale_derived_default() -> None:
    value, quality = decode(_ro_point(DataType.INT16, scale=0.001, precision=1), [1234])
    assert quality is Quality.GOOD
    assert value == 1.2


def test_precision_zero_returns_an_int() -> None:
    value, quality = decode(_ro_point(DataType.INT16, int, scale=0.1, precision=0), [27])
    assert quality is Quality.GOOD
    assert value == 3
    assert isinstance(value, int)


# ============================================================================================== transform


def test_transform_read_runs_after_scaling() -> None:
    point = _ro_point(DataType.UINT16, scale=0.1, transform=Transforms.SECONDS_AS_MINUTES)
    assert decode(point, [1200]) == (2.0, Quality.GOOD)  # 1200 * 0.1 = 120s -> 2 minutes


def test_transform_read_without_scaling() -> None:
    point = _ro_point(DataType.UINT16, transform=Transforms.SECONDS_AS_MINUTES)
    assert decode(point, [120]) == (2.0, Quality.GOOD)


def test_transform_write_runs_before_unscaling() -> None:
    point = _rw_point(DataType.UINT16, scale=0.1, transform=Transforms.SECONDS_AS_MINUTES)
    assert encode(point, 2.0).registers == (1200,)


def test_transform_write_without_scaling() -> None:
    point = _rw_point(DataType.UINT16, transform=Transforms.SECONDS_AS_MINUTES)
    assert encode(point, 2.0).registers == (120,)


# ================================================================================= limits and step


def test_write_below_the_minimum_is_refused() -> None:
    point = _rw_point(DataType.INT16, limits=Limits(min=5, max=35))
    with pytest.raises(InvalidValueError):
        encode(point, 4)


def test_write_above_the_maximum_is_refused() -> None:
    point = _rw_point(DataType.INT16, limits=Limits(min=5, max=35))
    with pytest.raises(InvalidValueError):
        encode(point, 36)


def test_write_within_limits_is_accepted() -> None:
    point = _rw_point(DataType.INT16, limits=Limits(min=5, max=35))
    encode(point, 5)
    encode(point, 35)


def test_write_off_step_is_refused() -> None:
    point = _rw_point(DataType.INT16, scale=0.01, limits=Limits(min=5, max=35, step=0.5))
    with pytest.raises(InvalidValueError):
        encode(point, 5.3)


def test_write_on_step_is_accepted() -> None:
    point = _rw_point(DataType.INT16, scale=0.01, limits=Limits(min=5, max=35, step=0.5))
    encode(point, 5.5)
    encode(point, 35.0)


# ========================================================================================= misc refusals


def test_numeric_write_refuses_a_bool() -> None:
    with pytest.raises(InvalidValueError):
        encode(_rw_point(DataType.UINT16), True)


def test_decode_refuses_the_wrong_number_of_registers() -> None:
    with pytest.raises(InvalidValueError):
        decode(_ro_point(DataType.UINT16), [1, 2])


def test_decode_refuses_a_register_above_0xffff() -> None:
    with pytest.raises(InvalidValueError):
        decode(_ro_point(DataType.UINT16), [0x10000])


def test_encode_refuses_a_point_with_no_write_side() -> None:
    with pytest.raises(InvalidValueError):
        encode(_ro_point(DataType.UINT16), 5)
