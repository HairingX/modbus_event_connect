"""Points, data_types, access, selectors, the protocol contract's value types, and the clock."""
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from src.modbus_event_connect import unit as unit_module
from src.modbus_event_connect.clock import Clock, SystemClock
from src.modbus_event_connect.data_type import DataType, DataTypeKind
from src.modbus_event_connect.device import EncodedWrite, Outcome, ReadResult, WriteResult
from src.modbus_event_connect.micro_nabto.access import DatapointRegister, SetpointRegister
from src.modbus_event_connect.modbus.access import (
    Coil,
    DiscreteInput,
    HoldingRegister,
    InputRegister,
    ModbusOptions,
    NumberRange,
    RegisterNumbering,
    modicon,
    plain,
)
from src.modbus_event_connect.point import (
    DEFAULT_INTERVALS,
    Access,
    Change,
    Labels,
    Limits,
    Point,
    PollRate,
    Pulse,
    Refresh,
    Transform,
    Transforms,
    WriteKind,
)
from src.modbus_event_connect.testing.clock import FakeClock
from src.modbus_event_connect.unit import Unit
from src.modbus_event_connect.value import DataValue, Quality


def _refused(match: str, **fields: Any) -> None:
    with pytest.raises(ValueError, match=match):
        Point("p", **fields)


# ================================================================================== data_types

@pytest.mark.parametrize("data_type,registers", [
    (DataType.UINT16, 1), (DataType.INT16, 1), (DataType.BCD16, 1), (DataType.BOOL, 1), (DataType.bit(0), 1),
    (DataType.UINT32, 2), (DataType.INT32, 2), (DataType.FLOAT32, 2), (DataType.BCD32, 2),
    (DataType.UINT64, 4), (DataType.INT64, 4), (DataType.FLOAT64, 4),
    (DataType.string(16), 16), (DataType.enum({0: "off"}), 1), (DataType.enum({0: "off"}, DataTypeKind.UINT32), 2),
])
def test_a_data_type_knows_its_width(data_type: DataType, registers: int) -> None:
    assert data_type.registers == registers


def test_the_data_type_constants_are_distinct_kinds() -> None:
    constants = [DataType.UINT16, DataType.INT16, DataType.UINT32, DataType.INT32, DataType.UINT64, DataType.INT64, DataType.FLOAT32,
                 DataType.FLOAT64, DataType.BCD16, DataType.BCD32, DataType.BOOL]
    assert len({c.kind for c in constants}) == len(constants)


@pytest.mark.parametrize("build,match", [
    (lambda: DataType.bit(16), "bit from 0 to 15"),
    (lambda: DataType.bit(-1), "bit from 0 to 15"),
    (lambda: DataType(DataTypeKind.BIT), "bit from 0 to 15"),
    (lambda: DataType(DataTypeKind.UINT16, bit_index=3), "only a BIT data type has a bit"),
    (lambda: DataType.string(0), "length of at least one"),
    (lambda: DataType(DataTypeKind.UINT16, length=4), "only a STRING data type has a length"),
    (lambda: DataType.enum({}), "at least one state"),
    (lambda: DataType.enum({0: "on", 1: "on"}), "same name"),
    (lambda: DataType.enum({0: "off"}, DataTypeKind.FLOAT32), "binary integer"),
    (lambda: DataType.enum({0: "off"}, DataTypeKind.BCD16), "binary integer"),
    (lambda: DataType(DataTypeKind.UINT16, mapping={0: "x"}), "only an ENUM data type has a mapping"),
], ids=["bit-16", "bit-negative", "bit-missing", "bit-on-u16", "string-empty", "length-on-u16",
        "enum-empty", "enum-duplicate-name", "enum-on-float", "enum-on-bcd", "mapping-on-u16"])
def test_a_contradictory_data_type_is_refused(build: Callable[[], DataType], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build()


def test_an_unknown_string_encoding_is_refused_at_construction() -> None:
    with pytest.raises(LookupError):
        DataType.string(4, "no-such-encoding")


def test_an_enum_mapping_cannot_be_changed_after_construction() -> None:
    mapping = {0: "off", 1: "on"}
    data_type = DataType.enum(mapping)
    mapping[2] = "surprise"
    assert data_type.mapping is not None and 2 not in data_type.mapping


@pytest.mark.parametrize("data_type,numeric,integer,floating,boolean", [
    (DataType.UINT16, True, True, False, False),
    (DataType.BCD32, True, True, False, False),
    (DataType.FLOAT64, True, False, True, False),
    (DataType.BOOL, False, False, False, True),
    (DataType.bit(2), False, False, False, True),
    (DataType.string(2), False, False, False, False),
    (DataType.enum({0: "a"}), False, False, False, False),
])
def test_a_data_type_classifies_itself(data_type: DataType, numeric: bool, integer: bool, floating: bool,
                                   boolean: bool) -> None:
    assert (data_type.is_numeric, data_type.is_integer, data_type.is_float, data_type.is_boolean) == \
           (numeric, integer, floating, boolean)


# ============================================================================= valid points

@pytest.mark.parametrize("fields", [
    {"read": InputRegister(1)},
    {"write": HoldingRegister(1)},
    {"read": HoldingRegister(1), "write": HoldingRegister(1)},
    {"read": InputRegister(100), "write": HoldingRegister(200)},                    # status and command apart
    {"read": Coil(1), "write": Coil(1), "data_type": DataType.BOOL},
    {"read": DiscreteInput(1), "data_type": DataType.BOOL},
    {"read": HoldingRegister(1), "data_type": DataType.bit(3)},
    {"read": HoldingRegister(1), "write": HoldingRegister(1), "data_type": DataType.bit(15)},
    {"read": InputRegister(1), "data_type": DataType.string(8)},
    {"read": HoldingRegister(1), "write": HoldingRegister(1), "data_type": DataType.enum({0: "off", 1: "on"})},
    {"read": InputRegister(1), "data_type": DataType.INT16, "scale": 0.01, "no_data": (0x7FFF,), "deadband": 0.05},
    {"read": InputRegister(1), "data_type": DataType.UINT16, "raw_range": (0, 1000)},
    {"write": HoldingRegister(1), "write_kind": WriteKind.COMMAND, "pulse": Pulse(idle=0, after=1.0)},
    {"read": HoldingRegister(1), "write": HoldingRegister(1), "limits": Limits(5, 35, 0.5),
     "on_write": Refresh(["a", "b"], after=2, until_stable=30)},
    {"read": DiscreteInput(1), "data_type": DataType.BOOL, "on_change": Refresh(Labels(kind="alarm"))},
    {"read": InputRegister(1), "poll_always": True, "poll_rate": PollRate.STATIC, "unit": Unit.CELSIUS},
    {"read": InputRegister(1), "transform": Transforms.SECONDS_AS_MINUTES},
    {"read": DatapointRegister(27)},
    {"read": SetpointRegister(5), "write": SetpointRegister(5)},
], ids=lambda f: "-".join(sorted(f)))
def test_a_valid_point_is_accepted(fields: dict[str, Any]) -> None:
    Point("p", **fields)


# =========================================================================== refused points

def test_a_point_needs_a_side() -> None:
    _refused("read side, a write side, or both")


@pytest.mark.parametrize("key", ["", " padded ", "trailing "])
def test_a_key_must_be_a_clean_non_empty_string(key: str) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        Point(key, read=InputRegister(1))


@pytest.mark.parametrize("access", [InputRegister(1), DiscreteInput(1), DatapointRegister(1)])
def test_a_read_only_space_cannot_be_written(access: Access) -> None:
    data_type = DataType.BOOL if access.bits else DataType.UINT16
    _refused("cannot be written", write=access, data_type=data_type)


@pytest.mark.parametrize("side", ["read", "write"])
@pytest.mark.parametrize("data_type", [DataType.UINT16, DataType.INT32, DataType.bit(0), DataType.string(2)])
def test_a_bit_space_only_holds_booleans(side: str, data_type: DataType) -> None:
    _refused("must be BOOL", **{side: Coil(1)}, data_type=data_type)


@pytest.mark.parametrize("scale", [0, float("inf"), float("nan")])
def test_scale_must_be_finite_and_non_zero(scale: float) -> None:
    _refused("scale must be", read=InputRegister(1), scale=scale)


def test_offset_must_be_finite() -> None:
    _refused("offset must be finite", read=InputRegister(1), offset=float("nan"))


def test_precision_cannot_be_negative() -> None:
    _refused("precision cannot be negative", read=InputRegister(1), precision=-1)


@pytest.mark.parametrize("data_type", [DataType.BOOL, DataType.bit(1), DataType.string(2), DataType.enum({0: "a"})])
@pytest.mark.parametrize("fields,match", [
    ({"scale": 2}, "scale and offset apply to numbers"),
    ({"offset": 1}, "scale and offset apply to numbers"),
    ({"transform": Transforms.INVERT_BOOL}, "transform applies to numbers"),
    ({"deadband": 1.0}, "deadband applies to numbers"),
    ({"limits": Limits(0, 1)}, "limits apply to numbers"),
], ids=["scale", "offset", "transform", "deadband", "limits"])
def test_number_only_features_are_refused_on_other_data_types(data_type: DataType, fields: dict[str, Any],
                                                           match: str) -> None:
    _refused(match, read=HoldingRegister(1), write=HoldingRegister(1), data_type=data_type, **fields)


def test_no_data_needs_a_read_side() -> None:
    _refused("has no read side", write=HoldingRegister(1), no_data=(0xFFFF,))


@pytest.mark.parametrize("data_type", [DataType.FLOAT32, DataType.string(2), DataType.BOOL, DataType.enum({0: "a"})])
def test_no_data_and_raw_range_compare_raw_integers_only(data_type: DataType) -> None:
    _refused("compare raw integers", read=HoldingRegister(1), data_type=data_type, no_data=(0,))
    _refused("compare raw integers", read=HoldingRegister(1), data_type=data_type, raw_range=(0, 1))


def test_a_float_data_type_is_told_it_handles_nan_itself() -> None:
    _refused("NaN and infinity", read=HoldingRegister(1), data_type=DataType.FLOAT32, no_data=(0,))


def test_an_empty_raw_range_is_refused() -> None:
    _refused("is empty", read=InputRegister(1), raw_range=(10, 5))


def test_limits_need_a_write_side() -> None:
    _refused("limits describe writes", read=InputRegister(1), limits=Limits(0, 10))


def test_deadband_cannot_be_negative() -> None:
    _refused("deadband cannot be negative", read=InputRegister(1), deadband=-0.1)


def test_deadband_needs_a_read_side() -> None:
    _refused("deadband filters reads", write=HoldingRegister(1), deadband=0.1)


def test_poll_always_needs_a_read_side() -> None:
    _refused("poll_always=True asks for reads", write=HoldingRegister(1), poll_always=True)


def test_a_command_needs_a_write_side() -> None:
    _refused("COMMAND is written", read=InputRegister(1), write_kind=WriteKind.COMMAND)


def test_only_a_command_can_pulse() -> None:
    _refused("only a COMMAND can pulse", write=HoldingRegister(1), pulse=Pulse(idle=0, after=1))


def test_on_write_needs_a_write_side() -> None:
    _refused("on_write re-reads after a write", read=InputRegister(1), on_write=Refresh(["x"]))


def test_on_change_needs_a_read_side() -> None:
    _refused("on_change reacts to reads", write=HoldingRegister(1), on_change=Refresh(["x"]))


def test_a_label_needs_a_name() -> None:
    _refused("a label needs a name", read=InputRegister(1), labels={"": 1})


def test_every_problem_is_named_in_one_error() -> None:
    with pytest.raises(ValueError) as caught:
        Point("p", read=InputRegister(1), scale=0, limits=Limits(0, 1), poll_always=True, write_kind=WriteKind.COMMAND)
    message = str(caught.value)
    for fragment in ("scale must be", "limits describe writes", "COMMAND is written"):
        assert fragment in message


# ============================================================================ point values

def test_no_data_and_labels_are_frozen_copies() -> None:
    labels = {"room": 3}
    sentinels = [0x7FFF]
    point = Point("p", read=InputRegister(1), labels=labels, no_data=sentinels)
    labels["room"] = 4
    sentinels.append(0)
    assert point.labels == {"room": 3}
    assert point.no_data == frozenset({0x7FFF})
    with pytest.raises(TypeError):
        point.labels["room"] = 5  # type: ignore[index]


def test_a_point_is_immutable() -> None:
    point = Point("p", read=InputRegister(1))
    with pytest.raises(AttributeError):
        point.key = "q"  # type: ignore[misc]


def test_readable_writable_and_registers() -> None:
    point = Point("p", read=InputRegister(1), data_type=DataType.FLOAT64)
    assert (point.readable, point.writable, point.registers) == (True, False, 4)


@pytest.mark.parametrize("fields,precision", [
    ({}, 0),
    ({"scale": 0.1}, 1),
    ({"scale": 0.01}, 2),
    ({"scale": 0.5}, 1),
    ({"scale": 10}, 0),
    ({"scale": 1e-5}, 5),
    ({"scale": 0.1, "offset": 0.25}, 2),
    ({"scale": 0.1, "precision": 3}, 3),
    ({"scale": 0.1, "transform": Transforms.SECONDS_AS_MINUTES}, None),
    ({"data_type": DataType.FLOAT32, "scale": 0.1}, None),
    ({"data_type": DataType.FLOAT32, "precision": 2}, 2),
], ids=["plain", "tenths", "hundredths", "halves", "tens", "tiny", "offset-wins", "explicit",
        "transform", "float", "float-explicit"])
def test_the_rounding_follows_how_the_scale_was_written(fields: dict[str, Any], precision: int | None) -> None:
    assert Point("p", read=InputRegister(1), **fields).effective_precision == precision


# ================================================================== selectors and effects

def test_labels_match_when_every_pair_is_present() -> None:
    selector = Labels(room=3, kind="temp")
    assert selector.matches({"room": 3, "kind": "temp", "extra": "x"})
    assert not selector.matches({"room": 3})
    assert not selector.matches({"room": 4, "kind": "temp"})


def test_labels_compare_by_content() -> None:
    assert Labels(a=1, b=2) == Labels(b=2, a=1)
    assert hash(Labels(a=1, b=2)) == hash(Labels(b=2, a=1))
    assert Labels(a=1) != Labels(a=2)


def test_empty_labels_select_nothing_and_are_refused() -> None:
    with pytest.raises(ValueError):
        Labels()


def test_refresh_takes_keys_or_labels() -> None:
    assert Refresh(["a", "b"]).targets == ("a", "b")
    assert Refresh(Labels(room=1)).targets == Labels(room=1)


@pytest.mark.parametrize("build,error", [
    (lambda: Refresh("fan_speed"), TypeError),                  # a string is not a list of keys
    (lambda: Refresh([]), ValueError),
    (lambda: Refresh(["a"], after=-1), ValueError),
    (lambda: Refresh(["a"], until_stable=0), ValueError),
    (lambda: Refresh(["a"], after=0, until_stable=10), ValueError),   # re-reading needs an interval
    (lambda: Refresh(["a"], after=-0.5), ValueError),
    (lambda: Refresh(["a"], after=0, until_stable=5), ValueError),
], ids=["string", "empty", "negative-after", "zero-until-stable", "until-stable-without-after",
        "refresh-negative-after", "refresh-until-stable-without-after"])
def test_a_contradictory_effect_is_refused(build: Callable[[], object], error: type[Exception]) -> None:
    with pytest.raises(error):
        build()


def test_a_write_refresh_waits_as_its_point_does_and_a_change_refresh_not_at_all() -> None:
    point = Point("p", read=HoldingRegister(1), write=HoldingRegister(1),
                  on_write=Refresh(["a"]), on_change=Refresh(["b"]))
    assert point.on_write is not None and point.on_write.after is None
    assert point.on_change is not None and point.on_change.after == 0.0


def test_read_back_after_needs_a_write_side() -> None:
    _refused("read_back_after describes writes", read=InputRegister(1), read_back_after=2.0)


@pytest.mark.parametrize("seconds", [-1.0, float("inf"), float("nan")])
def test_read_back_after_must_be_a_finite_number_of_seconds(seconds: float) -> None:
    _refused("read_back_after must be", read=HoldingRegister(1), write=HoldingRegister(1),
             read_back_after=seconds)


def test_a_refresh_reacts_to_any_change_unless_told_otherwise() -> None:
    assert Refresh(["a"]).when is Change.ANY


def test_on_write_has_no_change_to_wait_for() -> None:
    _refused("`when` belongs to on_change", read=HoldingRegister(1), write=HoldingRegister(1),
             on_write=Refresh(["a"], when=Change.RISING))


def test_re_reading_until_stable_after_a_change_needs_an_interval() -> None:
    _refused("needs a positive after", read=HoldingRegister(1), on_change=Refresh(["a"], until_stable=5))


@pytest.mark.parametrize("build", [
    lambda: Limits(10, 5),
    lambda: Limits(step=0),
    lambda: Limits(step=-1),
    lambda: Pulse(idle=0, after=0),
])
def test_contradictory_limits_and_pulses_are_refused(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize("transform", [Transforms.INVERT_BOOL, Transforms.SECONDS_AS_MINUTES,
                                       Transforms.MINUTES_AS_HOURS, Transforms.HOURS_AS_DAYS])
@pytest.mark.parametrize("value", [0, 1, 59, 60, 90, 1439, 86400])
def test_every_built_in_transform_round_trips(transform: Transform, value: int) -> None:
    assert abs(transform.write(transform.read(value)) - value) < 1e-9


# ================================================================================= access

def test_an_address_cannot_be_negative() -> None:
    with pytest.raises(ValueError):
        HoldingRegister(-1)


def test_modbus_tables_are_separate_spaces() -> None:
    spaces = {InputRegister(1).space, HoldingRegister(1).space, DiscreteInput(1).space, Coil(1).space}
    assert len(spaces) == 4
    assert HoldingRegister(1).space == HoldingRegister(500).space


def test_nabto_objects_are_separate_spaces() -> None:
    assert DatapointRegister(1, obj=0).space != DatapointRegister(1, obj=1).space
    assert DatapointRegister(1).space != SetpointRegister(1).space
    with pytest.raises(ValueError):
        DatapointRegister(1, obj=-1)


@pytest.mark.parametrize("access,writable,bits", [
    (InputRegister(0), False, False), (HoldingRegister(0), True, False),
    (DiscreteInput(0), False, True), (Coil(0), True, True),
    (DatapointRegister(0), False, False), (SetpointRegister(0), True, False),
])
def test_each_space_says_what_it_allows(access: Access, writable: bool, bits: bool) -> None:
    assert (access.writable, access.bits) == (writable, bits)


# ======================================================================= modbus addressing

SPLIT_TABLE = RegisterNumbering(input_registers=[NumberRange(10001, 19999, address=0),
                                                 NumberRange(20001, 29999, address=9999)])


@pytest.mark.parametrize("numbering,access,address", [
    (plain(first_address=1), HoldingRegister(0), 0),
    (plain(first_address=1), HoldingRegister(65535), 65535),
    (plain(first_address=0), HoldingRegister(1), 0),
    (plain(first_address=0), InputRegister(120), 119),
    (modicon(digits=5, first_address=0), HoldingRegister(40001), 0),
    (modicon(digits=5, first_address=0), HoldingRegister(40120), 119),
    (modicon(digits=5, first_address=0), HoldingRegister(49999), 9998),
    (modicon(digits=5, first_address=0), InputRegister(30001), 0),
    (modicon(digits=5, first_address=0), DiscreteInput(10001), 0),
    (modicon(digits=5, first_address=0), Coil(1), 0),
    (modicon(digits=6, first_address=0), HoldingRegister(400001), 0),
    (modicon(digits=6, first_address=0), HoldingRegister(465536), 65535),
    (modicon(digits=6, first_address=0), InputRegister(300120), 119),
    (modicon(digits=6, first_address=0), DiscreteInput(100001), 0),
    (modicon(digits=6, first_address=0), Coil(65536), 65535),
    (modicon(digits=5, first_address=1), HoldingRegister(40001), 1),
    (modicon(digits=5, first_address=1), HoldingRegister(49999), 9999),
    (modicon(digits=5, first_address=1), Coil(1), 1),
    (modicon(digits=6, first_address=1), HoldingRegister(400001), 1),
    (modicon(digits=6, first_address=1), HoldingRegister(465535), 65535),
    (RegisterNumbering(holding_registers=[NumberRange(40001, 49999, address=1)]), HoldingRegister(40001), 1),
    (SPLIT_TABLE, InputRegister(19999), 9998),
    (SPLIT_TABLE, InputRegister(20001), 9999),
    (SPLIT_TABLE, InputRegister(20100), 10098),
])
def test_register_numbers_become_addresses(numbering: RegisterNumbering, access: Access,
                                           address: int) -> None:
    assert ModbusOptions(numbering=numbering).address(access) == address


@pytest.mark.parametrize("numbering,access", [
    (plain(first_address=1), HoldingRegister(65536)),
    (plain(first_address=0), HoldingRegister(0)),
    (modicon(digits=5, first_address=0), HoldingRegister(30001)),       # an input register number, in a holding access
    (modicon(digits=5, first_address=0), HoldingRegister(120)),         # not a reference number at all
    (modicon(digits=5, first_address=0), InputRegister(40001)),
    (modicon(digits=5, first_address=0), HoldingRegister(400001)),      # six digits, in a five-digit numbering
    (modicon(digits=6, first_address=0), HoldingRegister(465537)),
    (modicon(digits=5, first_address=0), Coil(0)),
    (SPLIT_TABLE, InputRegister(20000)),             # between the two ranges
    (SPLIT_TABLE, HoldingRegister(1)),               # a table the numbering gives no range
    (modicon(digits=6, first_address=1), HoldingRegister(465536)),   # would be address 65536
    (modicon(digits=5, first_address=1), HoldingRegister(40000)),
], ids=["past-end", "one-based-zero", "wrong-table", "not-a-reference", "input-as-holding",
        "six-digit-in-five", "past-six-digit-end", "coil-zero", "between-ranges", "table-without-ranges",
        "six-digit-from-one-past-end", "five-digit-from-one-below-start"])
def test_a_number_the_numbering_has_no_address_for_is_refused(numbering: RegisterNumbering,
                                                              access: Access) -> None:
    with pytest.raises(ValueError):
        ModbusOptions(numbering=numbering).address(access)


@pytest.mark.parametrize("make", [
    lambda: NumberRange(10, 9, address=0),
    lambda: NumberRange(1, 10, address=-1),
    lambda: NumberRange(1, 65537, address=0),
    lambda: NumberRange(40001, 49999, address=60000),
    lambda: RegisterNumbering(holding_registers=[NumberRange(1, 10, address=0), NumberRange(10, 20, address=100)]),
    lambda: modicon(digits=4, first_address=0),  # pyright: ignore[reportArgumentType]
    lambda: modicon(digits=5, first_address=2),  # pyright: ignore[reportArgumentType]
    lambda: plain(first_address=2),  # pyright: ignore[reportArgumentType]
], ids=["backwards", "negative-address", "too-many-numbers", "past-last-address", "ranges-share-a-number",
        "modicon-four-digits", "modicon-from-address-two", "plain-from-address-two"])
def test_a_numbering_that_cannot_be_meant_is_refused(make: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        make()


def test_numberings_with_the_same_ranges_are_equal() -> None:
    again = RegisterNumbering(holding_registers=[NumberRange(40001, 49999, address=0)],
                              input_registers=[NumberRange(30001, 39999, address=0)],
                              discrete_inputs=[NumberRange(10001, 19999, address=0)],
                              coils=[NumberRange(1, 9999, address=0)])
    assert again == modicon(digits=5, first_address=0) and hash(again) == hash(modicon(digits=5, first_address=0))
    assert modicon(digits=5, first_address=0) != modicon(digits=6, first_address=0)


def test_options_name_every_problem_of_a_point() -> None:
    options = ModbusOptions(numbering=modicon(digits=5, first_address=0), max_registers=32)
    assert options.problems(Point("ok", read=HoldingRegister(40001), write=HoldingRegister(40001))) == []
    assert options.problems(Point("wrong-table", read=HoldingRegister(30001)))
    assert options.problems(Point("too-wide", read=HoldingRegister(40001), data_type=DataType.string(40)))
    assert options.problems(Point("nabto", read=DatapointRegister(1)))


def test_a_multi_register_point_cannot_run_off_the_end() -> None:
    options = ModbusOptions(numbering=plain(first_address=1))
    assert options.problems(Point("edge", read=HoldingRegister(65534), data_type=DataType.UINT16)) == []
    assert options.problems(Point("over", read=HoldingRegister(65535), data_type=DataType.UINT32))


@pytest.mark.parametrize("build", [
    lambda: ModbusOptions(numbering=plain(first_address=1), max_registers=0),
    lambda: ModbusOptions(numbering=plain(first_address=1), max_registers=126),
    lambda: ModbusOptions(numbering=plain(first_address=1), max_bits=0),
    lambda: ModbusOptions(numbering=plain(first_address=1), max_bits=2001),
])
def test_device_limits_must_fit_the_protocol(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        build()


# ========================================================================= protocol values

def test_a_raw_read_has_registers_exactly_when_it_succeeded() -> None:
    ReadResult(Outcome.OK, (1,))
    ReadResult(Outcome.MISSING, exception_code=2)
    with pytest.raises(ValueError):
        ReadResult(Outcome.OK)
    with pytest.raises(ValueError):
        ReadResult(Outcome.OFFLINE, (1,))


@pytest.mark.parametrize("build", [
    lambda: EncodedWrite(),
    lambda: EncodedWrite((1,), bit_index=2),
    lambda: EncodedWrite(bit_index=16),
    lambda: EncodedWrite((0x10000,)),
    lambda: EncodedWrite((-1,)),
])
def test_an_encoded_write_is_registers_or_one_bit(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        build()


def test_a_write_result_reports_success() -> None:
    assert WriteResult(Outcome.OK).ok
    assert not WriteResult(Outcome.BUSY).ok


# ============================================================================ value, clock

def test_a_data_value_knows_whether_it_is_good() -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert DataValue(21.5, Quality.GOOD, when).is_good
    assert not DataValue(None, Quality.NO_DATA, when).is_good


def test_the_system_clock_satisfies_the_protocol() -> None:
    clock: Clock = SystemClock()
    assert isinstance(clock, Clock)
    assert clock.now().tzinfo is not None
    assert clock.monotonic() <= clock.monotonic()


def test_a_fake_clock_moves_both_clocks_only_when_told() -> None:
    clock = FakeClock()
    start, wall = clock.monotonic(), clock.now()
    assert (clock.monotonic(), clock.now()) == (start, wall)
    clock.advance(2.5)
    assert clock.monotonic() == start + 2.5
    assert (clock.now() - wall).total_seconds() == 2.5


def test_a_wall_clock_jump_leaves_the_scheduling_clock_alone() -> None:
    clock = FakeClock()
    start = clock.monotonic()
    clock.jump_wall(-3600)
    assert clock.monotonic() == start


def test_a_fake_clock_cannot_run_backwards_or_without_a_timezone() -> None:
    with pytest.raises(ValueError):
        FakeClock().advance(-1)
    with pytest.raises(ValueError):
        FakeClock(now=datetime(2026, 1, 1))


# ================================================================================== units

UNITS: dict[Unit, tuple[str, str]] = {
    Unit.CELSIUS: ("°C", "Cel"),
    Unit.FAHRENHEIT: ("°F", "[degF]"),
    Unit.KELVIN: ("K", "K"),
    Unit.PERCENT: ("%", "%"),
    Unit.PPM: ("ppm", "[ppm]"),
    Unit.MILLISECONDS: ("ms", "ms"),
    Unit.SECONDS: ("s", "s"),
    Unit.MINUTES: ("min", "min"),
    Unit.HOURS: ("h", "h"),
    Unit.DAYS: ("d", "d"),
    Unit.WEEKS: ("wk", "wk"),
    Unit.MONTHS: ("mo", "mo"),
    Unit.YEARS: ("a", "a"),
    Unit.WATT: ("W", "W"),
    Unit.KILOWATT: ("kW", "kW"),
    Unit.WATT_HOUR: ("W·h", "W.h"),
    Unit.KILOWATT_HOUR: ("kW·h", "kW.h"),
    Unit.VOLT: ("V", "V"),
    Unit.AMPERE: ("A", "A"),
    Unit.HERTZ: ("Hz", "Hz"),
    Unit.RPM: ("/min", "/min"),
    Unit.PASCAL: ("Pa", "Pa"),
    Unit.BAR: ("bar", "bar"),
    Unit.CUBIC_METERS_PER_HOUR: ("m³/h", "m3/h"),
    Unit.LITERS_PER_MINUTE: ("L/min", "L/min"),
    Unit.LITERS_PER_HOUR: ("L/h", "L/h"),
}
"""Symbols follow the SI's writing rules, and UCUM's where the SI has none; codes are UCUM's."""


def test_every_unit_has_its_standard_symbol_and_ucum_code() -> None:
    assert {unit: (unit.value, unit.code) for unit in Unit} == UNITS


def test_every_unit_states_its_code_rather_than_falling_back_to_its_symbol() -> None:
    """A code left out would otherwise be its symbol, which for a unit like m³ is no UCUM code."""
    assert set(unit_module._UCUM_CODES) == set(Unit)


def test_a_code_is_plain_ascii_without_spaces() -> None:
    assert all(unit.code.isascii() and " " not in unit.code for unit in Unit)


def test_a_symbol_can_be_shown_by_a_legacy_code_page() -> None:
    for unit in Unit:
        unit.value.encode("latin-1")


def test_a_month_is_not_written_as_the_symbol_for_a_metre() -> None:
    assert Unit.MONTHS != "m"


# ============================================================================= poll rates

def test_the_poll_rates_climb_from_seconds_to_a_quarter_of_an_hour() -> None:
    assert dict(DEFAULT_INTERVALS) == {PollRate.FAST: 10.0, PollRate.MEDIUM: 30.0, PollRate.SLOW: 60.0,
                                       PollRate.RARE: 900.0, PollRate.STATIC: None}


def test_a_point_is_polled_at_the_medium_rate_unless_it_says_otherwise() -> None:
    assert Point("p", read=InputRegister(1)).poll_rate is PollRate.MEDIUM
