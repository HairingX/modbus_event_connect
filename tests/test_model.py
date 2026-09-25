"""The model walker: resolving a `Model` against an identity into a `ResolvedModel`, and every
rule that makes a model invalid."""
from __future__ import annotations

from typing import Any

import pytest

from src.modbus_event_connect._data_type import DataType
from src.modbus_event_connect._device import Identity
from src.modbus_event_connect._errors import ModelError
from src.modbus_event_connect._key import Key
from src.modbus_event_connect._model import (
    Instances,
    Model,
    ModelSelector,
    Scan,
    Section,
    problems,
    resolve,
)
from src.modbus_event_connect._point import Labels, Limits, Point, PollRate, Refresh, WriteKind
from src.modbus_event_connect._unit import Unit
from src.modbus_event_connect.modbus._access import (
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

# ============================================================================ happy path


def _room(n: int) -> list[Point[Any]]:
    base = 100 + (n - 1) * 20
    return [
        Point(Key(f"room_{n}_temp", float), read=InputRegister(base), data_type=DataType.INT16, scale=0.01, unit=Unit.CELSIUS),
        Point(Key(f"room_{n}_setpoint", float), read=HoldingRegister(base + 1), write=HoldingRegister(base + 1), data_type=DataType.INT16,
              scale=0.01, limits=Limits(5, 35, step=0.5), unit=Unit.CELSIUS, poll_rate=PollRate.RARE),
    ]


SENTIO_LIKE = Model(
    name="Sentio-like", manufacturer="Wavin",
    sections=[Instances(_room, range(1, 25), label="room")], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0
)


def test_sentio_like_model_resolves_every_room() -> None:
    resolved = resolve(SENTIO_LIKE, {})
    assert len(resolved.points) == 24 * 2
    assert resolved.instances["room"] == tuple(range(1, 25))


def test_sentio_like_labels_carry_the_instance_number() -> None:
    resolved = resolve(SENTIO_LIKE, {})
    assert resolved.point("room_3_temp").labels["room"] == 3
    assert resolved.point("room_24_setpoint").labels["room"] == 24


def test_select_by_label_returns_exactly_that_instance() -> None:
    resolved = resolve(SENTIO_LIKE, {})
    room3 = resolved.select(Labels(room=3))
    assert {point.key for point in room3} == {"room_3_temp", "room_3_setpoint"}


# ============================================================================ Section / Instances


def test_a_section_accepts_any_sequence_and_stores_a_tuple() -> None:
    points = [Point(Key("a", int), read=HoldingRegister(1), data_type=DataType.UINT16)]
    section = Section(points)
    assert section.points == (points[0],)
    assert isinstance(section.points, tuple)


def test_instances_accepts_any_iterable_and_stores_a_tuple() -> None:
    instances = Instances(lambda n: [], (n for n in range(3)), label="x")
    assert instances.numbers == (0, 1, 2)
    assert isinstance(instances.numbers, tuple)


def test_model_accepts_sequences_and_stores_tuples() -> None:
    identity_point = Point(Key("id_point", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    section = Section([Point(Key("b", int), read=HoldingRegister(2), data_type=DataType.UINT16)])

    async def step(scan: Scan) -> None:
        return None

    model = Model(name="X", manufacturer="Y", sections=[section], identity_points=[identity_point], scan_steps=[step], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    assert isinstance(model.sections, tuple)
    assert isinstance(model.identity_points, tuple)
    assert isinstance(model.scan_steps, tuple)


# ============================================================================ when


def test_a_section_with_when_none_is_always_included() -> None:
    p = Point(Key("always", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([p])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    assert "always" in resolve(model, {}).points


def _hardware_at_least_2(identity: Identity) -> bool:
    hardware = identity.get("hardware")
    return isinstance(hardware, (int, float)) and hardware >= 2


def test_a_feature_section_is_included_only_when_its_condition_holds() -> None:
    base = Point(Key("base", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    cooling = Point(Key("cooling_enabled", bool), read=Coil(1), data_type=DataType.BOOL)
    model = Model(name="X", manufacturer="Y",
                  sections=[Section([base]), Section([cooling], when=_hardware_at_least_2)], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)

    old_hw = resolve(model, {"hardware": 1})
    assert "cooling_enabled" not in old_hw.points

    new_hw = resolve(model, {"hardware": 2})
    assert "cooling_enabled" in new_hw.points


def test_a_when_that_raises_is_a_problem_naming_the_section_and_excludes_it() -> None:
    def broken_when(identity: Identity) -> bool:
        raise RuntimeError("boom")

    p = Point(Key("p", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([p], when=broken_when)], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("when(identity) raised" in msg and "boom" in msg for msg in found)


# ============================================================================ Nilan-like selection


CTS400 = Model(name="CTS400", manufacturer="Nilan",
                sections=[Section([Point(Key("base", int), read=HoldingRegister(1), data_type=DataType.UINT16)])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
CTS600 = Model(name="CTS600", manufacturer="Nilan",
                sections=[Section([Point(Key("base", int), read=HoldingRegister(1), data_type=DataType.UINT16)])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)


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
        return [Point(Key(f"p_{n}", int), read=InputRegister(n), data_type=DataType.UINT16)]

    model = Model(name="X", manufacturer="Y", sections=[Instances(flaky, [1, 2, 3], label="unit")], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert len(found) == 1
    assert "instance 2" in found[0] and "factory raised" in found[0]


def test_instance_numbers_must_be_unique() -> None:
    model = Model(name="X", manufacturer="Y",
                  sections=[Instances(lambda n: [Point(Key(f"p_{n}", int), read=InputRegister(n), data_type=DataType.UINT16)],
                                     [1, 2, 2, 3], label="unit")], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("instance number 2 appears 2 times" in msg for msg in found)


def test_instance_label_must_not_be_empty() -> None:
    model = Model(name="X", manufacturer="Y",
                  sections=[Instances(lambda n: [Point(Key(f"p_{n}", int), read=InputRegister(n), data_type=DataType.UINT16)],
                                     [1, 2], label="")], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("label must not be empty" in msg for msg in found)


def test_duplicate_keys_across_instances_are_reported() -> None:
    model = Model(name="X", manufacturer="Y",
                  sections=[Instances(lambda n: [Point(Key("shared_key", int), read=InputRegister(n), data_type=DataType.UINT16)],
                                     [1, 2], label="unit")], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("duplicate key 'shared_key'" in msg and "2 times" in msg for msg in found)


def test_a_point_already_carrying_the_instance_label_with_a_different_value_is_a_problem() -> None:
    def conflicting(n: int) -> list[Point[Any]]:
        return [Point(Key(f"p_{n}", int), read=InputRegister(n), data_type=DataType.UINT16, labels={"unit": 999})]

    model = Model(name="X", manufacturer="Y", sections=[Instances(conflicting, [1], label="unit")], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("already labeled unit=999" in msg for msg in found)


# ============================================================================ duplicate keys


def test_duplicate_keys_between_identity_and_a_section_are_reported() -> None:
    a = Point(Key("dup", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    b = Point(Key("dup", int), read=HoldingRegister(2), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([b])], identity_points=[a], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("duplicate key 'dup'" in msg and "2 times" in msg for msg in found)


# ============================================================================ identity points


def test_identity_point_without_a_read_side_is_a_problem() -> None:
    write_only = Point(Key("cmd", int), write=HoldingRegister(1), data_type=DataType.UINT16, write_kind=WriteKind.COMMAND)
    model = Model(name="X", manufacturer="Y", sections=[], identity_points=[write_only], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("cmd" in msg and "read side" in msg for msg in found)


# ============================================================================ overlap


def test_partial_overlap_of_read_sides_is_a_problem() -> None:
    wide = Point(Key("wide", int), read=HoldingRegister(100), data_type=DataType.INT32)
    narrow = Point(Key("narrow", int), read=HoldingRegister(101), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([wide, narrow])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("'wide'" in msg and "'narrow'" in msg and "overlap" in msg for msg in found)


def test_partial_overlap_of_write_sides_is_a_problem() -> None:
    wide = Point(Key("wide", int), read=HoldingRegister(100), write=HoldingRegister(100), data_type=DataType.INT32)
    narrow = Point(Key("narrow", int), read=HoldingRegister(101), write=HoldingRegister(101), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([wide, narrow])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("write sides" in msg and "overlap" in msg for msg in found)


def test_identical_spans_are_allowed() -> None:
    view_a = Point(Key("view_a", int), read=HoldingRegister(100), data_type=DataType.UINT16)
    view_b = Point(Key("view_b", int), read=HoldingRegister(100), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([view_a, view_b])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    assert problems(model, {}) == []


def test_bit_views_of_one_register_are_allowed() -> None:
    bit_a = Point(Key("bit_a", bool), read=HoldingRegister(50), data_type=DataType.bit(0))
    bit_b = Point(Key("bit_b", bool), read=HoldingRegister(50), data_type=DataType.bit(1))
    model = Model(name="X", manufacturer="Y", sections=[Section([bit_a, bit_b])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    assert problems(model, {}) == []


def test_different_address_spaces_never_overlap() -> None:
    in_input = Point(Key("in_input", int), read=InputRegister(100), data_type=DataType.UINT16)
    in_holding = Point(Key("in_holding", int), read=HoldingRegister(100), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([in_input, in_holding])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    assert problems(model, {}) == []


def test_bit_spaces_allow_the_same_address_for_several_points() -> None:
    d1 = Point(Key("d1", bool), read=DiscreteInput(5), data_type=DataType.BOOL)
    d2 = Point(Key("d2", bool), read=DiscreteInput(5), data_type=DataType.BOOL)
    model = Model(name="X", manufacturer="Y", sections=[Section([d1, d2])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    assert problems(model, {}) == []


# ========================================================================== read-back delay


def test_the_read_back_delay_is_the_write_effects_then_the_points_then_the_devices() -> None:
    device_delay = Point(Key("a", int), read=HoldingRegister(1), write=HoldingRegister(1))
    own = Point(Key("b", int), read=HoldingRegister(2), write=HoldingRegister(2), read_back_after=4.0)
    effect = Point(Key("c", int), read=HoldingRegister(3), write=HoldingRegister(3), read_back_after=4.0,
                   on_write=Refresh(["a"], after=9.0))
    model = Model(name="X", manufacturer="Y", sections=[Section([device_delay, own, effect])],
                  options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.5)
    assert [model.read_back_delay(p) for p in (device_delay, own, effect)] == [1.5, 4.0, 9.0]


@pytest.mark.parametrize("seconds", [-1.0, float("inf"), float("nan")])
def test_a_device_read_back_delay_must_be_a_finite_number_of_seconds(seconds: float) -> None:
    model = Model(name="X", manufacturer="Y", sections=[Section([Point(Key("a", int), read=HoldingRegister(1))])],
                  options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=seconds)
    assert any("read_back_after must be" in msg for msg in problems(model, {}))


@pytest.mark.parametrize("seconds,refused", [(0.0, True), (1.0, False)])
def test_re_reading_until_stable_needs_a_positive_read_back_delay(seconds: float, refused: bool) -> None:
    mode = Point(Key("mode", int), read=HoldingRegister(1), write=HoldingRegister(1),
                 on_write=Refresh(["fan"], until_stable=10.0))
    model = Model(name="X", manufacturer="Y", sections=[Section([mode, Point(Key("fan", int), read=InputRegister(2))])],
                  options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=seconds)
    assert any("positive read-back delay" in msg for msg in problems(model, {})) is refused


# =================================================================== on_write / on_change targets


def test_on_write_naming_an_unknown_key_is_a_problem() -> None:
    p = Point(Key("cause", int), read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.UINT16,
              on_write=Refresh(["does_not_exist"]))
    model = Model(name="X", manufacturer="Y", sections=[Section([p])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("cause" in msg and "on_write target" in msg and "does_not_exist" in msg for msg in found)


def test_on_write_labels_matching_nothing_is_a_problem() -> None:
    p = Point(Key("cause", int), read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.UINT16,
              on_write=Refresh(Labels(room=99)))
    model = Model(name="X", manufacturer="Y", sections=[Section([p])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("on_write target" in msg and "selects no point" in msg for msg in found)


def test_on_change_target_must_select_at_least_one_point() -> None:
    p = Point(Key("trigger", bool), read=DiscreteInput(1), data_type=DataType.BOOL, on_change=Refresh(["missing_key"]))
    model = Model(name="X", manufacturer="Y", sections=[Section([p])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("on_change target" in msg and "missing_key" in msg for msg in found)


# ============================================================================ protocol problems


def test_protocol_problems_are_prefixed_with_the_key() -> None:
    # 30001 is an input register number, not a holding register one.
    bad = Point(Key("bad", int), read=HoldingRegister(30001), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([bad])],
                  options=ModbusOptions(numbering=modicon(digits=5, first_address=0)), read_back_after=1.0)
    found = problems(model, {})
    assert any(msg.startswith("'bad': ") for msg in found)


def test_points_overlap_by_address_even_when_their_numbers_do_not() -> None:
    numbering = RegisterNumbering(input_registers=[NumberRange(10001, 19999, address=0),
                                                   NumberRange(20001, 29999, address=9999)])
    wide = Point(Key("wide", int), read=InputRegister(19999), data_type=DataType.UINT32)   # addresses 9998-9999
    next_ = Point(Key("next", int), read=InputRegister(20001))                            # address 9999
    model = Model(name="X", manufacturer="Y", sections=[Section([wide, next_])],
                  options=ModbusOptions(numbering=numbering), read_back_after=1.0)
    assert any("overlap" in msg for msg in problems(model, {}))


# ============================================================================ intervals


def test_min_interval_must_not_be_negative() -> None:
    model = Model(name="X", manufacturer="Y", sections=[], min_poll_interval=-1, options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("min_poll_interval" in msg for msg in found)


@pytest.mark.parametrize("interval", [0, -5])
def test_an_interval_must_be_positive_or_none(interval: float) -> None:
    model = Model(name="X", manufacturer="Y", sections=[],
                  poll_intervals={PollRate.FAST: interval, PollRate.SLOW: None, PollRate.RARE: None, PollRate.STATIC: None}, options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("must be positive or None" in msg for msg in found)


def test_an_interval_below_min_interval_is_a_problem() -> None:
    model = Model(name="X", manufacturer="Y", sections=[],
                  poll_intervals={PollRate.FAST: 1.0, PollRate.SLOW: 60.0, PollRate.RARE: 900.0, PollRate.STATIC: None},
                  min_poll_interval=5.0, options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert any("below min_poll_interval" in msg for msg in found)


def test_default_intervals_and_min_interval_cause_no_problem() -> None:
    model = Model(name="X", manufacturer="Y", sections=[], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    assert problems(model, {}) == []


# ============================================================================ name / manufacturer


@pytest.mark.parametrize("name,manufacturer", [("", "Y"), ("X", ""), ("", "")])
def test_empty_name_or_manufacturer_is_a_problem(name: str, manufacturer: str) -> None:
    model = Model(name=name, manufacturer=manufacturer, sections=[], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    found = problems(model, {})
    assert found


# ============================================================================ resolve / problems / ModelError


def test_resolve_raises_model_error_listing_every_problem() -> None:
    model = Model(name="", manufacturer="", sections=[], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    with pytest.raises(ModelError) as excinfo:
        resolve(model, {})
    message = str(excinfo.value)
    assert "model name" in message
    assert "manufacturer" in message


def test_problems_never_raises_and_returns_an_empty_list_when_valid() -> None:
    model = Model(name="X", manufacturer="Y", sections=[], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    assert problems(model, {}) == []


def test_resolve_succeeds_when_there_are_no_problems() -> None:
    p = Point(Key("ok", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([p])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    resolved = resolve(model, {})
    assert resolved.point("ok") is p


# ============================================================================ ResolvedModel


def test_resolved_model_select_by_keys_preserves_the_given_order() -> None:
    a = Point(Key("a", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    b = Point(Key("b", int), read=HoldingRegister(2), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([a, b])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    resolved = resolve(model, {})
    assert resolved.select(("b", "a")) == (b, a)


def test_resolved_model_select_by_keys_raises_naming_every_unknown_key() -> None:
    a = Point(Key("a", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([a])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    resolved = resolve(model, {})
    with pytest.raises(KeyError, match="missing_one.*missing_two|missing_two.*missing_one"):
        resolved.select(("a", "missing_one", "missing_two"))


def test_resolved_model_select_by_labels_may_be_empty() -> None:
    a = Point(Key("a", int), read=HoldingRegister(1), data_type=DataType.UINT16, labels={"room": 1})
    model = Model(name="X", manufacturer="Y", sections=[Section([a])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    resolved = resolve(model, {})
    assert resolved.select(Labels(room=99)) == ()


def test_resolved_model_point_raises_key_error_naming_the_key() -> None:
    model = Model(name="X", manufacturer="Y", sections=[], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    resolved = resolve(model, {})
    with pytest.raises(KeyError, match="missing"):
        resolved.point("missing")


def test_resolved_model_points_are_in_declaration_order_identity_first() -> None:
    id_point = Point(Key("id_point", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    section_point = Point(Key("section_point", int), read=HoldingRegister(2), data_type=DataType.UINT16)
    model = Model(name="X", manufacturer="Y", sections=[Section([section_point])], identity_points=[id_point], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)
    resolved = resolve(model, {})
    assert list(resolved.points.keys()) == ["id_point", "section_point"]
