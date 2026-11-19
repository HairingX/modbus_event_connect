"""The model walker: resolving a `Model` against an identity into a `ResolvedModel`, and every
rule that makes a model invalid."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest

from event_connect import (
    Key,
    Labels,
    Level,
    Limits,
    ModelError,
    PollRate,
    Refresh,
    Unit,
    WriteKind,
)
from event_connect import Identity
from modbus_event_connect._data_type import DataType
from modbus_event_connect._model import (
    Model,
    ModelSelector,
    RepeatedSection,
    Scan,
    Section,
    problems,
    resolve,
)
from modbus_event_connect._point import Point
from modbus_event_connect.modbus._access import (
    Coil,
    DiscreteInput,
    HoldingRegister,
    InputRegister,
    NumberRange,
    RegisterNumbering,
    modicon,
)
from modbus_event_connect.modbus import BitWrite
from models import modbus_model

# ============================================================================ happy path


def _room(n: int) -> list[Point[Any]]:
    base = 100 + (n - 1) * 20
    return [
        Point(Key(f"room_{n}_temp", float), read=InputRegister(base), data_type=DataType.INT16, scale=0.01, unit=Unit.CELSIUS, poll_rate=PollRate.MEDIUM),
        Point(Key(f"room_{n}_setpoint", float), read=HoldingRegister(base + 1), write=HoldingRegister(base + 1), data_type=DataType.INT16,
              scale=0.01, limits=Limits(5, 35, step=0.5), unit=Unit.CELSIUS, poll_rate=PollRate.RARE),
    ]


SENTIO_LIKE = modbus_model([RepeatedSection(_room, range(1, 25), label="room")], name="Sentio-like", manufacturer="Wavin")


def test_sentio_like_model_resolves_every_room() -> None:
    resolved = resolve(SENTIO_LIKE, {}, Level.DOCUMENTED)
    assert len(resolved.points) == 24 * 2
    assert resolved.instances["room"] == tuple(range(1, 25))


def test_sentio_like_labels_carry_the_instance_number() -> None:
    resolved = resolve(SENTIO_LIKE, {}, Level.DOCUMENTED)
    assert resolved.point("room_3_temp").labels["room"] == 3
    assert resolved.point("room_24_setpoint").labels["room"] == 24


def test_select_by_label_returns_exactly_that_instance() -> None:
    resolved = resolve(SENTIO_LIKE, {}, Level.DOCUMENTED)
    room3 = resolved.select(Labels(room=3))
    assert {point.key for point in room3} == {"room_3_temp", "room_3_setpoint"}


# ============================================================================ Section / RepeatedSection


def test_a_section_accepts_any_sequence_and_stores_a_tuple() -> None:
    points = [Point(Key("a", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)]
    section = Section(points)
    assert section.points == (points[0],)
    assert isinstance(section.points, tuple)


def test_instances_accepts_any_iterable_and_stores_a_tuple() -> None:
    instances = RepeatedSection(lambda n: [], (n for n in range(3)), label="x")
    assert instances.numbers == (0, 1, 2)
    assert isinstance(instances.numbers, tuple)


def test_model_accepts_sequences_and_stores_tuples() -> None:
    identity_point = Point(Key("id_point", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    section = Section([Point(Key("b", int), read=HoldingRegister(2), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)])

    async def step(scan: Scan) -> None:
        return None

    model = modbus_model([section], name="X", manufacturer="Y", identity_points=[identity_point], scan_steps=[step],
                         scan_check=PollRate.RARE)
    assert isinstance(model.sections, tuple)
    assert isinstance(model.identity_points, tuple)
    assert isinstance(model.poll_intervals, MappingProxyType)
    assert isinstance(model.scan_steps, tuple)


# ============================================================================ when


def test_a_section_with_when_none_is_always_included() -> None:
    p = Point(Key("always", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([p])], name="X", manufacturer="Y")
    assert "always" in resolve(model, {}, Level.DOCUMENTED).points


def _hardware_at_least_2(identity: Identity) -> bool:
    hardware = identity.get("hardware")
    return isinstance(hardware, (int, float)) and hardware >= 2


def test_a_feature_section_is_included_only_when_its_condition_holds() -> None:
    base = Point(Key("base", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    cooling = Point(Key("cooling_enabled", bool), read=Coil(1), data_type=DataType.BOOL, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([base]), Section([cooling], when=_hardware_at_least_2)], name="X", manufacturer="Y")

    old_hw = resolve(model, {"hardware": 1}, Level.DOCUMENTED)
    assert "cooling_enabled" not in old_hw.points

    new_hw = resolve(model, {"hardware": 2}, Level.DOCUMENTED)
    assert "cooling_enabled" in new_hw.points


def test_a_when_that_raises_is_a_problem_naming_the_section_and_excludes_it() -> None:
    def broken_when(identity: Identity) -> bool:
        raise RuntimeError("boom")

    p = Point(Key("p", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([p], when=broken_when)], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("when(identity) raised" in msg and "boom" in msg for msg in found)


# ============================================================================ Nilan-like selection


CTS400 = modbus_model([Section([Point(Key("base", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)])], name="CTS400", manufacturer="Nilan")
CTS600 = modbus_model([Section([Point(Key("base", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)])], name="CTS600", manufacturer="Nilan")


def _select_nilan_model(identity: Identity) -> Model | None:
    key: tuple[object, object, object] = (
        identity.get("device_model"), identity.get("slave_device_number"), identity.get("slave_device_model"))
    table: dict[tuple[object, object, object], Model] = {
        (1140, 72270, 1): CTS400,
        (1140, 72270, 2): CTS600,
    }
    return table.get(key)


nilan_selector: ModelSelector = _select_nilan_model


def test_nilan_like_decision_table_picks_the_matching_model() -> None:
    assert nilan_selector({"device_model": 1140, "slave_device_number": 72270, "slave_device_model": 1}) is CTS400


def test_nilan_like_decision_table_distinguishes_slave_device_model() -> None:
    assert nilan_selector({"device_model": 1140, "slave_device_number": 72270, "slave_device_model": 2}) is CTS600


def test_nilan_like_decision_table_returns_none_for_unknown_combination() -> None:
    assert nilan_selector({"device_model": 1, "slave_device_number": 2, "slave_device_model": 3}) is None


# ============================================================================ instances: factory / labels


def test_a_factory_that_raises_is_reported_with_its_instance_number() -> None:
    def flaky(n: int) -> list[Point[Any]]:
        if n == 2:
            raise ValueError("unlucky")
        return [Point(Key(f"p_{n}", int), read=InputRegister(n), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)]

    model = modbus_model([RepeatedSection(flaky, [1, 2, 3], label="unit")], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert len(found) == 1
    assert "instance 2" in found[0] and "factory raised" in found[0]


def test_instance_numbers_must_be_unique() -> None:
    model = modbus_model([RepeatedSection(lambda n: [Point(Key(f"p_{n}", int), read=InputRegister(n), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)],
                                     [1, 2, 2, 3], label="unit")], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("instance number 2 appears 2 times" in msg for msg in found)


def test_instance_label_must_not_be_empty() -> None:
    model = modbus_model([RepeatedSection(lambda n: [Point(Key(f"p_{n}", int), read=InputRegister(n), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)],
                                     [1, 2], label="")], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("label must not be empty" in msg for msg in found)


def test_two_repeated_sections_may_not_share_a_label() -> None:
    """A label names the instances each instance's scan covers."""
    def first(n: int) -> list[Point[Any]]:
        return [Point(Key(f"a_{n}", int), read=InputRegister(n), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)]

    def second(n: int) -> list[Point[Any]]:
        return [Point(Key(f"b_{n}", int), read=InputRegister(10 + n), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)]

    model = modbus_model([RepeatedSection(first, [1, 2], label="unit"),
                            RepeatedSection(second, [1, 2], label="unit")], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("already has the label 'unit'" in msg for msg in found)


def test_duplicate_keys_across_instances_are_reported() -> None:
    model = modbus_model([RepeatedSection(lambda n: [Point(Key("shared_key", int), read=InputRegister(n), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)],
                                     [1, 2], label="unit")], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("duplicate key 'shared_key'" in msg and "2 times" in msg for msg in found)


def test_a_point_already_carrying_the_instance_label_with_a_different_value_is_a_problem() -> None:
    def conflicting(n: int) -> list[Point[Any]]:
        return [Point(Key(f"p_{n}", int), read=InputRegister(n), data_type=DataType.UINT16, labels={"unit": 999}, poll_rate=PollRate.MEDIUM)]

    model = modbus_model([RepeatedSection(conflicting, [1], label="unit")], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("already labeled unit=999" in msg for msg in found)


# ============================================================================ duplicate keys


def test_duplicate_keys_between_identity_and_a_section_are_reported() -> None:
    a = Point(Key("dup", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    b = Point(Key("dup", int), read=HoldingRegister(2), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([b])], name="X", manufacturer="Y", identity_points=[a])
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("duplicate key 'dup'" in msg and "2 times" in msg for msg in found)


# ============================================================================ identity points


def test_identity_point_without_a_read_side_is_a_problem() -> None:
    write_only = Point(Key("cmd", int), write=HoldingRegister(1), data_type=DataType.UINT16, write_kind=WriteKind.COMMAND)
    model = modbus_model([], name="X", manufacturer="Y", identity_points=[write_only])
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("cmd" in msg and "read side" in msg for msg in found)


# ============================================================================ overlap


def test_partial_overlap_of_read_sides_is_a_problem() -> None:
    wide = Point(Key("wide", int), read=HoldingRegister(100), data_type=DataType.INT32, poll_rate=PollRate.MEDIUM)
    narrow = Point(Key("narrow", int), read=HoldingRegister(101), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([wide, narrow])], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("'wide'" in msg and "'narrow'" in msg and "overlap" in msg for msg in found)


def test_partial_overlap_of_write_sides_is_a_problem() -> None:
    wide = Point(Key("wide", int), read=HoldingRegister(100), write=HoldingRegister(100), data_type=DataType.INT32, poll_rate=PollRate.MEDIUM)
    narrow = Point(Key("narrow", int), read=HoldingRegister(101), write=HoldingRegister(101), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([wide, narrow])], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("write sides" in msg and "overlap" in msg for msg in found)


def test_identical_spans_are_allowed() -> None:
    view_a = Point(Key("view_a", int), read=HoldingRegister(100), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    view_b = Point(Key("view_b", int), read=HoldingRegister(100), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([view_a, view_b])], name="X", manufacturer="Y")
    assert problems(model, {}, Level.DOCUMENTED) == []


def test_bit_views_of_one_register_are_allowed() -> None:
    bit_a = Point(Key("bit_a", bool), read=HoldingRegister(50), data_type=DataType.bit(0), poll_rate=PollRate.MEDIUM)
    bit_b = Point(Key("bit_b", bool), read=HoldingRegister(50), data_type=DataType.bit(1), poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([bit_a, bit_b])], name="X", manufacturer="Y")
    assert problems(model, {}, Level.DOCUMENTED) == []


def test_different_address_spaces_never_overlap() -> None:
    in_input = Point(Key("in_input", int), read=InputRegister(100), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    in_holding = Point(Key("in_holding", int), read=HoldingRegister(100), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([in_input, in_holding])], name="X", manufacturer="Y")
    assert problems(model, {}, Level.DOCUMENTED) == []


def test_bit_spaces_allow_the_same_address_for_several_points() -> None:
    d1 = Point(Key("d1", bool), read=DiscreteInput(5), data_type=DataType.BOOL, poll_rate=PollRate.MEDIUM)
    d2 = Point(Key("d2", bool), read=DiscreteInput(5), data_type=DataType.BOOL, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([d1, d2])], name="X", manufacturer="Y")
    assert problems(model, {}, Level.DOCUMENTED) == []


# ========================================================================== read-back delay


@pytest.mark.parametrize("seconds,refused", [(0.0, True), (1.0, False)])
def test_re_reading_until_stable_needs_a_positive_read_back_delay(seconds: float, refused: bool) -> None:
    mode = Point(Key("mode", int), read=HoldingRegister(1), write=HoldingRegister(1),
                 on_write=Refresh(["fan"], until_stable=10.0), poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([mode, Point(Key("fan", int), read=InputRegister(2), poll_rate=PollRate.MEDIUM)])], name="X", manufacturer="Y", read_back_after=seconds)
    assert any("positive read-back delay" in msg for msg in problems(model, {}, Level.DOCUMENTED)) is refused


# =================================================================== on_write / on_change targets


def test_on_write_naming_an_unknown_key_is_a_problem() -> None:
    p = Point(Key("cause", int), read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.UINT16,
              on_write=Refresh(["does_not_exist"]), poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([p])], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("cause" in msg and "on_write target" in msg and "does_not_exist" in msg for msg in found)


def test_on_write_labels_matching_nothing_is_a_problem() -> None:
    p = Point(Key("cause", int), read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.UINT16,
              on_write=Refresh(Labels(room=99)), poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([p])], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("on_write target" in msg and "selects no point" in msg for msg in found)


def test_on_change_target_must_select_at_least_one_point() -> None:
    p = Point(Key("trigger", bool), read=DiscreteInput(1), data_type=DataType.BOOL, on_change=Refresh(["missing_key"]), poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([p])], name="X", manufacturer="Y")
    found = problems(model, {}, Level.DOCUMENTED)
    assert any("on_change target" in msg and "missing_key" in msg for msg in found)


# ============================================================================ protocol problems


def test_protocol_problems_are_prefixed_with_the_key() -> None:
    # 30001 is an input register number, not a holding register one.
    bad = Point(Key("bad", int), read=HoldingRegister(30001), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    found = problems(modbus_model([Section([bad])], numbering=modicon(digits=5, first_address=0)), {}, Level.DOCUMENTED)
    assert any(msg.startswith("'bad': ") for msg in found)


def test_points_overlap_by_address_even_when_their_numbers_do_not() -> None:
    numbering = RegisterNumbering(input_registers=[NumberRange(10001, 19999, address=0),
                                                   NumberRange(20001, 29999, address=9999)])
    wide = Point(Key("wide", int), read=InputRegister(19999), data_type=DataType.UINT32, poll_rate=PollRate.MEDIUM)   # addresses 9998-9999
    next_ = Point(Key("next", int), read=InputRegister(20001), poll_rate=PollRate.MEDIUM)                            # address 9999
    model = modbus_model([Section([wide, next_])], numbering=numbering)
    assert any("overlap" in msg for msg in problems(model, {}, Level.DOCUMENTED))


# ================================================================== what a model states


FAST_ONE = Point(Key("fast_one", int), read=InputRegister(1), poll_rate=PollRate.FAST)
SLOW_ONE = Point(Key("slow_one", int), read=InputRegister(2), poll_rate=PollRate.SLOW)
FOREVER = Point(Key("forever", int), read=InputRegister(3), poll_rate=PollRate.STATIC)


def _invalid(**changes: Any) -> str:
    """The message of the ModelError a model with `changes` raises when it is built."""
    with pytest.raises(ModelError) as raised:
        modbus_model(**changes)
    return str(raised.value)


def test_a_model_states_an_interval_for_every_rate_its_readable_points_use() -> None:
    message = _invalid(sections=[Section([FAST_ONE, SLOW_ONE])], poll_intervals={PollRate.FAST: 10.0})
    assert "no interval for SLOW, at which 'slow_one' is read" in message


def test_a_missing_interval_names_the_first_few_points_and_counts_the_rest() -> None:
    many = [Point(Key(f"p_{n}", int), read=InputRegister(n), poll_rate=PollRate.SLOW) for n in range(5)]
    message = _invalid(sections=[Section(many)], poll_intervals={})
    assert "'p_0', 'p_1', 'p_2' and 2 more are read" in message


def test_a_model_states_no_interval_for_a_rate_nothing_is_read_at() -> None:
    message = _invalid(sections=[Section([FAST_ONE])], poll_intervals={PollRate.FAST: 10.0, PollRate.RARE: 900.0})
    assert "gives RARE an interval, but nothing is read at it" in message


def test_the_rates_of_identity_points_and_of_every_instance_count() -> None:
    def room(n: int) -> list[Point[Any]]:
        return [Point(Key(f"room_{n}", int), read=InputRegister(10 + n), poll_rate=PollRate.MEDIUM)]

    message = _invalid(sections=[RepeatedSection(room, [1, 2], label="room")], identity_points=[FAST_ONE],
                       poll_intervals={})
    assert "no interval for FAST, at which 'fast_one' is read" in message
    assert "no interval for MEDIUM, at which 'room_1', 'room_2' are read" in message


def test_a_model_whose_points_are_all_static_needs_no_interval() -> None:
    assert modbus_model([Section([FOREVER])], poll_intervals={}).poll_intervals == {}


def test_an_interval_that_is_not_a_positive_number_is_refused() -> None:
    assert "timing: " in _invalid(sections=[Section([FAST_ONE])], poll_intervals={PollRate.FAST: 0.0})


def test_a_model_that_scans_needs_a_scan_check() -> None:
    async def step(scan: Scan) -> None:
        return None

    assert "it scans, so it needs a scan_check" in _invalid(scan_steps=[step])


def test_a_repeated_section_with_a_scan_is_a_scan() -> None:
    async def scan(scan: Scan, number: int) -> None:
        return None

    section = RepeatedSection(lambda n: [], [1], label="unit", scan=scan)
    assert "it scans, so it needs a scan_check" in _invalid(sections=[section])
    assert modbus_model([section], scan_check=PollRate.RARE).poll_intervals == {PollRate.RARE: 900.0}


def test_a_model_without_a_scan_takes_no_scan_check() -> None:
    message = _invalid(scan_check=PollRate.RARE, poll_intervals={PollRate.RARE: 900.0})
    assert "scan_check is RARE, but the model has no scan to check" in message


def test_the_scan_check_rate_needs_an_interval() -> None:
    async def step(scan: Scan) -> None:
        return None

    message = _invalid(scan_steps=[step], scan_check=PollRate.RARE, poll_intervals={})
    assert "no interval for RARE, at which the scan check is read" in message


@pytest.mark.parametrize("name,manufacturer", [("", "Y"), ("X", ""), ("", "")])
def test_an_empty_name_or_manufacturer_is_refused(name: str, manufacturer: str) -> None:
    message = _invalid(name=name, manufacturer=manufacturer)
    assert ("model name" in message) is (name == "") and ("manufacturer" in message) is (manufacturer == "")


def test_every_problem_of_a_model_is_named_at_once() -> None:
    message = _invalid(name="", sections=[Section([FAST_ONE])], poll_intervals={PollRate.SLOW: 60.0},
                       max_registers=0)
    assert "model name" in message and "FAST" in message and "SLOW" in message and "max_registers" in message


def test_a_model_knows_every_key_a_unit_of_it_may_have() -> None:
    def room(n: int) -> list[Point[Any]]:
        if n == 3:
            raise ValueError("no third room")
        return [Point(Key(f"room_{n}", int), read=InputRegister(10 + n), poll_rate=PollRate.MEDIUM)]

    model = modbus_model([Section([SLOW_ONE], when=lambda identity: False),
                          RepeatedSection(room, [1, 2, 3], label="room")], identity_points=[FAST_ONE])
    assert model.keys == {"fast_one", "slow_one", "room_1", "room_2"}


def test_a_model_with_a_point_that_writes_one_bit_of_a_holding_register_says_how() -> None:
    bit = Point(Key("bit", bool), read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.bit(2),
                poll_rate=PollRate.MEDIUM)
    assert "'bit' writes one bit of a holding register, so the model needs a bit_write" in _invalid(
        sections=[Section([bit])])
    assert modbus_model([Section([bit])], bit_write=BitWrite.MASK).bit_write is BitWrite.MASK


def test_a_model_with_no_point_that_writes_one_bit_takes_no_bit_write() -> None:
    coil = Point(Key("coil", bool), read=Coil(1), write=Coil(1), data_type=DataType.BOOL, poll_rate=PollRate.MEDIUM)
    message = _invalid(sections=[Section([coil])], bit_write=BitWrite.READ_MODIFY_WRITE)
    assert "bit_write is READ_MODIFY_WRITE, but no point writes one bit of a holding register" in message


@pytest.mark.parametrize("seconds", [0.0, -1.0, float("inf"), float("nan")])
def test_a_modbus_unit_answers_within_a_positive_time(seconds: float) -> None:
    assert "answers_within must be a finite number above 0" in _invalid(answers_within=seconds)


def test_a_model_is_itself_and_no_other() -> None:
    assert modbus_model() != modbus_model()


# ============================================================================ resolve / problems / ModelError


def test_resolve_raises_model_error_listing_every_problem() -> None:
    a = Point(Key("dup", int), read=HoldingRegister(1), poll_rate=PollRate.MEDIUM, on_change=Refresh(["nowhere"]))
    b = Point(Key("dup", int), read=HoldingRegister(2), poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([a, b])])
    with pytest.raises(ModelError) as excinfo:
        resolve(model, {}, Level.DOCUMENTED)
    message = str(excinfo.value)
    assert "duplicate key 'dup'" in message
    assert "nowhere" in message


def test_problems_never_raises_and_returns_an_empty_list_when_valid() -> None:
    model = modbus_model([], name="X", manufacturer="Y")
    assert problems(model, {}, Level.DOCUMENTED) == []


def test_resolve_succeeds_when_there_are_no_problems() -> None:
    p = Point(Key("ok", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([p])], name="X", manufacturer="Y")
    resolved = resolve(model, {}, Level.DOCUMENTED)
    assert resolved.point("ok") is p


# ============================================================================ ResolvedModel


def test_resolved_model_select_by_keys_preserves_the_given_order() -> None:
    a = Point(Key("a", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    b = Point(Key("b", int), read=HoldingRegister(2), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([a, b])], name="X", manufacturer="Y")
    resolved = resolve(model, {}, Level.DOCUMENTED)
    assert resolved.select(("b", "a")) == (b, a)


def test_resolved_model_select_by_keys_raises_naming_every_unknown_key() -> None:
    a = Point(Key("a", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([a])], name="X", manufacturer="Y")
    resolved = resolve(model, {}, Level.DOCUMENTED)
    with pytest.raises(KeyError, match="missing_one.*missing_two|missing_two.*missing_one"):
        resolved.select(("a", "missing_one", "missing_two"))


def test_resolved_model_select_by_labels_may_be_empty() -> None:
    a = Point(Key("a", int), read=HoldingRegister(1), data_type=DataType.UINT16, labels={"room": 1}, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([a])], name="X", manufacturer="Y")
    resolved = resolve(model, {}, Level.DOCUMENTED)
    assert resolved.select(Labels(room=99)) == ()


def test_resolved_model_point_raises_key_error_naming_the_key() -> None:
    model = modbus_model([], name="X", manufacturer="Y")
    resolved = resolve(model, {}, Level.DOCUMENTED)
    with pytest.raises(KeyError, match="missing"):
        resolved.point("missing")


def test_resolved_model_points_are_in_declaration_order_identity_first() -> None:
    id_point = Point(Key("id_point", int), read=HoldingRegister(1), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    section_point = Point(Key("section_point", int), read=HoldingRegister(2), data_type=DataType.UINT16, poll_rate=PollRate.MEDIUM)
    model = modbus_model([Section([section_point])], name="X", manufacturer="Y", identity_points=[id_point])
    resolved = resolve(model, {}, Level.DOCUMENTED)
    assert list(resolved.points.keys()) == ["id_point", "section_point"]
