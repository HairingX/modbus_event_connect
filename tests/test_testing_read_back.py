"""`measure_read_back`, against a device that shows each write only after a lag."""
import asyncio
from collections.abc import Mapping, Sequence

import pytest

from src.modbus_event_connect._client import Client
from src.modbus_event_connect._errors import InvalidValueError
from src.modbus_event_connect._device import (
    EncodedWrite,
    Identity,
    Outcome,
    ProtocolOptions,
    ReadResult,
    WriteResult,
)
from src.modbus_event_connect._errors import ReadOnlyError
from src.modbus_event_connect.modbus._access import HoldingRegister, InputRegister
from src.modbus_event_connect._model import Model, Section
from src.modbus_event_connect._point import Limits, Point
from src.modbus_event_connect.testing._clock import FakeClock
from src.modbus_event_connect.testing._read_back import (
    ReadBackMeasurement,
    ReadBackTrial,
    measure_read_back,
)

SETTING = Point("setting", read=HoldingRegister(1), write=HoldingRegister(1))
SENSOR = Point("sensor", read=InputRegister(2))
MODEL = Model(name="Lagging", manufacturer="Test", sections=[Section([SETTING, SENSOR])],
              options=ProtocolOptions(), read_back_after=1.0)


class LaggingDevice:
    """Holds each written register, but shows it to reads only `lag` seconds after the write."""

    def __init__(self, clock: FakeClock, lag: float, *, applies: bool = True) -> None:
        self.clock = clock
        self.lag = lag
        self.applies = applies
        self.writes: list[int] = []
        self._pending: list[tuple[float, int]] = []
        self._shown = 0

    async def connect(self) -> Identity | None:
        return {}

    async def disconnect(self) -> None:
        pass

    def configure(self, options: ProtocolOptions) -> None:
        pass

    async def read(self, points: Sequence[Point]) -> Mapping[str, ReadResult]:
        now = self.clock.monotonic()
        for at, value in [p for p in self._pending if p[0] <= now]:
            self._shown = value
            self._pending.remove((at, value))
        return {p.key: ReadResult(Outcome.OK, (self._shown,)) for p in points}

    async def write(self, point: Point, value: EncodedWrite) -> WriteResult:
        self.writes.append(value.registers[0])
        if self.applies:
            self._pending.append((self.clock.monotonic() + self.lag, value.registers[0]))
        return WriteResult(Outcome.OK)

    def diagnostics(self) -> Mapping[str, object]:
        return {}


def _measure(lag: float, *, applies: bool = True, read_only: bool = False,
             delays: Sequence[float] = (1, 2, 3, 4)) -> tuple[ReadBackMeasurement, LaggingDevice]:
    clock = FakeClock()
    device = LaggingDevice(clock, lag, applies=applies)
    client = Client(device, MODEL, clock=clock, read_only=read_only)

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)

    async def run() -> ReadBackMeasurement:
        await client.connect()
        return await measure_read_back(client, "setting", (10, 20), delays=delays, repeats=3, sleep=sleep)
    return asyncio.run(run()), device


def test_the_shortest_delay_that_shows_every_write_is_recommended() -> None:
    measured, _ = _measure(lag=2.5)
    assert [(t.delay, t.seen, t.written) for t in measured.trials] == [(1, 0, 3), (2, 0, 3), (3, 3, 3), (4, 3, 3)]
    assert measured.recommended == 3


def test_a_late_earlier_write_of_the_same_value_does_not_pass_for_a_quick_one() -> None:
    """Without settling on the start value first, 10 written at 1 s would show at 3 s as the 10 written at 2 s."""
    measured, _ = _measure(lag=2.5, delays=(1, 3))
    assert measured.trials[0] == ReadBackTrial(delay=1, written=3, seen=0)


def test_a_device_that_never_shows_the_write_gets_no_recommendation() -> None:
    measured, _ = _measure(lag=0, applies=False)
    assert measured.recommended is None
    assert "try longer ones" in str(measured)


def test_a_recommendation_needs_every_longer_delay_to_have_shown_every_write() -> None:
    measured = ReadBackMeasurement("k", (ReadBackTrial(1, 3, 3), ReadBackTrial(2, 3, 2), ReadBackTrial(3, 3, 3)))
    assert measured.recommended == 3


def test_the_value_the_point_had_is_written_back() -> None:
    _, device = _measure(lag=0.5)
    assert device.writes[-1] == 0


def test_a_read_only_client_is_never_written_through() -> None:
    with pytest.raises(ReadOnlyError):
        _measure(lag=0.5, read_only=True)


@pytest.mark.parametrize("key,values,delays", [
    ("sensor", (10, 20), (1,)),               # cannot be written
    ("setting", (10, 10), (1,)),              # a write could not be told from none
    ("setting", (10, 20), ()),
    ("setting", (10, 20), (-1,)),
], ids=["read-only-point", "equal-values", "no-delays", "negative-delay"])
def test_a_measurement_that_cannot_tell_anything_is_refused(key: str, values: tuple[int, int],
                                                             delays: tuple[float, ...]) -> None:
    clock = FakeClock()
    device = LaggingDevice(clock, 0)
    client = Client(device, MODEL, clock=clock)

    async def run() -> None:
        await client.connect()
        await measure_read_back(client, key, values, delays=delays)
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert device.writes == []


def test_a_value_the_point_refuses_is_found_before_anything_is_written() -> None:
    """The point holds 0, below its limits, so it could not be written back at the end."""
    limited = Point("limited", read=HoldingRegister(1), write=HoldingRegister(1), limits=Limits(min=5, max=30))
    model = Model(name="L", manufacturer="Test", sections=[Section([limited])], options=ProtocolOptions(),
                  read_back_after=1.0)
    clock = FakeClock()
    device = LaggingDevice(clock, 0)
    client = Client(device, model, clock=clock)

    async def run() -> None:
        await client.connect()
        await measure_read_back(client, "limited", (10, 20), delays=(1,))
    with pytest.raises(InvalidValueError):
        asyncio.run(run())
    assert device.writes == []
