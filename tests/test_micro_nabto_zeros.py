"""Answers of only zeros, where registers held values, and what the warnings alone tell of each
outcome: against a simulated device on localhost UDP, on a fake clock."""
import logging
from typing import Any

import pytest

from modbus_event_connect._client import Client
from modbus_event_connect._data_type import DataType
from modbus_event_connect._device import Identity, Outcome, ReadResult
from modbus_event_connect._errors import AuthenticationError
from modbus_event_connect._key import Key
from modbus_event_connect._model import Model, Section
from modbus_event_connect._point import Point
from modbus_event_connect._value import Quality
from modbus_event_connect.micro_nabto._access import DatapointRegister
from modbus_event_connect.micro_nabto._connection import MicroNabtoConnection
from modbus_event_connect.micro_nabto._device import TAKE_ZEROS, MicroNabtoDevice, MicroNabtoOptions, RereadZeros
from modbus_event_connect import Exponential, Fixed, Retry, Steps
from modbus_event_connect.testing._clock import FakeClock
from modbus_event_connect.testing._micro_nabto import SimulatedMicroNabtoDevice

EMAIL = "user@example.invalid"
EVERY_TEN_SECONDS = Retry(Fixed(10.0))
"""Trying a failed handshake again every 10 s: what a test takes for granted unless it tests another."""
LOGGER = "modbus_event_connect.micro_nabto"


def _dp(address: int) -> Point[Any]:
    return Point(Key(f"dp_{address}", int), read=DatapointRegister(address), data_type=DataType.UINT16)


POINTS = [_dp(1), _dp(2), _dp(3)]


def _simulated(clock: FakeClock) -> SimulatedMicroNabtoDevice:
    return SimulatedMicroNabtoDevice(emails=frozenset({EMAIL}), clock=clock,
                                     datapoint_registers={(0, a): a for a in range(1, 11)})


def _device(simulated: SimulatedMicroNabtoDevice, clock: FakeClock, *,
            reread_zeros_for: float | None = None, zeros: RereadZeros | None = None) -> MicroNabtoDevice:
    host, port = simulated.address
    device = MicroNabtoDevice(MicroNabtoConnection(EMAIL, host=host, port=port, timeout=0.05, retries=1,
                                                   answer_wait=0.5, clock=clock, reconnect=EVERY_TEN_SECONDS),
                              owns_connection=True, reread_zeros_for=reread_zeros_for)
    device.configure(MicroNabtoOptions(later_answer_wait=0.2, zeros=zeros))
    return device


async def _connected(simulated: SimulatedMicroNabtoDevice, clock: FakeClock, *,
                     reread_zeros_for: float | None = None, zeros: RereadZeros | None = None) -> MicroNabtoDevice:
    device = _device(simulated, clock, reread_zeros_for=reread_zeros_for, zeros=zeros)
    assert await device.connect() is not None
    await device.read(POINTS)
    return device


def _warnings(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def _email_in(caplog: pytest.LogCaptureFixture) -> bool:
    return EMAIL in caplog.text or EMAIL.encode().hex(" ") in caplog.text


def _ended(**counts: int) -> dict[str, int]:
    """Diagnostics' count of how answers of zeros ended: `counts`, and none otherwise."""
    return {ending: counts.get(ending, 0)
            for ending in ("values_at_once", "values_after_waiting", "values_in_halves", "zeros_taken")}


async def _until_taken(device: MicroNabtoDevice, clock: FakeClock) -> tuple[dict[str, ReadResult], list[float]]:
    """Read as a client would: again after each answer's `retry_after`, until one is taken."""
    retries: list[float] = []
    while True:
        answers = dict(await device.read(POINTS))
        retry = answers["dp_1"].retry_after
        if retry is None:
            return answers, retries
        assert {a.outcome for a in answers.values()} == {Outcome.BUSY}
        retries.append(retry)
        clock.advance(retry)


async def test_a_single_answer_of_zeros_is_read_again_at_once_and_the_warning_says_all_it_knows(
        caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock)
        simulated.zero_answers = 1
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            answers = await device.read(POINTS)
        assert [a.registers for a in answers.values()] == [(1,), (2,), (3,)]
        said = _warnings(caplog)
        assert "DatapointRegister 1 to 3, 3 registers (3 points) answered only zeros, where they held values" in said
        assert "addresses 1, 2, 3; this read: 1 request(s) of 3 registers" in said
        assert "s ago DatapointRegister read of 3: values" in said and "modbus_event_connect" in said
        assert "request 3 of a session 0.0 s old (started on connect)" in said
        assert "read again at once: values, which are taken" in said and not _email_in(caplog)
        assert device.diagnostics()["zeros_ended"] == _ended(values_at_once=1)


async def test_zeros_that_stay_are_not_taken_and_the_points_are_read_again_half_a_second_later(
        caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock)
        simulated.zeros_until = clock.monotonic() + 100
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            answers = await device.read(POINTS)
        assert {(a.outcome, a.retry_after) for a in answers.values()} == {(Outcome.BUSY, 0.5)}
        assert "the last values are kept, and the registers read again for up to 7.5 s" in _warnings(caplog)
        assert device.diagnostics()["zeros_open"] == 1


async def test_a_gateway_not_ready_yet_is_read_every_second_and_the_warning_says_when_it_was(
        caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock)
        simulated.zeros_until = clock.monotonic() + 4.0
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            answers, retries = await _until_taken(device, clock)
        assert [a.registers for a in answers.values()] == [(1,), (2,), (3,)]
        assert retries == [0.5, 1.0, 1.0, 1.0, 1.0]
        assert ("answered values 4.5 s after the zeros of DatapointRegister 1 to 3, 3 registers (3 points); "
                "before them: 0.0 s only zeros, 0.5 s only zeros, 1.5 s only zeros, 2.5 s only zeros, "
                "3.5 s only zeros") in _warnings(caplog)
        diagnostics = device.diagnostics()
        assert (diagnostics["zeros_ended"], diagnostics["longest_until_values"]) == (_ended(values_after_waiting=1), 4.5)


async def test_a_read_in_between_neither_restarts_nor_extends_the_wait() -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock)
        simulated.zeros_until = clock.monotonic() + 100
        await device.read(POINTS)
        clock.advance(0.2)
        assert (await device.read(POINTS))["dp_1"].retry_after == pytest.approx(0.3)
        clock.advance(0.3)
        _, retries = await _until_taken(device, clock)
        assert sum(retries) == pytest.approx(7.0)


async def test_zeros_that_stay_are_taken_at_reread_zeros_for_after_a_reading_in_halves(
        caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock)
        for address in (1, 2, 3):
            simulated.datapoint_registers[(0, address)] = 0
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            answers, retries = await _until_taken(device, clock)
        assert {a.registers for a in answers.values()} == {(0,)}
        assert retries == [0.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        assert "answered only zeros for 7.5 s" in _warnings(caplog)
        assert "and read in two halves only zeros: the zeros are taken" in _warnings(caplog)
        assert device.diagnostics()["zeros_ended"] == _ended(zeros_taken=1)


async def test_a_read_too_large_for_the_device_is_shown_to_be_so_by_its_halves(
        caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock, reread_zeros_for=1.0)
        simulated.zeros_above = 2
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            answers, retries = await _until_taken(device, clock)
        assert [a.registers for a in answers.values()] == [(1,), (2,), (3,)]
        assert retries == [0.5, 0.5]
        assert ("read in two halves of 1 and 2 registers, it answered values, which are taken: "
                "3 registers in one request are too many for this device") in _warnings(caplog)
        diagnostics = device.diagnostics()
        assert (diagnostics["zeros_ended"], diagnostics["smallest_read_of_zeros"]) == (_ended(values_in_halves=1), 3)


async def test_with_no_time_to_wait_zeros_are_read_again_at_once_then_in_halves_and_taken() -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock, reread_zeros_for=0)
        simulated.zeros_until = clock.monotonic() + 100
        answers = await device.read(POINTS)
        assert {(a.outcome, a.registers) for a in answers.values()} == {(Outcome.OK, (0,))}


async def test_a_model_that_takes_zeros_takes_them_at_once_without_reading_again(
        caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock, zeros=TAKE_ZEROS)
        simulated.zeros_until = clock.monotonic() + 100
        sent = device.diagnostics()["exchanges"]
        assert isinstance(sent, int)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            answers = await device.read(POINTS)
        assert {(a.outcome, a.registers) for a in answers.values()} == {(Outcome.OK, (0,))}
        assert device.diagnostics()["exchanges"] == sent + 1
        assert "answered only zeros for 0.0 s (0.0 s only zeros): the zeros are taken" in _warnings(caplog)
        assert device.diagnostics()["zeros_ended"] == _ended(zeros_taken=1)


async def test_a_model_states_when_zeros_are_read_again_and_how_long_until_they_are_taken() -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        patient = RereadZeros(within=20.0, wait=Steps(1.0, then=Exponential(2.0)), halves=False)
        device = await _connected(simulated, clock, zeros=patient)
        for address in (1, 2, 3):
            simulated.datapoint_registers[(0, address)] = 0
        answers, retries = await _until_taken(device, clock)
        assert {a.registers for a in answers.values()} == {(0,)}
        assert retries == [1.0, 2.0, 4.0, 8.0, 5.0]


async def test_a_wait_of_nothing_reads_zeros_again_at_the_next_read() -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock, zeros=RereadZeros(within=2.0, wait=Fixed(0)))
        simulated.zeros_until = clock.monotonic() + 100
        clock.advance(0.1)
        assert (await device.read(POINTS))["dp_1"].retry_after == 0.0


def test_a_device_given_its_own_zeros_rule_keeps_it_over_the_models() -> None:
    connection = MicroNabtoConnection(EMAIL, host="device.invalid", reconnect=EVERY_TEN_SECONDS)
    device = MicroNabtoDevice(connection, zeros=TAKE_ZEROS)
    device.configure(MicroNabtoOptions(zeros=RereadZeros(within=3.0)))
    assert device.zeros == TAKE_ZEROS
    assert MicroNabtoDevice(connection, zeros=TAKE_ZEROS, reread_zeros_for=2.0).zeros.within == 2.0
    with pytest.raises(ValueError):
        RereadZeros(within=-1.0)


def test_the_time_until_zeros_are_taken_is_the_devices_then_the_models_then_seven_and_a_half_seconds() -> None:
    connection = MicroNabtoConnection(EMAIL, host="device.invalid", reconnect=EVERY_TEN_SECONDS)
    device = MicroNabtoDevice(connection)
    assert device.reread_zeros_for == 7.5
    device.configure(MicroNabtoOptions(reread_zeros_for=3.0))
    assert device.reread_zeros_for == 3.0
    given = MicroNabtoDevice(connection, reread_zeros_for=1.0)
    given.configure(MicroNabtoOptions(reread_zeros_for=3.0))
    assert given.reread_zeros_for == 1.0 and given.diagnostics()["reread_zeros_for"] == 1.0
    assert MicroNabtoDevice.udp(EMAIL, host="device.invalid", reconnect=EVERY_TEN_SECONDS, reread_zeros_for=2.0).reread_zeros_for == 2.0
    with pytest.raises(ValueError):
        MicroNabtoDevice(connection, reread_zeros_for=-1.0)
    with pytest.raises(ValueError):
        MicroNabtoOptions(reread_zeros_for=-1.0)


async def test_a_new_session_says_why_it_started_and_how_long_after_the_last_answer(
        caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock)
        clock.advance(13.0)
        simulated.zero_answers = 1
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await device.read(POINTS)
        assert "of a session 0.0 s old (started after 13.0 s unused, 13.0 s after the last answer)" in _warnings(caplog)


async def test_a_silent_device_is_warned_of_once_and_again_when_it_answers(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = await _connected(simulated, clock)
        simulated.silent = True
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await device.read(POINTS)
            clock.advance(11.0)
            await device.read(POINTS)
            assert _warnings(caplog).count("no answer to request") == 1
            simulated.silent = False
            clock.advance(11.0)
            answers = await device.read(POINTS)
        assert [a.registers for a in answers.values()] == [(1,), (2,), (3,)]
        said = _warnings(caplog)
        assert "no answer to request 3 of a session 0.0 s old (started on connect), sent 2 time(s)" in said
        assert "answering again, 22.0 s after the last answer before it went silent" in said
        assert "of a session" in said and not _email_in(caplog)
        assert device.diagnostics()["silences"] == 1


async def test_a_busy_device_is_tried_again_later_and_never_taken_for_a_wrong_email(
        caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        simulated.busy_handshakes = 2
        device = _device(simulated, clock)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert await device.connect() is None
            clock.advance(11.0)
            assert await device.connect() is None
            clock.advance(11.0)
            assert await device.connect() is not None
        said = _warnings(caplog)
        assert said.count("refused the session with status 0x8009 (busy, no session free)") == 1
        assert device.diagnostics()["refusals"] == {"0x8009": 2}


async def test_an_email_the_device_does_not_know_is_still_refused() -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        device = MicroNabtoDevice(MicroNabtoConnection("other@example.invalid", host=simulated.address[0],
                                                       port=simulated.address[1], timeout=0.05, retries=1,
                                                       clock=clock, reconnect=EVERY_TEN_SECONDS), owns_connection=True)
        with pytest.raises(AuthenticationError):
            await device.connect()


MODEL = Model(name="Unit", manufacturer="Example", sections=[Section(POINTS)], options=MicroNabtoOptions(),
              read_back_after=1.0)


def _select(identity: Identity) -> Model | None:
    return MODEL


async def test_a_client_shows_the_last_values_while_zeros_are_read_again_and_reads_them_on_time() -> None:
    clock = FakeClock()
    async with _simulated(clock) as simulated:
        client = Client(_device(simulated, clock), _select, clock=clock)
        await client.connect()
        for point in POINTS:
            client.subscribe(point.key, lambda key, old, new: None)
        simulated.zeros_until = clock.monotonic() + 1.0
        await client.refresh()
        value = client.value(Key("dp_2", int))
        assert value is not None and (value.value, value.quality) == (2, Quality.STALE)
        assert client.seconds_until_next_poll() == pytest.approx(0.5)
        await client.refresh(["dp_2"])                                   # one in between: still the last value
        value = client.value(Key("dp_2", int))
        assert value is not None and (value.value, value.quality) == (2, Quality.STALE)
        clock.advance(1.0)
        await client.poll()
        value = client.value(Key("dp_2", int))
        assert value is not None and (value.value, value.quality) == (2, Quality.GOOD)
