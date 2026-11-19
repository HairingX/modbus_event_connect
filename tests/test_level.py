"""Levels and candidates: which of a model's points a unit has, as resolving and the client see it."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from enum import IntEnum
from typing import Any

import pytest

from event_connect import Identity
from event_connect.testing import FakeClock
from modbus_event_connect import (
    Candidate,
    Certainty,
    Client,
    DataType,
    Key,
    Labels,
    Level,
    ModelError,
    Point,
    PollRate,
    Refresh,
    Scan,
    Section,
)
from modbus_event_connect.modbus import HoldingRegister, InputRegister, ModbusModel
from modbus_event_connect.testing import assert_models_valid, resolve
from modbus_event_connect._model import problems
from models import OFFLINE_AFTER_INTERVALS, modbus_model
from test_client import FakeDevice, Recorder


class CoilKind(IntEnum):
    NONE = 0
    REHEAT = 1
    PREHEAT = 2


SEEN = Key("seen", int)
STATED = Key("stated", int)
GUESS = Key("guess", int)
REHEAT = Key("reheat", bool)
PREHEAT = Key("preheat", bool)
COIL = Key("coil", CoilKind)
MIN_AIR = Key("min_air", int)

TODAY: list[Point[Any]] = [
    Point(SEEN, read=HoldingRegister(1), write=HoldingRegister(1), poll_rate=PollRate.MEDIUM,
          certainty=Certainty.VERIFIED, on_write=Refresh(["stated", "guess"], after=2.0)),
    Point(STATED, read=HoldingRegister(2), write=HoldingRegister(2), poll_rate=PollRate.MEDIUM),
    Point(GUESS, read=InputRegister(3), poll_rate=PollRate.MEDIUM, certainty=Certainty.INFERRED),
    Point(REHEAT, read=HoldingRegister(21), data_type=DataType.BOOL, poll_rate=PollRate.SLOW),
    Point(PREHEAT, read=HoldingRegister(20), data_type=DataType.BOOL, poll_rate=PollRate.SLOW),
]
CANDIDATES: list[Point[Any]] = [
    Point(COIL, read=HoldingRegister(21), poll_rate=PollRate.SLOW, candidate=Candidate(replaces=REHEAT)),
    Point(PREHEAT, read=HoldingRegister(16), data_type=DataType.BOOL, poll_rate=PollRate.SLOW,
          candidate=Candidate(replaces=PREHEAT)),
    Point(MIN_AIR, read=HoldingRegister(22), poll_rate=PollRate.SLOW, candidate=Candidate()),
]
WITH_CANDIDATES = modbus_model([Section(TODAY), Section(CANDIDATES)], name="WITH")
WITHOUT_CANDIDATES = modbus_model([Section(TODAY)], name="WITHOUT")

REGISTERS: dict[str, tuple[int, ...]] = {
    "seen": (1,), "stated": (2,), "guess": (3,), "reheat": (1,), "preheat": (0,), "coil": (1,), "min_air": (30,),
}


def _point(key: Key[Any], address: int, **fields: Any) -> Point[Any]:
    return Point(key, read=HoldingRegister(address), poll_rate=PollRate.SLOW, **fields)


def _client(level: Level, model: ModbusModel = WITH_CANDIDATES,
            registers: Mapping[str, tuple[int, ...]] = REGISTERS) -> tuple[Client, FakeDevice, FakeClock]:
    device = FakeDevice(registers, models=[model])
    clock = FakeClock(autojump=True)
    return Client(device, model, offline_after_intervals=OFFLINE_AFTER_INTERVALS, level=level, clock=clock), device, clock


def _connected(level: Level, model: ModbusModel = WITH_CANDIDATES,
               registers: Mapping[str, tuple[int, ...]] = REGISTERS) -> tuple[Client, FakeDevice, FakeClock]:
    client, device, clock = _client(level, model, registers)
    asyncio.run(client.connect())
    return client, device, clock


def _read(device: FakeDevice) -> set[str]:
    return {key for batch in device.reads for key in batch}


# ================================================================================ resolving


def test_on_the_documented_level_a_model_has_exactly_the_points_it_had_without_candidates() -> None:
    with_them = resolve(WITH_CANDIDATES, {}, Level.DOCUMENTED)
    without_them = resolve(WITHOUT_CANDIDATES, {}, Level.DOCUMENTED)
    assert dict(with_them.points) == dict(without_them.points)
    assert with_them.used == without_them.used
    assert list(with_them.used) == ["seen", "stated", "reheat", "preheat"]


def test_at_the_test_level_a_candidate_takes_the_place_of_the_point_it_replaces() -> None:
    resolved = resolve(WITH_CANDIDATES, {}, Level.TEST)
    assert list(resolved.used) == ["seen", "stated", "guess", "coil", "preheat", "min_air"]
    assert resolved.points["preheat"].read == HoldingRegister(16)
    assert dict(resolved.left_out) == {"reheat": "replaced by the candidate 'coil' at the test level"}


def test_a_level_leaves_out_the_points_of_a_certainty_it_does_not_admit_with_the_reason() -> None:
    resolved = resolve(WITH_CANDIDATES, {}, Level.VERIFIED)
    assert list(resolved.points) == ["seen", "stated", "guess", "reheat", "preheat"]
    assert dict(resolved.left_out) == {
        "stated": "reported points are left out at the verified level",
        "guess": "inferred points are left out at the verified level",
        "reheat": "reported points are left out at the verified level",
        "preheat": "reported points are left out at the verified level",
    }
    assert list(resolved.used) == ["seen"]
    assert resolved.level is Level.VERIFIED


def test_the_keys_a_unit_may_have_at_a_level_are_those_it_uses_and_at_the_test_level_every_key() -> None:
    assert WITH_CANDIDATES.keys_at(Level.VERIFIED) == {"seen"}
    assert WITH_CANDIDATES.keys_at(Level.DOCUMENTED) == {"seen", "stated", "reheat", "preheat"}
    assert WITH_CANDIDATES.keys_at(Level.TEST) == WITH_CANDIDATES.keys
    assert WITH_CANDIDATES.keys == {"seen", "stated", "guess", "reheat", "preheat", "coil", "min_air"}


def test_a_candidate_with_the_key_of_a_point_it_does_not_replace_is_a_duplicate_only_at_the_test_level() -> None:
    model = modbus_model([Section([_point(SEEN, 1), _point(SEEN, 2, candidate=Candidate())])])
    assert problems(model, {}, Level.DOCUMENTED) == []
    assert problems(model, {}, Level.TEST) == ["duplicate key 'seen' (2 times)"]
    with pytest.raises(AssertionError, match="/ test: duplicate key 'seen'"):
        assert_models_valid([model], identities=[{}])


# ============================================================================== the model


@pytest.mark.parametrize("sections, identity_points, problem", [
    ([Section([_point(SEEN, 1), _point(COIL, 2, candidate=Candidate(replaces=Key("nowhere", int)))])], [],
     "'coil': the candidate replaces 'nowhere', which no point of the model's sections that is not a candidate has"),
    ([Section([_point(SEEN, 1, candidate=Candidate()), _point(COIL, 2, candidate=Candidate(replaces=SEEN))])], [],
     "'coil': the candidate replaces 'seen', which no point of the model's sections that is not a candidate has"),
    ([Section([_point(COIL, 2, candidate=Candidate(replaces=SEEN))])], [_point(SEEN, 1)],
     "'coil': the candidate replaces 'seen', which no point of the model's sections that is not a candidate has"),
    ([Section([_point(STATED, 2)])], [_point(SEEN, 1, candidate=Candidate())],
     "identity point 'seen' is a candidate, but identity points are read at every level"),
    ([Section([_point(SEEN, 1), _point(Key("seen", float), 2, candidate=Candidate(replaces=SEEN))])], [],
     "'seen': the candidate replaces its own key, but holds a float where the point it replaces holds another type"),
    ([Section([_point(SEEN, 1), _point(STATED, 2, candidate=Candidate(replaces=SEEN)),
               _point(GUESS, 3, candidate=Candidate(replaces=SEEN))])], [],
     "'seen' is replaced by more than one candidate: 'stated', 'guess'"),
    ([Section([_point(SEEN, 1), _point(SEEN, 2, candidate=Candidate(replaces=SEEN)),
               _point(STATED, 3, candidate=Candidate(replaces=SEEN))])], [],
     "'seen' is replaced by more than one candidate: 'seen', 'stated'"),
    ([Section([_point(SEEN, 1, write=HoldingRegister(1), on_write=Refresh(["stated"])), _point(STATED, 2),
               _point(GUESS, 3, candidate=Candidate(replaces=STATED))])], [],
     "'seen': on_write names 'stated', which a candidate of another key replaces; name it by labels"),
    ([Section([_point(SEEN, 1, on_change=Refresh(["stated"])), _point(STATED, 2),
               _point(GUESS, 3, candidate=Candidate(replaces=STATED))])], [],
     "'seen': on_change names 'stated', which a candidate of another key replaces; name it by labels"),
], ids=["unknown key", "another candidate", "an identity point", "identity candidate", "another type",
        "two candidates", "two candidates, one of its own key", "on_write", "on_change"])
def test_a_model_refuses_a_candidate_that_replaces_nothing_it_may_replace(
        sections: Sequence[Section], identity_points: Sequence[Point[Any]], problem: str) -> None:
    with pytest.raises(ModelError) as raised:
        modbus_model(sections, identity_points=identity_points)
    assert problem in str(raised.value)


def test_a_refresh_may_name_a_point_a_candidate_of_its_own_key_replaces_or_one_by_labels() -> None:
    model = modbus_model([Section([
        _point(SEEN, 1, write=HoldingRegister(1), on_write=Refresh(["stated"]), on_change=Refresh(Labels(group=1))),
        _point(STATED, 2, labels={"group": 1}), _point(GUESS, 3, labels={"group": 1}),
        _point(STATED, 4, candidate=Candidate(replaces=STATED)),
        _point(MIN_AIR, 5, candidate=Candidate(replaces=GUESS), labels={"group": 1})])])
    assert all(problems(model, {}, level) == [] for level in Level)


def test_a_candidate_may_replace_a_point_of_its_own_key_and_type_declared_after_it() -> None:
    model = modbus_model([Section([_point(SEEN, 2, candidate=Candidate(replaces=SEEN))]), Section([_point(SEEN, 1)])])
    assert resolve(model, {}, Level.TEST).points["seen"].read == HoldingRegister(2)
    assert resolve(model, {}, Level.DOCUMENTED).points["seen"].read == HoldingRegister(1)


# ================================================================================ the client


def test_a_client_at_the_documented_level_neither_has_nor_reads_a_candidate() -> None:
    client, device, _ = _connected(Level.DOCUMENTED)
    assert set(client.points) == {"seen", "stated", "reheat", "preheat"}
    assert client.points[PREHEAT].read == HoldingRegister(20)
    assert not _read(device) & {"coil", "min_air", "guess"}
    with pytest.raises(KeyError, match="no model of this client has a point 'min_air' at the documented level"):
        client.subscribe(MIN_AIR, Recorder())


def test_a_client_at_the_test_level_has_each_candidate_and_not_the_point_it_replaces() -> None:
    client, device, _ = _client(Level.TEST)
    client.subscribe(REHEAT, Recorder())
    asyncio.run(client.connect())
    assert set(client.points) == {"seen", "stated", "guess", "coil", "preheat", "min_air"}
    assert client.points[PREHEAT].read == HoldingRegister(16)
    coil = client.value(COIL)
    assert coil is not None and coil.value is CoilKind.REHEAT
    assert not client.has(REHEAT) and "reheat" not in _read(device)
    with pytest.raises(KeyError, match="the WITH model has no point 'reheat' at the test level"):
        client.subscribe(REHEAT, Recorder())


def test_a_client_at_the_verified_level_has_only_verified_points_and_refuses_the_others() -> None:
    client, _, _ = _connected(Level.VERIFIED)
    assert set(client.points) == {"seen"} and set(client.values) == {"seen"}
    assert client.value(STATED) is None and not client.has(STATED)
    assert client.unavailable_reasons == {}
    with pytest.raises(KeyError, match="'stated'"):
        client.subscribe(STATED, Recorder())
    with pytest.raises(KeyError, match="'stated'"):
        asyncio.run(client.write(STATED, 4))


@pytest.mark.parametrize("level, refreshed", [(Level.DOCUMENTED, {"seen", "stated"}), (Level.VERIFIED, {"seen"})])
def test_a_point_the_level_leaves_out_is_neither_polled_nor_refreshed(level: Level, refreshed: set[str]) -> None:
    client, device, clock = _connected(level)
    for key in ("seen", "stated", "guess"):
        client.set_polling(key)
    device.reads.clear()
    asyncio.run(client.write(SEEN, 4))
    clock.advance(3)
    asyncio.run(client.poll())
    assert _read(device) == refreshed
    clock.advance(60)
    asyncio.run(client.poll())
    assert _read(device) == refreshed


def test_a_key_one_variant_has_verified_and_this_unit_has_only_reported_is_told_of_and_refused(
        caplog: pytest.LogCaptureFixture) -> None:
    variants = modbus_model([
        Section([_point(SEEN, 1, certainty=Certainty.VERIFIED)], when=lambda identity: identity.get("hardware") == 1),
        Section([_point(SEEN, 1)], when=lambda identity: identity.get("hardware") == 2),
    ], name="VARIANTS")
    device = FakeDevice(REGISTERS, models=[variants], identity={"hardware": 2})
    client = Client(device, variants, offline_after_intervals=OFFLINE_AFTER_INTERVALS, level=Level.VERIFIED,
                    clock=FakeClock(autojump=True))
    client.subscribe(SEEN, Recorder())
    with caplog.at_level(logging.WARNING):
        asyncio.run(client.connect())
    assert "'seen' is subscribed to, but the VARIANTS model does not have it at the verified level" in caplog.text
    with pytest.raises(KeyError, match="the VARIANTS model has no point 'seen' at the verified level"):
        client.subscribe(SEEN, Recorder())


async def _guess_absent_without_stated(scan: Scan) -> None:
    """A scan step reading a point a level may leave out, to decide on another."""
    found = await scan.read([STATED])
    if found[STATED].value == 0:
        scan.set_available([GUESS], False, reason="no guess without a statement")


SCANNED = modbus_model([Section(TODAY)], name="SCANNED", scan_steps=[_guess_absent_without_stated],
                       scan_check=PollRate.RARE)


@pytest.mark.parametrize("level, unavailable", [
    (Level.VERIFIED, {}),
    (Level.TEST, {"guess": "no guess without a statement"}),
])
def test_a_scan_reads_a_point_the_level_leaves_out_and_what_it_finds_missing_of_those_is_not_told(
        level: Level, unavailable: dict[str, str]) -> None:
    client, device, _ = _connected(level, SCANNED, {**REGISTERS, "stated": (0,)})
    assert "stated" in _read(device)
    assert client.unavailable_reasons == unavailable
    assert not client.has(GUESS)


SCANNED_AND_REPLACED = modbus_model(
    [Section([_point(STATED, 2), _point(GUESS, 3), _point(MIN_AIR, 22, candidate=Candidate(replaces=STATED))])],
    name="SCANNED_AND_REPLACED", scan_steps=[_guess_absent_without_stated], scan_check=PollRate.RARE)


def test_a_scan_reads_a_point_a_candidate_of_another_key_replaces_at_the_test_level() -> None:
    client, device, _ = _connected(Level.TEST, SCANNED_AND_REPLACED, {**REGISTERS, "stated": (0,)})
    assert "stated" in _read(device)
    assert client.unavailable_reasons == {"guess": "no guess without a statement"}
    assert not client.has(STATED) and client.has(MIN_AIR)


def test_a_value_the_level_leaves_out_in_the_model_chosen_afresh_is_dropped() -> None:
    seen_here = modbus_model([Section([_point(SEEN, 1, certainty=Certainty.VERIFIED)])], name="HERE")
    stated_there = modbus_model([Section([_point(SEEN, 1)])], name="THERE")
    device = FakeDevice(REGISTERS, models=[seen_here, stated_there])

    def choose(identity: Identity) -> ModbusModel:
        return stated_there if identity.get("device_model") == 2 else seen_here

    client = Client(device, choose, offline_after_intervals=OFFLINE_AFTER_INTERVALS, level=Level.VERIFIED,
                    clock=FakeClock(autojump=True))
    asyncio.run(client.connect())
    assert client.value(SEEN) is not None
    device.identity = {"device_model": 2}
    asyncio.run(client.connect())
    assert client.model is stated_there and client.value(SEEN) is None


def test_a_client_names_its_level_and_diagnoses_each_points_certainty_and_each_candidate_it_uses() -> None:
    client, _, _ = _connected(Level.TEST)
    assert client.level is Level.TEST
    diagnostics = client.diagnostics()
    assert diagnostics["level"] == "test"
    assert diagnostics["certainty"] == {"seen": "verified", "stated": "reported", "guess": "inferred",
                                        "coil": "reported", "preheat": "reported", "min_air": "reported"}
    assert diagnostics["candidates"] == {"coil": "reheat", "preheat": "preheat", "min_air": None}
