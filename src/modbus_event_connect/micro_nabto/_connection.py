"""A micro_nabto session over UDP: the handshake, then requests one at a time."""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import _wire as wire
from .._clock import Clock, SystemClock
from .._device import Identity
from .._errors import AuthenticationError

_LOGGER = logging.getLogger(__name__)

_SEQUENCE_MAX = 0xFFFF
_Receive = Callable[[bytes, tuple[str, int]], None]


@dataclass(frozen=True)
class DiscoveredDevice:
    device_id: str
    host: str
    port: int


class _Endpoint(asyncio.DatagramProtocol):
    def __init__(self, receive: _Receive) -> None:
        self._receive = receive

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self._receive(data, (str(addr[0]), addr[1]))


async def _open_endpoint(receive: _Receive) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _Endpoint(receive), local_addr=("0.0.0.0", 0), allow_broadcast=True)
    return transport


async def discover(device_id: str | None = None, *, timeout: float = 2.0, resend: float = 0.5,
                   target: tuple[str, int] = wire.BROADCAST) -> list[DiscoveredDevice]:
    """The devices that answer a discovery broadcast within `timeout`.

    With `device_id`, only that device is asked for, and the search ends when it answers.
    The broadcast is repeated every `resend` seconds, since a datagram can be lost.
    """
    found: dict[str, DiscoveredDevice] = {}
    answered = asyncio.Event()

    def receive(data: bytes, addr: tuple[str, int]) -> None:
        found_id = wire.discovery_reply(data)
        if found_id is None or (device_id is not None and found_id != device_id):
            return
        found[found_id] = DiscoveredDevice(found_id, addr[0], addr[1])
        if device_id is not None:
            answered.set()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    transport = await _open_endpoint(receive)
    try:
        packet = wire.discovery_request(device_id)
        while (remaining := deadline - loop.time()) > 0:
            transport.sendto(packet, target)
            try:
                await asyncio.wait_for(answered.wait(), min(resend, remaining))
                break
            except asyncio.TimeoutError:
                pass
    finally:
        transport.close()
    return list(found.values())


class MicroNabtoConnection:
    """A session with one device; requests are sent one at a time.

    uNabto's reference implementation ends a session left unused for 7/2 times the keep-alive
    interval the client states, 17.5 s when it states none, as this client does; a Nilan
    CTS 402 was measured ending one between 15 and 20 s. A session unused for `session_idle`
    is therefore established anew before the next request. An unanswered request is sent again
    `retries` times; if the device stays silent, the session is established anew, at an
    address found by `device_id` if it has changed, and the request tried once more. After a
    failed handshake, requests fail at once until `backoff_for` seconds have passed.
    """

    def __init__(self, email: str, *, host: str | None = None, device_id: str | None = None,
                 port: int = wire.DEVICE_PORT, timeout: float = 1.0, retries: int = 2,
                 backoff_for: float = 10.0, session_idle: float = 12.0,
                 discovery_target: tuple[str, int] = wire.BROADCAST, clock: Clock | None = None) -> None:
        """Args:
            email: the account paired with the device; it is never logged.
            host, device_id: where to reach the device; with only `device_id`, it is discovered.
            timeout: seconds to wait for each answer.
        """
        if host is None and device_id is None:
            raise ValueError("a connection needs a host, a device_id, or both")
        if timeout <= 0 or session_idle <= 0 or retries < 0 or backoff_for < 0:
            raise ValueError("timeout and session_idle must be positive; retries and backoff_for "
                             "cannot be negative")
        self._email = email
        self._device_id = device_id
        self._address: tuple[str, int] | None = (host, port) if host is not None else None
        self._timeout = timeout
        self._retries = retries
        self._backoff_for = backoff_for
        self._session_idle = session_idle
        self._discovery_target = discovery_target
        self._clock: Clock = clock if clock is not None else SystemClock()

        self._client_id = secrets.token_bytes(4)
        self._server_id: bytes | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._waiting: tuple[int, asyncio.Future[wire.ConnectReply | wire.DataReply]] | None = None
        self._backoff_until: float | None = None
        self._last_answer = 0.0

        self._exchanges = 0
        self._answered = 0
        self._resends = 0
        self._handshakes = 0
        self._rediscoveries = 0
        self._latency_total = 0.0

    @property
    def connected(self) -> bool:
        return self._server_id is not None

    async def open(self) -> Identity | None:
        """Establish a session.

        Returns:
            The device's identity from the handshake, or None if the device did not answer.

        Raises:
            AuthenticationError: the device refused the email.
        """
        async with self._lock:
            self._backoff_until = None
            return await self._establish()

    async def close(self) -> None:
        self._server_id = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def request(self, command: bytes) -> bytes | None:
        """Send `command` and return the device's answer, or None if it gave none."""
        async with self._lock:
            if not await self._ready():
                return None
            answer = await self._data(command)
            if answer is None:
                _LOGGER.info("micro_nabto: no answer; establishing the session anew")
                if not await self._reestablish():
                    return None
                answer = await self._data(command)
            return answer

    async def send(self, command: bytes) -> bool:
        """Send `command` without waiting for an answer. False if there is no session to send it on."""
        async with self._lock:
            if not await self._ready() or self._server_id is None:
                return False
            self._send(wire.data_request(self._client_id, self._server_id, self._next_sequence(), command))
            return True

    def diagnostics(self) -> Mapping[str, object]:
        """Counters for a bug report; never an address, a device id or the email."""
        return {
            "connected": self.connected,
            "exchanges": self._exchanges,
            "answered": self._answered,
            "resends": self._resends,
            "handshakes": self._handshakes,
            "rediscoveries": self._rediscoveries,
            "average_latency": self._latency_total / self._answered if self._answered else None,
        }

    # ------------------------------------------------------------------------ session

    async def _ready(self) -> bool:
        if self._server_id is not None:
            if self._clock.monotonic() - self._last_answer <= self._session_idle:
                return True
            return await self._reestablish()
        if self._backoff_until is not None and self._clock.monotonic() < self._backoff_until:
            return False
        return await self._reestablish()

    async def _reestablish(self) -> bool:
        try:
            return await self._establish() is not None
        except AuthenticationError:
            _LOGGER.error("micro_nabto: the device refused the handshake; is the email still paired with it?")
            return False

    async def _establish(self) -> Identity | None:
        self._server_id = None
        if self._transport is None:
            self._transport = await _open_endpoint(self._receive)
        identity = await self._handshake() if self._address is not None else None
        if identity is None and self._device_id is not None:
            found = await discover(self._device_id, timeout=self._timeout * (self._retries + 1),
                                   target=self._discovery_target)
            if found and (found[0].host, found[0].port) != self._address:
                self._rediscoveries += 1
                self._address = (found[0].host, found[0].port)
                identity = await self._handshake()
        self._backoff_until = None if identity is not None else self._clock.monotonic() + self._backoff_for
        return identity

    async def _handshake(self) -> Identity | None:
        connected = await self._exchange(
            lambda sequence: wire.connect_request(self._client_id, sequence, self._email))
        if not isinstance(connected, wire.ConnectReply):
            return None
        if not connected.accepted:
            raise AuthenticationError("the device refused the handshake; is the email paired with it?")
        self._server_id = connected.server_id
        answer = await self._data(wire.ping())
        found = wire.identity(answer) if answer is not None else None
        if found is None:
            self._server_id = None
            return None
        self._handshakes += 1
        return found

    # ------------------------------------------------------------------------ exchange

    async def _data(self, command: bytes) -> bytes | None:
        server_id = self._server_id
        if server_id is None:
            return None
        answer = await self._exchange(
            lambda sequence: wire.data_request(self._client_id, server_id, sequence, command))
        return answer.payload if isinstance(answer, wire.DataReply) else None

    async def _exchange(self, packet: Callable[[int], bytes]) -> wire.ConnectReply | wire.DataReply | None:
        """Send a packet and wait for its reply, sending it again while none comes."""
        sequence = self._next_sequence()
        datagram = packet(sequence)
        loop = asyncio.get_running_loop()
        self._exchanges += 1
        for attempt in range(self._retries + 1):
            future: asyncio.Future[wire.ConnectReply | wire.DataReply] = loop.create_future()
            self._waiting = (sequence, future)
            if attempt:
                self._resends += 1
            started = self._clock.monotonic()
            self._send(datagram)
            try:
                answer = await asyncio.wait_for(future, self._timeout)
            except asyncio.TimeoutError:
                continue
            finally:
                self._waiting = None
            self._answered += 1
            self._last_answer = self._clock.monotonic()
            self._latency_total += self._last_answer - started
            return answer
        return None

    def _send(self, datagram: bytes) -> None:
        if self._transport is not None and self._address is not None and not self._transport.is_closing():
            self._transport.sendto(datagram, self._address)

    def _receive(self, data: bytes, addr: tuple[str, int]) -> None:
        answer = wire.reply(data, self._client_id)
        waiting = self._waiting
        if answer is None or waiting is None:
            return
        sequence, future = waiting
        if answer.sequence == sequence and not future.done():
            future.set_result(answer)

    def _next_sequence(self) -> int:
        self._sequence = self._sequence % _SEQUENCE_MAX + 1
        return self._sequence
