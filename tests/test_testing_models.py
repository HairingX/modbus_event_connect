"""`assert_models_valid`: the check a device repository runs over its models in its tests."""
import pytest

from event_connect import Key, Timing, Transform, Transforms
from modbus_event_connect._data_type import DataType
from modbus_event_connect._device import Identity
from modbus_event_connect._model import Model, Section, problems, resolve
from modbus_event_connect._point import Point
from modbus_event_connect.modbus._access import HoldingRegister
from modbus_event_connect.testing._models import assert_models_valid
from profiles import modbus


def _with_transform(transform: Transform) -> Model:
    point = Point(Key("value", float), read=HoldingRegister(1), write=HoldingRegister(1), data_type=DataType.INT16,
                  transform=transform)
    return Model(name="X", manufacturer="Y", sections=[Section([point])], timing=Timing(read_back_after=1.0))


def _valid(model: Model, *, identities: list[Identity]) -> None:
    assert_models_valid([model], [modbus(model)], identities=identities)


# ============================================================================ variants

def test_at_least_one_identity_is_needed() -> None:
    with pytest.raises(ValueError, match="at least one identity"):
        _valid(Model(name="X", manufacturer="Y", sections=[], timing=Timing(read_back_after=1.0)), identities=[])


def test_a_model_that_resolves_passes_silently() -> None:
    good = Point(Key("good", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    _valid(Model(name="X", manufacturer="Y", sections=[Section([good])], timing=Timing(read_back_after=1.0)), identities=[{}])


def test_a_section_broken_only_under_one_identity_is_caught() -> None:
    wide = Point(Key("wide", int), read=HoldingRegister(100), data_type=DataType.INT32)
    narrow = Point(Key("narrow", int), read=HoldingRegister(101), data_type=DataType.UINT16)
    broken = Section([wide, narrow], when=lambda identity: identity.get("variant") == "broken")
    identities: list[Identity] = [{"variant": "ok"}, {"variant": "broken"}]
    with pytest.raises(AssertionError, match="overlap"):
        _valid(Model(name="X", manufacturer="Y", sections=[broken], timing=Timing(read_back_after=1.0)), identities=identities)


def test_a_section_no_identity_includes_is_reported_as_untested() -> None:
    p = Point(Key("feature", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    unreachable = Section([p], when=lambda identity: identity.get("variant") == "never-happens")
    identities: list[Identity] = [{"variant": "a"}, {"variant": "b"}]
    with pytest.raises(AssertionError, match="never resolved"):
        _valid(Model(name="X", manufacturer="Y", sections=[unreachable], timing=Timing(read_back_after=1.0)), identities=identities)


def test_a_section_without_a_condition_is_never_reported_as_untested() -> None:
    p = Point(Key("always", int), read=HoldingRegister(1), data_type=DataType.UINT16)
    _valid(Model(name="X", manufacturer="Y", sections=[Section([p])], timing=Timing(read_back_after=1.0)), identities=[{}])


# ============================================================================ profiles


def _model(name: str) -> Model:
    return Model(name=name, manufacturer="Y", sections=[Section([])], timing=Timing())


def test_a_model_without_a_profile_is_caught() -> None:
    a, b = _model("A"), _model("B")
    with pytest.raises(AssertionError, match="B: no profile"):
        assert_models_valid([a, b], [modbus(a)], identities=[{}])


def test_a_profile_of_a_model_not_checked_is_caught() -> None:
    a, stray = _model("A"), _model("Stray")
    with pytest.raises(AssertionError, match="a profile of Stray, which is not among the models"):
        assert_models_valid([a], [modbus(a), modbus(stray)], identities=[{}])


def test_two_profiles_of_one_model_are_caught() -> None:
    a = _model("A")
    with pytest.raises(AssertionError, match="A: 2 profiles"):
        assert_models_valid([a], [modbus(a), modbus(a, max_registers=8)], identities=[{}])


def test_a_problem_only_the_profile_sees_is_caught() -> None:
    wide = Point(Key("wide", int), read=HoldingRegister(1), data_type=DataType.UINT64)
    model = Model(name="X", manufacturer="Y", sections=[Section([wide])], timing=Timing())
    with pytest.raises(AssertionError, match="spans 4, more than the 2"):
        assert_models_valid([model], [modbus(model, max_registers=2)], identities=[{}])


# ========================================================================== transforms

def test_a_transform_that_round_trips_passes() -> None:
    _valid(_with_transform(Transforms.SECONDS_AS_MINUTES), identities=[{}])


def test_a_lossy_transform_is_caught() -> None:
    lossy = Transform(read=lambda s: round(s / 60), write=lambda m: m * 60, name="lossy_minutes")
    with pytest.raises(AssertionError, match="'value': lossy_minutes does not round-trip"):
        _valid(_with_transform(lossy), identities=[{}])


def test_a_transform_that_fails_on_negative_values_is_caught() -> None:
    positive_only = Transform(read=abs, write=lambda v: v, name="positive_only")
    with pytest.raises(AssertionError, match=r"positive_only does not round-trip: write\(read\(-7.5\)\)"):
        _valid(_with_transform(positive_only), identities=[{}])


def test_a_transform_that_drops_fractions_is_caught() -> None:
    whole_only = Transform(read=lambda v: float(int(v)) if v >= 0 else v, write=lambda v: v, name="whole_only")
    with pytest.raises(AssertionError, match=r"whole_only does not round-trip: write\(read\(0.5\)\)"):
        _valid(_with_transform(whole_only), identities=[{}])


def test_a_transform_that_raises_is_caught() -> None:
    def explode(_: float) -> float:
        raise ZeroDivisionError("nope")
    with pytest.raises(AssertionError, match="'value': broken raised"):
        _valid(_with_transform(Transform(read=explode, write=lambda v: v, name="broken")),
                            identities=[{}])


def test_resolving_a_model_leaves_sampling_its_transforms_to_the_tests() -> None:
    lossy = _with_transform(Transform(read=lambda s: round(s / 60), write=lambda m: m * 60))
    assert problems(modbus(lossy), {}) == []
    assert "value" in resolve(modbus(lossy), {}).points
