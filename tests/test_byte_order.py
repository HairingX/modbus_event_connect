"""
Tests for declarable word order and byte order on multi-register values.

ModbusParser.combine_values used to unconditionally assemble registers high-word-first with
big-endian bytes inside each register. That is still the default, but WordOrder and ByteOrder
now let a point say otherwise, for devices (energy meters, inverters, ...) that put the low
word first or swap the bytes within each register.
"""
from enum import auto
from functools import reduce
import itertools

import pytest

from src.modbus_event_connect import (
    ByteOrder,
    ModbusParser,
    ModbusSetpoint,
    ModbusSetpointKey,
    ModbusValueType,
    WordOrder,
)


class SK(ModbusSetpointKey):
    VALUE = auto()


def _point(*, word_order: WordOrder = WordOrder.HIGH_FIRST, byte_order: ByteOrder = ByteOrder.BIG,
           divider: int = 1, signed: bool = False) -> ModbusSetpoint:
    return ModbusSetpoint(key=SK.VALUE, read_address=1, write_address=1, read_length=2,
                          write_length=2, divider=divider, signed=signed,
                          value_type=ModbusValueType.AUTO,
                          word_order=word_order, byte_order=byte_order)


# --------------------------------------------------------------- combine_values / decoding

_REGISTERS = [0x1234, 0x5678]

# Expected combined value for each (word_order, byte_order) pair, worked out by hand:
#   HIGH_FIRST, BIG    -> registers used as-is                       -> 0x12345678
#   LOW_FIRST,  BIG     -> registers reversed                         -> 0x56781234
#   HIGH_FIRST, LITTLE -> bytes swapped within each register          -> 0x34127856
#   LOW_FIRST,  LITTLE -> registers reversed AND bytes swapped        -> 0x78563412
_EXPECTED_COMBINED = {
    (WordOrder.HIGH_FIRST, ByteOrder.BIG): 0x12345678,
    (WordOrder.LOW_FIRST, ByteOrder.BIG): 0x56781234,
    (WordOrder.HIGH_FIRST, ByteOrder.LITTLE): 0x34127856,
    (WordOrder.LOW_FIRST, ByteOrder.LITTLE): 0x78563412,
}


@pytest.mark.parametrize("word_order,byte_order", list(itertools.product(WordOrder, ByteOrder)))
def test_combine_values_honours_word_and_byte_order(word_order: WordOrder, byte_order: ByteOrder) -> None:
    expected = _EXPECTED_COMBINED[(word_order, byte_order)]
    assert ModbusParser.combine_values(_REGISTERS, word_order=word_order, byte_order=byte_order) == expected


@pytest.mark.parametrize("word_order,byte_order", list(itertools.product(WordOrder, ByteOrder)))
def test_values_to_value_honours_the_points_word_and_byte_order(word_order: WordOrder, byte_order: ByteOrder) -> None:
    point = _point(word_order=word_order, byte_order=byte_order)
    expected = _EXPECTED_COMBINED[(word_order, byte_order)]
    assert ModbusParser.values_to_value(_REGISTERS, point) == expected


# -------------------------------------------------------------------------------- encoding

@pytest.mark.parametrize("word_order,byte_order", list(itertools.product(WordOrder, ByteOrder)))
def test_encode_decode_round_trips_for_every_combination(word_order: WordOrder, byte_order: ByteOrder) -> None:
    point = _point(word_order=word_order, byte_order=byte_order)
    for value in (0, 1, 0x1234, 0x89AB, 0xFFFFFFFF):
        registers = ModbusParser.value_to_values(value, point)
        assert registers is not None
        assert ModbusParser.values_to_value(registers, point) == value


# Expected wire registers for encoding 0x12345678 (canonical big-endian words [0x1234, 0x5678]),
# worked out by hand the same way as _EXPECTED_COMBINED above.
_EXPECTED_REGISTERS = {
    (WordOrder.HIGH_FIRST, ByteOrder.BIG): [0x1234, 0x5678],
    (WordOrder.LOW_FIRST, ByteOrder.BIG): [0x5678, 0x1234],
    (WordOrder.HIGH_FIRST, ByteOrder.LITTLE): [0x3412, 0x7856],
    (WordOrder.LOW_FIRST, ByteOrder.LITTLE): [0x7856, 0x3412],
}


@pytest.mark.parametrize("word_order,byte_order", list(itertools.product(WordOrder, ByteOrder)))
def test_number_to_values_produces_the_register_layout_combine_values_expects(word_order: WordOrder, byte_order: ByteOrder) -> None:
    """
    Round-tripping alone would also pass with a no-op encoder, since a transform that is never
    applied on either side cancels out too. Pin the actual wire registers independently, so an
    encoder that stops transforming the registers cannot sneak through.
    """
    point = _point(word_order=word_order, byte_order=byte_order)
    registers = ModbusParser.number_to_values(0x12345678, point)
    assert registers == _EXPECTED_REGISTERS[(word_order, byte_order)]


# -------------------------------------------------------------- defaults match old behaviour

def test_defaults_reproduce_the_old_unconditional_combine_values():
    """
    The old implementation was exactly this one line, with no ordering concept at all:
        reduce(lambda acc, val: (acc << 16) | val, values, 0)
    combine_values() with no arguments (and a point built with no word_order/byte_order) must
    keep producing bit-for-bit the same result, so existing devices are unaffected.
    """
    values = [0xABCD, 0x0123, 0x4567]
    old_result = reduce(lambda acc, val: (acc << 16) | val, values, 0)

    assert ModbusParser.combine_values(values) == old_result

    point = ModbusSetpoint(key=SK.VALUE, read_address=1, write_address=1, read_length=3,
                           write_length=3)
    assert point.word_order == WordOrder.HIGH_FIRST
    assert point.byte_order == ByteOrder.BIG
    assert ModbusParser.values_to_value(values, point) == old_result
