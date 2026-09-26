"""Finding out, while polling, that the unit has changed: what each scan read is read again at
`PollRate.SCAN`, a scan runs again only where that changed, and consumers are told."""
import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from modbus_event_connect import (
    Client,
    InstanceScanStep,
    Key,
    Labels,
    Model,
    ModelError,
    Point,
    PollRate,
    Quality,
    RepeatedSection,
    Scan,
    Section,
    Status,
)
from modbus_event_connect._device import Identity, Outcome, ProtocolOptions, ReadResult, WriteResult
from modbus_event_connect._data_type import DataType
from modbus_event_connect.modbus._access import HoldingRegister, InputRegister
from modbus_event_connect.testing import FakeClock

VERSION = Key("version", int)
EXTRA = Key("extra", int)
SCAN_INTERVAL = 900.0
DUMMY = 1


class FakeDevice:
    """Answers per key; a key with no answer reads MISSING, as an absent register does."""

    def __init__(self, registers: Mapping[str, int]) -> None:
        self.answers: dict[str, ReadResult] = {k: ReadResult(Outcome.OK, (v,)) for k, v in registers.items()}
        self.reads: list[tuple[str, ...]] = []
        self.connected = False
        self.silent = False

    async def connect(self) -> Identity | None:
        self.connected = True
        return {}

    async def disconnect(self) -> None:
        self.connected = False

    def configure(self, options: ProtocolOptions | None) -> None:
        pass

    async def read(self, points: Sequence[Point[Any]]) -> Mapping[str, ReadResult]:
        self.reads.append(tuple(p.key for p in points))
        if self.silent:
            return {p.key: ReadResult(Outcome.NO_ANSWER) for p in points}
        missing = ReadResult(Outcome.MISSING, exception_code=2)
        return {p.key: self.answers.get(p.key, missing) for p in points}

    async def write(self, point: Point[Any], value: Any) -> WriteResult:
        return WriteResult(Outcome.OK)

    def diagnostics(self) -> Mapping[str, object]:
        return {}

    def answer(self, key: str, value: int) -> None:
        self.answers[key] = ReadResult(Outcome.OK, (value,))

    def remove(self, *keys: str) -> None:
        for key in keys:
            self.answers.pop(key, None)

    def silence(self) -> None:
        """Answer nothing at all, as a unit that stopped answering does."""
        self.silent = True

    def read_keys(self) -> set[str]:
        return {key for batch in self.reads for key in batch}


def zone_type(n: int) -> Key[int]:
    return Key(f"zone_{n}_type", int)


def zone_temp(n: int) -> Key[float]:
    return Key(f"zone_{n}_temp", float)


def zone_setting(n: int) -> Key[int]:
    return Key(f"zone_{n}_setting", int)


def _zone(n: int) -> list[Point[Any]]:
    base = n * 10
    return [
        Point(zone_type(n), read=InputRegister(base), poll_rate=PollRate.STATIC),
        Point(zone_temp(n), read=InputRegister(base + 1), data_type=DataType.INT16, scale=0.1,
              labels={"kind": "sensor"}),
        Point(zone_setting(n), read=HoldingRegister(base + 2), write=HoldingRegister(base + 2)),
    ]


async def scan_zone(scan: Scan, n: int) -> None:
    """Zone n is there while its type answers; a dummy zone has no sensor."""
    found = (await scan.read((zone_type(n),)))[zone_type(n)]
    if found.quality is Quality.MISSING:
        scan.set_available(Labels(zone=n), False, reason="zone not set up")
    elif found.value == DUMMY:
        scan.set_available(Labels(zone=n, kind="sensor"), False, reason="dummy zone")


async def read_version(scan: Scan) -> None:
    """The whole unit's scan: its version decides what the unit is."""
    await scan.read((VERSION,))


def _has_extra(identity: Identity) -> bool:
    version = identity.get("version")
    return isinstance(version, int) and version >= 2


def _model(scan: InstanceScanStep = scan_zone) -> Model:
    return Model(
        name="ZONES", manufacturer="TEST",
        identity_points=[Point(VERSION, read=InputRegister(1), poll_rate=PollRate.STATIC)],
        sections=[Section([Point(EXTRA, read=InputRegister(2))], when=_has_extra),
                  RepeatedSection(_zone, range(1, 4), label="zone", scan=scan)],
        scan_steps=[read_version],
        options=ProtocolOptions(), read_back_after=1.0,
        poll_intervals={PollRate.SCAN: SCAN_INTERVAL},
    )


REGISTERS: dict[str, int] = {
    "version": 1, "extra": 7,
    "zone_1_type": 0, "zone_1_temp": 215, "zone_1_setting": 3,
    "zone_2_type": DUMMY, "zone_2_temp": 0x7FFF, "zone_2_setting": 4,
}
"""Zone 1 has a sensor, zone 2 is a dummy, zone 3 is not set up."""


class PointsSeen:
    def __init__(self) -> None:
        self.calls: list[tuple[set[str], set[str]]] = []

    def __call__(self, added: frozenset[Key[Any]], removed: frozenset[Key[Any]]) -> None:
        self.calls.append(({str(k) for k in added}, {str(k) for k in removed}))


def _connected(registers: Mapping[str, int] = REGISTERS,
               model: Model | None = None) -> tuple[Client, FakeDevice, FakeClock, PointsSeen]:
    device = FakeDevice(registers)
    clock = FakeClock()
    client = Client(device, model or _model(), clock=clock)
    asyncio.run(client.connect())
    seen = PointsSeen()
    client.subscribe_points(seen)
    return client, device, clock, seen


def _poll_through(client: Client, clock: FakeClock, seconds: float, step: float = 5.0) -> None:
    """Poll as a host would, `step` seconds apart, for `seconds`."""
    elapsed = 0.0
    while elapsed < seconds:
        clock.advance(step)
        elapsed += step
        asyncio.run(client.poll())


# ====================================================================== what a scan finds

def test_each_instance_is_scanned_by_itself_at_connect() -> None:
    client, _, _, _ = _connected()
    assert client.instances("zone") == (1, 2)
    assert "zone_2_temp" not in client.points and "zone_2_setting" in client.points
    assert client.unavailable_reasons[zone_temp(2)] == "dummy zone"
    assert client.unavailable_reasons[zone_type(3)] == "zone not set up"


def test_an_instance_scan_may_not_mark_another_instances_points() -> None:
    async def overreaching(scan: Scan, n: int) -> None:
        scan.set_available(Labels(zone=n % 3 + 1), False, reason="not mine")

    device = FakeDevice(REGISTERS)
    client = Client(device, _model(overreaching), clock=FakeClock())
    with pytest.raises(ModelError, match="not its own"):
        asyncio.run(client.connect())


# ======================================================================= a static unit

def test_a_unit_that_does_not_change_is_only_read_where_its_scans_read() -> None:
    client, device, clock, seen = _connected()
    device.reads.clear()
    for _ in range(4):
        clock.advance(SCAN_INTERVAL)
        asyncio.run(client.refresh(PollRate.SCAN))
    checked = {key for batch in device.reads for key in batch}
    assert checked == {"version", "zone_1_type", "zone_2_type", "zone_3_type"}
    assert seen.calls == []


def test_the_checks_are_spread_evenly_over_the_scan_interval() -> None:
    """Four scans - the unit's and three zones' - are checked a quarter interval apart."""
    client, device, clock, _ = _connected()
    checked_at: list[tuple[float, tuple[str, ...]]] = []
    elapsed = 0.0
    while elapsed < SCAN_INTERVAL:
        wait = client.seconds_until_next_poll()
        assert wait is not None
        clock.advance(wait)
        elapsed += wait
        device.reads.clear()
        asyncio.run(client.poll())
        checked_at.extend((elapsed, batch) for batch in device.reads)
    assert checked_at == [(SCAN_INTERVAL / 4, ("version",)),
                          (SCAN_INTERVAL / 2, ("zone_1_type",)),
                          (SCAN_INTERVAL * 3 / 4, ("zone_2_type",)),
                          (SCAN_INTERVAL, ("zone_3_type",))]


def test_seconds_until_next_poll_counts_the_checks() -> None:
    device = FakeDevice({"version": 1, "zone_1_type": 0})
    clock = FakeClock()
    client = Client(device, _model(), clock=clock)
    asyncio.run(client.connect())
    wait = client.seconds_until_next_poll()
    assert wait is not None and 0 < wait <= SCAN_INTERVAL, "nothing is subscribed, only checks are due"


def test_checks_follow_the_scan_interval_set_for_them() -> None:
    client, device, clock, _ = _connected()
    client.set_poll_interval(PollRate.SCAN, 60)
    asyncio.run(client.refresh(PollRate.SCAN))                 # the next check follows the override
    device.reads.clear()
    clock.advance(60)
    asyncio.run(client.poll())
    assert "zone_1_type" in device.read_keys()


def test_a_model_without_a_scan_interval_is_never_checked() -> None:
    model = Model(name="ZONES", manufacturer="TEST",
                  sections=[RepeatedSection(_zone, range(1, 4), label="zone", scan=scan_zone)],
                  options=ProtocolOptions(), read_back_after=1.0,
                  poll_intervals={PollRate.SCAN: None})
    client, device, clock, _ = _connected(model=model)
    assert client.seconds_until_next_poll() is None, "nothing is subscribed, and nothing is checked"
    device.reads.clear()
    clock.advance(SCAN_INTERVAL * 10)
    asyncio.run(client.poll())
    assert device.reads == []


# ===================================================================== a unit that changed

def test_an_instance_set_up_later_is_found_and_told() -> None:
    client, device, _, seen = _connected()
    device.answer("zone_3_type", 0)
    device.answer("zone_3_temp", 190)
    device.answer("zone_3_setting", 2)
    asyncio.run(client.refresh(PollRate.SCAN))
    assert seen.calls == [({"zone_3_type", "zone_3_temp", "zone_3_setting"}, set())]
    temp = client.value(zone_temp(3))
    assert temp is not None and temp.value == 19.0
    assert client.instances("zone") == (1, 2, 3)


def test_an_instance_that_stops_being_a_dummy_gains_its_sensor() -> None:
    client, device, _, seen = _connected()
    device.answer("zone_2_type", 0)
    device.answer("zone_2_temp", 205)
    asyncio.run(client.refresh(PollRate.SCAN))
    assert seen.calls == [({"zone_2_temp"}, set())]


def test_an_instance_removed_is_found_at_the_next_read_of_its_points() -> None:
    client, device, clock, seen = _connected()
    told: list[Quality] = []
    client.subscribe(zone_temp(1), lambda key, old, new: told.append(new.quality))
    device.remove("zone_1_type", "zone_1_temp", "zone_1_setting")
    _poll_through(client, clock, 60)                            # MEDIUM: the temperature is due
    assert seen.calls and seen.calls[-1][1] >= {"zone_1_type", "zone_1_temp"}
    assert "zone_1_setting" not in client.points
    assert told[-1] is Quality.MISSING
    assert client.instances("zone") == (2,)


def test_without_scheduled_polling_a_read_that_finds_an_instance_gone_still_checks_it() -> None:
    client, device, _, _ = _connected()
    client.subscribe(zone_temp(1), lambda key, old, new: None)
    client.set_scheduled_polling(False)
    device.remove("zone_1_type", "zone_1_temp", "zone_1_setting")
    asyncio.run(client.refresh([zone_temp(1)]))
    assert "zone_1_setting" not in client.points and client.instances("zone") == (2,)


def test_one_register_gone_while_its_instance_stays_removes_only_that_register() -> None:
    client, device, clock, seen = _connected()
    client.subscribe(zone_temp(1), lambda key, old, new: None)
    device.remove("zone_1_temp")
    _poll_through(client, clock, 60)
    assert seen.calls == [(set(), {"zone_1_temp"})]
    assert "zone_1_setting" in client.points and client.instances("zone") == (1, 2)


def test_an_unanswered_check_changes_nothing() -> None:
    client, device, _, seen = _connected()
    keys = set(client.points)
    device.silence()
    asyncio.run(client.refresh(PollRate.SCAN))
    assert set(client.points) == keys
    assert seen.calls == []
    assert client.status(Status.CONNECTED).value is False


def test_a_change_to_what_the_whole_unit_scan_read_scans_everything_again() -> None:
    client, device, _, seen = _connected()
    assert "extra" not in client.points
    device.answer("version", 2)
    with LogRecords() as records:
        asyncio.run(client.refresh(PollRate.SCAN))
    assert seen.calls == [({"extra"}, set())]
    assert any("scanning all of it again" in r.getMessage() for r in records)


def test_a_rescan_keeps_interest_and_interval_overrides() -> None:
    client, device, clock, _ = _connected()
    told: list[object] = []
    client.subscribe(zone_temp(1), lambda key, old, new: told.append(new.value))
    client.set_poll_interval(PollRate.MEDIUM, 20)
    device.answer("version", 2)
    asyncio.run(client.refresh(PollRate.SCAN))
    device.answer("zone_1_temp", 230)
    device.reads.clear()
    clock.advance(20)
    asyncio.run(client.poll())
    assert "zone_1_temp" in device.read_keys(), "the subscription and its interval survived"
    assert told[-1] == 23.0


def test_a_points_callback_that_fails_is_logged_and_the_others_are_told() -> None:
    client, device, _, seen = _connected()

    def broken(added: frozenset[Key[Any]], removed: frozenset[Key[Any]]) -> None:
        raise RuntimeError("consumer bug")

    client.subscribe_points(broken)
    later = PointsSeen()
    client.subscribe_points(later)
    device.answer("zone_2_type", 0)
    with LogRecords() as records:
        asyncio.run(client.refresh(PollRate.SCAN))
    assert later.calls and seen.calls
    assert any("points callback failed" in r.getMessage() for r in records)


def test_an_unsubscribed_points_callback_is_not_told() -> None:
    device = FakeDevice(REGISTERS)
    client = Client(device, _model(), clock=FakeClock())
    asyncio.run(client.connect())
    seen = PointsSeen()
    unsubscribe = client.subscribe_points(seen)
    unsubscribe()
    unsubscribe()                                               # twice is harmless
    device.answer("zone_2_type", 0)
    asyncio.run(client.refresh(PollRate.SCAN))
    assert seen.calls == []


class LogRecords(logging.Handler):
    """Keeps the client's log records for the duration of a `with` block."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._logger = logging.getLogger("modbus_event_connect._client")

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __enter__(self) -> list[logging.LogRecord]:
        self._logger.addHandler(self)
        self._level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        return self.records

    def __exit__(self, *_: object) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._level)
