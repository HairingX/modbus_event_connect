"""The micro_nabto session against a simulated device on a localhost UDP port."""
import asyncio
import logging

import pytest

from src.modbus_event_connect.errors import AuthenticationError
from src.modbus_event_connect.micro_nabto import wire
from src.modbus_event_connect.micro_nabto.connection import (
    DiscoveredDevice,
    MicroNabtoConnection,
    discover,
)
from src.modbus_event_connect.testing.clock import FakeClock
from src.modbus_event_connect.testing.micro_nabto import SimulatedMicroNabtoDevice

EMAIL = "user@example.invalid"
IDENTITY = {"device_number": 7, "device_model": 1140, "slave_device_number": 72270, "slave_device_model": 1}


def _device(*, clock: FakeClock | None = None, session_timeout: float | None = None,
            first: int = 1000) -> SimulatedMicroNabtoDevice:
    return SimulatedMicroNabtoDevice(identity=IDENTITY, datapoint_registers={(0, a): first + a for a in range(1, 41)},
                                session_timeout=session_timeout, clock=clock)


def _connection(device: SimulatedMicroNabtoDevice, *, email: str = EMAIL, host: bool = True,
                device_id: bool = False, clock: FakeClock | None = None,
                session_idle: float = 12.0) -> MicroNabtoConnection:
    address, port = device.address
    return MicroNabtoConnection(email, host=address if host else None, port=port,
                                device_id=device.device_id if device_id else None,
                                timeout=0.05, retries=1, backoff_for=10.0, session_idle=session_idle,
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
        return MicroNabtoConnection(EMAIL, host="device.invalid")
    connection = await asyncio.to_thread(build)
    assert not connection.connected


def test_a_connection_needs_a_host_or_a_device_id() -> None:
    with pytest.raises(ValueError):
        MicroNabtoConnection(EMAIL)


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
        connection = _connection(device, clock=clock)
        assert await connection.open() is None
        reached = device.datagrams
        assert await _read(connection, 1) is None
        assert device.datagrams == reached, "nothing is sent while the device is known to be away"
        clock.advance(10.5)
        device.silent = False
        assert await _read(connection, 1) == [1001]


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
    logger = logging.getLogger("src.modbus_event_connect")
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
