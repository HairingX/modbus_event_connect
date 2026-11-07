"""The micro_nabto session against a simulated device on a localhost UDP port."""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from modbus_event_connect._errors import AuthenticationError
from modbus_event_connect.micro_nabto import _wire as wire
from modbus_event_connect import Attempts, Exponential, Fixed, Retry
from modbus_event_connect.micro_nabto._connection import (
    DiscoveredDevice,
    MicroNabtoConnection,
    discover,
)
from modbus_event_connect.testing._clock import FakeClock
from modbus_event_connect.testing._micro_nabto import SimulatedMicroNabtoDevice

EMAIL = "user@example.invalid"
IDENTITY = {"device_number": 7, "device_model": 1140, "slave_device_number": 72270, "slave_device_model": 1}


def _device(*, clock: FakeClock | None = None, session_timeout: float | None = None,
            first: int = 1000) -> SimulatedMicroNabtoDevice:
    return SimulatedMicroNabtoDevice(identity=IDENTITY, datapoint_registers={(0, a): first + a for a in range(1, 41)},
                                session_timeout=session_timeout, clock=clock)


def _connection(device: SimulatedMicroNabtoDevice, *, email: str = EMAIL, host: bool = True,
                device_id: bool = False, clock: FakeClock | None = None,
                session_idle: float = 12.0, reconnect: Retry | None = None) -> MicroNabtoConnection:
    address, port = device.address
    return MicroNabtoConnection(email, host=address if host else None, port=port,
                                device_id=device.device_id if device_id else None,
                                timeout=0.05, retries=1, reconnect=reconnect, session_idle=session_idle,
                                discovery_target=device.address, clock=clock)


async def _read(connection: MicroNabtoConnection, *addresses: int) -> list[int] | None:
    answer = await connection.request(wire.datapoint_read([(0, a) for a in addresses]))
    return wire.datapoint_values(answer) if answer is not None else None


# ================================================================================ handshake

async def test_the_handshake_returns_the_identity_the_device_reports() -> None:
    async with _device() as device:
        assert await _connection(device).open() == IDENTITY


async def test_an_email_the_device_does_not_know_is_refused() -> None:
    async with _device() as device:
        with pytest.raises(AuthenticationError):
            await _connection(device, email="stranger@example.invalid").open()


async def test_a_silent_device_gives_no_identity() -> None:
    async with _device() as device:
        device.silent = True
        assert await _connection(device).open() is None


async def test_a_connection_is_made_without_an_event_loop() -> None:
    def build() -> MicroNabtoConnection:
        return MicroNabtoConnection(EMAIL, host="device.invalid", reconnect=None)
    connection = await asyncio.to_thread(build)
    assert not connection.connected


def test_a_connection_needs_a_host_or_a_device_id() -> None:
    with pytest.raises(ValueError):
        MicroNabtoConnection(EMAIL, reconnect=None)


# ============================================================ one connection, one device each

async def test_two_connections_keep_their_own_answers() -> None:
    async with _device(first=1000) as one, _device(first=2000) as two:
        first, second = _connection(one), _connection(two)
        await first.open()
        await second.open()
        answers = await asyncio.gather(*(_read(c, 1) for _ in range(10) for c in (first, second)))
        assert answers == [[1001], [2001]] * 10


async def test_every_connection_has_its_own_client_id() -> None:
    async with _device() as device:
        await _connection(device).open()
        await _connection(device).open()
        assert len(set(device.handshakes)) == 2


async def test_concurrent_requests_on_one_connection_each_get_their_own_answer() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        answers = await asyncio.gather(*(_read(connection, a) for a in range(1, 21)))
        assert answers == [[1000 + a] for a in range(1, 21)]


async def test_an_unanswered_request_gives_none_rather_than_raising() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        device.silent = True
        assert await _read(connection, 1) is None


async def test_an_answer_arriving_before_the_wait_begins_is_not_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        # The device answers inside sendto, before the request has started waiting.
        def deliver(data: bytes, addr: object = None) -> None:
            device._receive(data, ("127.0.0.1", 1))
        monkeypatch.setattr(device, "_send", connection._receive)
        monkeypatch.setattr(connection._transport, "sendto", deliver)
        assert await _read(connection, 1) == [1001]


# ======================================================================= a real device's ways

async def test_a_lost_datagram_is_sent_again() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        device.drop = 1
        assert await _read(connection, 1) == [1001]
        assert connection.diagnostics()["resends"] == 1


async def test_the_last_exchange_says_the_request_was_sent_again_and_holds_the_answer() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        device.drop = 1
        await _read(connection, 1)
        exchange = connection.last_exchange
        assert exchange is not None and exchange.sends == 2
        assert exchange.answer is not None and exchange.answer[0] == 0x16
        assert (1001).to_bytes(2, "big") in exchange.answer


async def test_a_new_session_counts_its_requests_and_its_age_afresh() -> None:
    clock = FakeClock()
    async with _device(clock=clock) as device:
        connection = _connection(device, clock=clock)
        await connection.open()
        clock.advance(3.0)
        await _read(connection, 1)
        exchange = connection.last_exchange
        assert exchange is not None and (exchange.session_request, exchange.session_age) == (2, 3.0)
        clock.advance(13.0)                                              # past session_idle
        await _read(connection, 1)
        exchange = connection.last_exchange
        assert exchange is not None and (exchange.session_request, exchange.session_age) == (2, 0.0)


def test_the_wait_for_a_later_answer_is_the_connections_then_the_models_then_five_seconds() -> None:
    connection = MicroNabtoConnection(EMAIL, host="device.invalid", reconnect=None)
    assert connection.later_answer_wait == 5.0
    connection.use_model_later_answer_wait(2.0)
    assert connection.later_answer_wait == 2.0
    given = MicroNabtoConnection(EMAIL, host="device.invalid", reconnect=None, later_answer_wait=7.0)
    given.use_model_later_answer_wait(2.0)
    assert given.later_answer_wait == 7.0
    assert given.diagnostics()["later_answer_wait"] == 7.0
    with pytest.raises(ValueError):
        MicroNabtoConnection(EMAIL, host="device.invalid", reconnect=None, later_answer_wait=-1.0)


async def test_diagnostics_give_the_longest_answer_too() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        await _read(connection, 1)
        longest, average = connection.diagnostics()["max_latency"], connection.diagnostics()["average_latency"]
        assert isinstance(longest, float) and isinstance(average, float) and longest >= average


async def test_a_session_left_unused_is_renewed_before_the_device_ends_it() -> None:
    clock = FakeClock()
    async with _device(clock=clock, session_timeout=15.0) as device:
        connection = _connection(device, clock=clock)
        await connection.open()
        clock.advance(13.0)
        assert await _read(connection, 1) == [1001]
        assert len(device.handshakes) == 2
        assert connection.diagnostics()["resends"] == 0, "no request was lost to an ended session"


async def test_a_session_in_use_is_kept() -> None:
    clock = FakeClock()
    async with _device(clock=clock, session_timeout=15.0) as device:
        connection = _connection(device, clock=clock)
        await connection.open()
        for _ in range(5):
            clock.advance(10.0)
            assert await _read(connection, 1) == [1001]
        assert len(device.handshakes) == 1


async def test_a_restarted_device_is_reached_again_within_the_same_request() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        device.restart()
        assert await _read(connection, 1) == [1001]
        assert len(device.handshakes) == 2


async def test_a_device_given_a_new_address_is_found_again_by_its_id() -> None:
    async with _device() as device:
        connection = _connection(device, device_id=True)
        await connection.open()
        await device.move()
        assert await _read(connection, 1) == [1001]
        assert connection.diagnostics()["rediscoveries"] == 1


async def test_without_a_device_id_a_device_at_a_new_address_is_out_of_reach() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        await device.move()
        assert await _read(connection, 1) is None


async def test_with_only_a_device_id_the_device_is_discovered() -> None:
    async with _device() as device:
        assert await _connection(device, host=False, device_id=True).open() == IDENTITY


async def test_after_a_failed_handshake_requests_fail_at_once_until_retry_after() -> None:
    clock = FakeClock()
    async with _device(clock=clock) as device:
        device.silent = True
        connection = _connection(device, clock=clock, reconnect=Retry(Fixed(10.0)))
        assert await connection.open() is None
        reached = device.datagrams
        assert await _read(connection, 1) is None
        assert device.datagrams == reached, "nothing is sent while the device is known to be away"
        clock.advance(10.5)
        device.silent = False
        assert await _read(connection, 1) == [1001]


async def test_a_failed_handshake_pauses_longer_each_time_by_its_rule() -> None:
    clock = FakeClock()
    async with _device(clock=clock) as device:
        device.silent = True
        address, port = device.address
        connection = MicroNabtoConnection(EMAIL, host=address, port=port, timeout=0.05, retries=1, clock=clock,
                                          reconnect=Retry(Exponential(10.0)))
        assert await connection.open() is None
        clock.advance(10)
        assert await _read(connection, 1) is None
        reached = device.datagrams
        clock.advance(19)
        assert await _read(connection, 1) is None
        assert device.datagrams == reached, "a handshake was tried before the second, longer pause was over"
        assert connection.diagnostics()["handshake_pauses"] == 2


def test_a_connection_keeps_the_reconnect_it_was_given() -> None:
    reconnect = Retry(Fixed(5.0))
    assert MicroNabtoConnection(EMAIL, host="device.invalid", reconnect=reconnect).reconnect is reconnect
    assert MicroNabtoConnection(EMAIL, host="device.invalid", reconnect=None).reconnect is None


def test_a_connection_must_be_told_when_to_try_a_handshake_again() -> None:
    unstated: dict[str, Any] = {}
    with pytest.raises(TypeError, match="reconnect"):
        MicroNabtoConnection(EMAIL, host="device.invalid", **unstated)


async def test_without_a_reconnect_a_failed_handshake_is_tried_again_only_on_open() -> None:
    clock = FakeClock()
    async with _device(clock=clock) as device:
        device.silent = True
        address, port = device.address
        connection = MicroNabtoConnection(EMAIL, host=address, port=port, timeout=0.05, retries=1, clock=clock,
                                          reconnect=None)
        assert await connection.open() is None
        clock.advance(86400)
        reached = device.datagrams
        assert await _read(connection, 1) is None
        assert device.datagrams == reached, "a handshake was tried again without a reconnect"
        device.silent = False
        assert await connection.open() is not None


async def test_a_reconnect_that_gives_up_leaves_the_handshake_untried_until_open() -> None:
    clock = FakeClock()
    async with _device(clock=clock) as device:
        device.silent = True
        address, port = device.address
        connection = MicroNabtoConnection(EMAIL, host=address, port=port, timeout=0.05, retries=1, clock=clock,
                                          reconnect=Retry(Fixed(10.0), Attempts(2)))
        assert await connection.open() is None
        clock.advance(10)
        assert await _read(connection, 1) is None
        reached = device.datagrams
        clock.advance(86400)
        assert await _read(connection, 1) is None
        assert device.datagrams == reached
        device.silent = False
        assert await connection.open() is not None


async def test_a_reconnect_not_ready_holds_the_handshake_back_for_another_pause() -> None:
    @dataclass(frozen=True)
    class Offline(Retry):
        online: list[bool] = field(default_factory=lambda: [False])

        async def ready(self) -> bool:
            return self.online[0]

    clock = FakeClock()
    reconnect = Offline(Fixed(10.0))
    async with _device(clock=clock) as device:
        device.silent = True
        address, port = device.address
        connection = MicroNabtoConnection(EMAIL, host=address, port=port, timeout=0.05, retries=1, clock=clock,
                                          reconnect=reconnect)
        assert await connection.open() is None
        device.silent = False
        clock.advance(10)
        reached = device.datagrams
        assert await _read(connection, 1) is None
        assert device.datagrams == reached, "a handshake was tried while the reconnect was not ready"
        reconnect.online[0] = True
        clock.advance(10)
        assert await _read(connection, 1) == [1001]


async def test_every_failed_handshake_is_logged_for_debugging(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    async with _device(clock=clock) as device:
        device.silent = True
        address, port = device.address
        connection = MicroNabtoConnection(EMAIL, host=address, port=port, timeout=0.05, retries=1, clock=clock,
                                          reconnect=Retry(Fixed(10.0), Attempts(2)))
        with caplog.at_level(logging.DEBUG, logger="modbus_event_connect.micro_nabto._connection"):
            await connection.open()
            clock.advance(10)
            await _read(connection, 1)
    messages = [r.getMessage() for r in caplog.records
                if r.name == "modbus_event_connect.micro_nabto._connection" and "handshake" in r.getMessage()]
    assert messages[-2:] == ["micro_nabto: handshake 1 failed; trying again in 10.0 s",
                             "micro_nabto: handshake 2 failed; not tried again until connected"]


async def test_a_reconnect_not_ready_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    @dataclass(frozen=True)
    class Offline(Retry):
        async def ready(self) -> bool:
            return False

    clock = FakeClock()
    async with _device(clock=clock) as device:
        device.silent = True
        address, port = device.address
        connection = MicroNabtoConnection(EMAIL, host=address, port=port, timeout=0.05, retries=1, clock=clock,
                                          reconnect=Offline(Fixed(10.0)))
        await connection.open()
        clock.advance(10)
        with caplog.at_level(logging.DEBUG, logger="modbus_event_connect.micro_nabto._connection"):
            await _read(connection, 1)
    assert "micro_nabto: reconnect not ready; trying a handshake again in 10.0 s" in caplog.messages


async def test_a_request_the_device_has_received_is_not_sent_again_while_it_answers() -> None:
    async with _device() as device:
        device.answer_delay = 0.2      # four times the connection's timeout
        connection = _connection(device)
        await connection.open()
        sent = device.datagrams
        assert await _read(connection, 1) == [1001]
        assert device.datagrams == sent + 1
        assert connection.diagnostics()["resends"] == 0
        received = connection.diagnostics()["received"]
        assert isinstance(received, int) and received >= 1


async def test_an_answer_that_never_comes_after_the_receipt_is_none() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        device.answer_delay = 10.0
        assert await _read(connection, 1) is None


def test_the_wait_for_an_answer_cannot_be_shorter_than_the_timeout() -> None:
    with pytest.raises(ValueError):
        MicroNabtoConnection(EMAIL, host="192.0.2.1", timeout=1.0, answer_wait=0.5, reconnect=None)


async def test_a_duplicated_answer_is_not_taken_for_the_next() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        device.duplicate = True
        assert [await _read(connection, a) for a in (1, 2, 3)] == [[1001], [1002], [1003]]


async def test_a_closed_connection_has_no_session() -> None:
    async with _device() as device:
        connection = _connection(device)
        await connection.open()
        await connection.close()
        assert not connection.connected


# ================================================================================ discovery

async def test_discovery_lists_the_devices_that_answer() -> None:
    async with _device() as device:
        found = await discover(timeout=0.2, target=device.address)
        assert found == [DiscoveredDevice(device.device_id, *device.address)]


async def test_discovery_for_an_unknown_id_finds_nothing() -> None:
    async with _device() as device:
        assert await discover("other.device.invalid", timeout=0.1, target=device.address) == []


# ================================================================================== privacy

async def test_nothing_logged_or_diagnosed_names_the_host_port_device_or_email() -> None:
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)
    logger = logging.getLogger("modbus_event_connect")
    handler, level = Keep(level=logging.DEBUG), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        async with _device() as device:
            connection = _connection(device, device_id=True)
            await connection.open()
            await _read(connection, 1)
            await device.move()
            await _read(connection, 2)
            device.silent = True
            await _read(connection, 3)
            device.silent = False
            with pytest.raises(AuthenticationError):
                await _connection(device, email="stranger@example.invalid").open()
            host, port = device.address
            said = [r.getMessage() for r in records] + [repr(connection.diagnostics())]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert records, "the scenario logs something, so the check below is not vacuous"
    for secret in (host, str(port), device.device_id, EMAIL, "stranger@example.invalid"):
        assert not [line for line in said if secret in line], secret


async def test_a_cancelled_handshake_probe_lets_the_next_one_through() -> None:
    clock = FakeClock()
    async with _device(clock=clock) as device:
        device.silent = True
        connection = _connection(device, clock=clock, reconnect=Retry(Fixed(10.0)))
        assert await connection.open() is None
        clock.advance(10)
        probe = asyncio.ensure_future(_read(connection, 1))
        await asyncio.sleep(0.01)
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe
        device.silent = False
        assert await _read(connection, 1) == [1001]
