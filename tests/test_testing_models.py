"""`assert_models_valid`: the check a device repository runs over its models in its tests."""
import pytest

from src.modbus_event_connect._data_type import DataType
from src.modbus_event_connect._device import Identity
from src.modbus_event_connect._key import Key
from src.modbus_event_connect._model import Model, Section, problems, resolve
from src.modbus_event_connect._point import Point, Transform, Transforms
from src.modbus_event_connect.modbus._access import HoldingRegister, ModbusOptions, plain
from src.modbus_event_connect.testing._models import assert_models_valid


def _with_transform(transform: Transform) -> Model:
    point = Point(Key("value", float), read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.INT16,
                  transform=transform)
    return Model(name="X", manufacturer="Y", sections=[Section([point])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)


# ============================================================================ variants

def test_at_least_one_identity_is_needed() -> None:
    with pytest.raises(ValueError, match="at least one identity"):
        assert_models_valid(Model(name="X", manufacturer="Y", sections=[], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0), identities=[])


def test_a_model_that_resolves_passes_silently() -> None:
    good = Point(Key("good", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    assert_models_valid(Model(name="X", manufacturer="Y", sections=[Section([good])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0), identities=[{}])


def test_a_section_broken_only_under_one_identity_is_caught() -> None:
    wide = Point(Key("wide", int), read=HoldingRegister(100), data_type=DataType.INT32)
    narrow = Point(Key("narrow", int), read=HoldingRegister(101), data_type=DataType.UINT16)
    broken = Section([wide, narrow], when=lambda identity: identity.get("variant") == "broken")
    identities: list[Identity] = [{"variant": "ok"}, {"variant": "broken"}]
    with pytest.raises(AssertionError, match="overlap"):
        assert_models_valid(Model(name="X", manufacturer="Y", sections=[broken], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0), identities=identities)


def test_a_section_no_identity_includes_is_reported_as_untested() -> None:
    p = Point(Key("feature", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    unreachable = Section([p], when=lambda identity: identity.get("variant") == "never-happens")
    identities: list[Identity] = [{"variant": "a"}, {"variant": "b"}]
    with pytest.raises(AssertionError, match="never resolved"):
        assert_models_valid(Model(name="X", manufacturer="Y", sections=[unreachable], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0), identities=identities)


def test_a_section_without_a_condition_is_never_reported_as_untested() -> None:
    p = Point(Key("always", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    assert_models_valid(Model(name="X", manufacturer="Y", sections=[Section([p])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0), identities=[{}])


# ========================================================================== transforms

def test_a_transform_that_round_trips_passes() -> None:
    assert_models_valid(_with_transform(Transforms.SECONDS_AS_MINUTES), identities=[{}])


def test_a_lossy_transform_is_caught() -> None:
    lossy = Transform(read=lambda s: round(s / 60), write=lambda m: m * 60, name="lossy_minutes")
    with pytest.raises(AssertionError, match="'value': lossy_minutes does not round-trip"):
        assert_models_valid(_with_transform(lossy), identities=[{}])


def test_a_transform_that_fails_on_negative_values_is_caught() -> None:
    positive_only = Transform(read=abs, write=lambda v: v, name="positive_only")
    with pytest.raises(AssertionError, match=r"positive_only does not round-trip: write\(read\(-7.5\)\)"):
        assert_models_valid(_with_transform(positive_only), identities=[{}])


def test_a_transform_that_drops_fractions_is_caught() -> None:
    whole_only = Transform(read=lambda v: float(int(v)) if v >= 0 else v, write=lambda v: v, name="whole_only")
    with pytest.raises(AssertionError, match=r"whole_only does not round-trip: write\(read\(0.5\)\)"):
        assert_models_valid(_with_transform(whole_only), identities=[{}])


def test_a_transform_that_raises_is_caught() -> None:
    def explode(_: float) -> float:
        raise ZeroDivisionError("nope")
    with pytest.raises(AssertionError, match="'value': broken raised"):
        assert_models_valid(_with_transform(Transform(read=explode, write=lambda v: v, name="broken")),
                            identities=[{}])


def test_resolving_a_model_leaves_sampling_its_transforms_to_the_tests() -> None:
    lossy = _with_transform(Transform(read=lambda s: round(s / 60), write=lambda m: m * 60))
    assert problems(lossy, {}) == []
    assert "value" in resolve(lossy, {}).points
