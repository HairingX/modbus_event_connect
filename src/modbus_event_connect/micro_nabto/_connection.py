"""A micro_nabto session over UDP: the handshake, then requests one at a time."""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from event_connect import AuthenticationError, Breaker, CircuitBreaker, Clock, SystemClock

from . import _wire as wire
from .._device import Identity

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


@dataclass(frozen=True)
class Exchange:
    """How one request and its answer went, for a bug report."""
    sequence: int
    sends: int
    """How many times the request was sent: more than once when the device did not acknowledge
    it in time."""
    acknowledged: bool
    """Whether the device said it had the request before it answered."""
    seconds: float
    """From the last sending to the answer."""
    answer: bytes | None
    """The answer's datagram from its kind on, the session's ids left out; None without one, and
    for a ping, whose answer carries the email."""
    at: float = 0.0
    """Monotonic time the exchange ended."""
    session_age: float | None = None
    """Seconds since the session was established; None before the first."""
    session_request: int = 0
    """Which request of the session this was, the handshake's ping being the first."""
    set_aside: bool = False
    """Whether an answer to the request was set aside for a later answer to it."""
    waited: float = 0.0
    """Seconds the request was kept open for a later answer; 0 when it was not."""
    session_reason: str = ""
    """Why the session was established, and how long after the answer before it."""


class MicroNabtoConnection:
    """A session with one device; requests are sent one at a time.

    uNabto's reference implementation ends a session left unused for 7/2 times the keep-alive
    interval the client states, 17.5 s when it states none, as this client does; a Nilan
    CTS 402 was measured ending one between 15 and 20 s. A session unused for `session_idle`
    is therefore established anew before the next request. An unanswered request is sent again
    `retries` times; if the device stays silent, the session is established anew, at an
    address found by `device_id` if it has changed, and the request tried once more. Once
    handshakes in a row have failed, requests fail at once until `reconnect` says to try one again.
    """

    def __init__(self, email: str, *, host: str | None = None, device_id: str | None = None,
                 reconnect: CircuitBreaker, port: int = wire.DEVICE_PORT, timeout: float = 1.0,
                 retries: int = 2, answer_wait: float = 5.0, session_idle: float = 12.0,
                 discovery_target: tuple[str, int] = wire.BROADCAST, clock: Clock | None = None) -> None:
        """Args:
            email: the account paired with the device; it is never logged.
            host, device_id: where to reach the device; with only `device_id`, it is discovered.
            reconnect: after how many failed handshakes in a row no request is sent, and when a
                handshake is tried again; its retry's `ready()` is asked before each try.
            timeout: seconds to wait for each answer.
            answer_wait: seconds to wait for an answer once the device has said it has the
                request; it is not sent again meanwhile.
        """
        if host is None and device_id is None:
            raise ValueError("a connection needs a host, a device_id, or both")
        if timeout <= 0 or session_idle <= 0 or retries < 0 or answer_wait < timeout:
            raise ValueError("timeout and session_idle must be positive; retries cannot be "
                             "negative; answer_wait cannot be shorter than timeout")
        self._email = email
        self._device_id = device_id
        self._address: tuple[str, int] | None = (host, port) if host is not None else None
        self._timeout = timeout
        self._retries = retries
        self._answer_wait = answer_wait
        self._reconnect = reconnect
        self._session_idle = session_idle
        self._discovery_target = discovery_target
        self._clock: Clock = clock if clock is not None else SystemClock()

        self._client_id = secrets.token_bytes(4)
        self._server_id: bytes | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._waiting: tuple[int, asyncio.Future[wire.ConnectReply | wire.DataReply], asyncio.Event] | None = None
        self._breaker: Breaker = reconnect.breaker()
        self._last_answer = 0.0

        self._exchanges = 0
        self._answered = 0
        self._resends = 0
        self._received = 0
        self._handshakes = 0
        self._rediscoveries = 0
        self._latency_total = 0.0
        self._last_exchange: Exchange | None = None
        self._session_started: float | None = None
        self._session_requests = 0
        self._max_latency = 0.0
        self._later_answer_waits = 0
        self._later_answers_taken = 0
        self._longest_later_answer_wait = 0.0
        self._next_reason = "on connect"
        self._session_reason = ""
        self._silent_since: float | None = None
        """When the answer came before the request that went unanswered; None while answering."""
        self._unanswered = 0
        self._silences = 0
        self._longest_silence = 0.0
        self._refusals: Counter[int] = Counter()
        self._refusal_logged: int | None = None
        self._answer_before = 0.0
        """When the last answer before the session being established came."""

    @property
    def connected(self) -> bool:
        return self._server_id is not None

    @property
    def clock(self) -> Clock:
        """The clock every time this connection measures is on."""
        return self._clock

    @property
    def reconnect(self) -> CircuitBreaker:
        """After how many failed handshakes in a row no request is sent, and when one is tried again."""
        return self._reconnect

    @property
    def last_exchange(self) -> Exchange | None:
        """How the last request sent went; None before the first. The answer to a ping, which
        carries the email, is not kept."""
        return self._last_exchange

    async def open(self) -> Identity | None:
        """Establish a session.

        Returns:
            The device's identity from the handshake, or None if the device did not answer.

        Raises:
            AuthenticationError: the device refused the email.
        """
        async with self._lock:
            self._breaker.reset()
            self._next_reason = "on connect"
            return await self._establish()

    async def close(self) -> None:
        self._server_id = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def request(self, command: bytes, *, accept: Callable[[bytes], bool] | None = None,
                      later_answer_wait: float = 0.0) -> bytes | None:
        """Send `command` and return the device's answer, or None if it gave none.

        Args:
            accept: whether an answer is one to take. An answer it refuses, to a request sent more
                than once, is set aside while a later answer to the same request is waited for, up
                to `later_answer_wait` seconds; that one is taken if `accept` takes it, and the
                first otherwise.
        """
        async with self._lock:
            if not await self._ready():
                return None
            answer = await self._data(command, accept, later_answer_wait)
            if answer is None:
                self._note_unanswered()
                if not await self._reestablish():
                    return None
                answer = await self._data(command, accept, later_answer_wait)
            return answer

    def diagnostics(self) -> Mapping[str, object]:
        """Counters for a bug report; never an address, a device id or the email."""
        return {
            "connected": self.connected,
            "exchanges": self._exchanges,
            "answered": self._answered,
            "resends": self._resends,
            "received": self._received,
            "handshakes": self._handshakes,
            "rediscoveries": self._rediscoveries,
            "average_latency": self._latency_total / self._answered if self._answered else None,
            "max_latency": self._max_latency if self._answered else None,
            "later_answer_waits": self._later_answer_waits,
            "later_answers_taken": self._later_answers_taken,
            "longest_later_answer_wait": self._longest_later_answer_wait,
            "silences": self._silences,
            "longest_silence": self._longest_silence,
            "handshake_pauses": self._breaker.openings,
            "refusals": {f"0x{status:04x}": count for status, count in sorted(self._refusals.items())},
        }

    def _note_unanswered(self) -> None:
        """Log the first request a device leaves unanswered, once until it answers again."""
        self._unanswered += 1
        exchange = self._last_exchange
        request = exchange.session_request if exchange is not None else 0
        self._next_reason = f"after no answer to request {request}"
        if self._silent_since is not None:
            _LOGGER.debug("micro_nabto: no answer again, %d unanswered", self._unanswered)
            return
        self._silent_since = self._last_answer
        self._silences += 1
        age = f"{exchange.session_age:.1f}" if exchange is not None and exchange.session_age is not None else "?"
        _LOGGER.warning("micro_nabto: no answer to request %d of a session %s s old (%s), sent %d time(s), "
                        "%.1f s after the last answer; establishing the session anew",
                        request, age, self._session_reason or "no session",
                        exchange.sends if exchange is not None else 0,
                        self._clock.monotonic() - self._last_answer)

    def _note_answered(self) -> None:
        if self._silent_since is None:
            return
        silent = self._clock.monotonic() - self._silent_since
        self._longest_silence = max(self._longest_silence, silent)
        _LOGGER.warning("micro_nabto: answering again, %.1f s after the last answer before it went silent; "
                        "%d request(s) unanswered meanwhile", silent, self._unanswered)
        self._silent_since = None
        self._unanswered = 0

    # ------------------------------------------------------------------------ session

    async def _ready(self) -> bool:
        if self._server_id is not None:
            idle = self._clock.monotonic() - self._last_answer
            if idle <= self._session_idle:
                return True
            self._next_reason = f"after {idle:.1f} s unused"
            return await self._reestablish()
        return await self._reestablish()

    async def _reestablish(self) -> bool:
        try:
            return await self._establish() is not None
        except AuthenticationError:
            _LOGGER.error("micro_nabto: the device refused the handshake; is the email still paired with it?")
            return False

    async def _establish(self) -> Identity | None:
        """Establish a session as `reconnect` allows; None if it holds the handshake back, or the
        device did not answer."""
        if not await self._ready_to_try():
            return None
        attempt = self._breaker.attempt(self._clock.monotonic())
        if attempt is None:
            return None
        with attempt:
            identity = await self._try_establish()
            if identity is None:
                attempt.unanswered(self._clock.monotonic())
                self._log_pause()
            else:
                attempt.answered()
        return identity

    async def _ready_to_try(self) -> bool:
        """Whether a handshake that is due may be tried, as `reconnect`'s retry says; True when
        none is held back."""
        breaker = self._breaker
        if not breaker.is_open or breaker.open_for(self._clock.monotonic()) > 0:
            return True
        if await self._reconnect.retry.ready():
            return True
        breaker.postpone(self._clock.monotonic())
        _LOGGER.debug("micro_nabto: reconnect not ready; trying a handshake again in %.1f s",
                      breaker.open_for(self._clock.monotonic()))
        return False

    def _log_pause(self) -> None:
        breaker = self._breaker
        if breaker.gave_up:
            _LOGGER.debug("micro_nabto: handshake %d failed; not tried again until connected",
                          breaker.openings)
        else:
            _LOGGER.debug("micro_nabto: handshake %d failed; trying again in %.1f s",
                          breaker.openings, breaker.open_for(self._clock.monotonic()))

    async def _try_establish(self) -> Identity | None:
        self._answer_before = self._last_answer
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
        return identity

    async def _handshake(self) -> Identity | None:
        connected = await self._exchange(
            lambda sequence: wire.connect_request(self._client_id, sequence, self._email))
        if not isinstance(connected, wire.ConnectReply):
            return None
        if not connected.accepted:
            if connected.status in (wire.REFUSED_ACCESS, 0):
                raise AuthenticationError("the device refused the handshake; is the email paired with it?")
            self._refused(connected.status)
            return None
        self._server_id = connected.server_id
        now = self._clock.monotonic()
        before = f", {now - self._answer_before:.1f} s after the last answer" if self._answer_before else ""
        self._session_reason = f"started {self._next_reason}{before}"
        self._next_reason = "after the session ended"
        self._refusal_logged = None
        self._session_started = now
        self._session_requests = 0
        answer = await self._data(wire.ping(), keep_answer=False)
        found = wire.identity(answer) if answer is not None else None
        if found is None:
            self._server_id = None
            return None
        self._handshakes += 1
        return found

    # ------------------------------------------------------------------------ exchange

    async def _data(self, command: bytes, accept: Callable[[bytes], bool] | None = None,
                    later_answer_wait: float = 0.0, *, keep_answer: bool = True) -> bytes | None:
        server_id = self._server_id
        if server_id is None:
            return None
        self._session_requests += 1
        answer = await self._exchange(
            lambda sequence: wire.data_request(self._client_id, server_id, sequence, command), accept,
            later_answer_wait, keep_answer=keep_answer)
        return answer.payload if isinstance(answer, wire.DataReply) else None

    async def _exchange(self, packet: Callable[[int], bytes], accept: Callable[[bytes], bool] | None = None,
                        later_answer_wait: float = 0.0, *,
                        keep_answer: bool = True) -> wire.ConnectReply | wire.DataReply | None:
        """Send a packet and wait for its reply, sending it again while none comes; `keep_answer`
        False leaves the answer out of `last_exchange`."""
        sequence = self._next_sequence()
        datagram = packet(sequence)
        loop = asyncio.get_running_loop()
        self._exchanges += 1
        acknowledged = False
        started = self._clock.monotonic()
        for attempt in range(self._retries + 1):
            future: asyncio.Future[wire.ConnectReply | wire.DataReply] = loop.create_future()
            received = asyncio.Event()
            self._waiting = (sequence, future, received)
            if attempt:
                self._resends += 1
            started = self._clock.monotonic()
            self._send(datagram)
            try:
                await asyncio.wait({future}, timeout=self._timeout)
                if not future.done() and received.is_set():
                    # uNabto: the device has the request and answers when it is ready; sending it
                    # again would only be acknowledged again.
                    await asyncio.wait({future}, timeout=self._answer_wait - self._timeout)
                    if not future.done():
                        self._exchanged(sequence, attempt + 1, True, started, None)
                        return None
                acknowledged = acknowledged or received.is_set()
                if not future.done():
                    continue
                answer = future.result()
            finally:
                self._waiting = None
                if not future.done():
                    future.cancel()
            self._answered += 1
            self._note_answered()
            self._last_answer = self._clock.monotonic()
            self._latency_total += self._last_answer - started
            self._max_latency = max(self._max_latency, self._last_answer - started)
            set_aside, waited = False, 0.0
            if (accept is not None and attempt and isinstance(answer, wire.DataReply)
                    and not accept(answer.payload)):
                # A device that got the request twice may answer both sendings, and the answer
                # that came first need not be the one that holds the values.
                later = await self._later_answer(sequence, later_answer_wait)
                waited = self._clock.monotonic() - self._last_answer
                self._later_answer_waits += 1
                self._longest_later_answer_wait = max(self._longest_later_answer_wait, waited)
                if later is not None and accept(later.payload):
                    answer, set_aside = later, True
                    self._later_answers_taken += 1
            kept = answer.raw if keep_answer and isinstance(answer, wire.DataReply) else None
            self._exchanged(sequence, attempt + 1, acknowledged, started, kept, set_aside, waited)
            return answer
        self._exchanged(sequence, self._retries + 1, acknowledged, started, None)
        return None

    def _refused(self, status: int) -> None:
        """Count a handshake the device refused for a reason other than the email, and log it
        once a status until a session is established."""
        self._refusals[status] += 1
        if self._refusal_logged == status:
            return
        self._refusal_logged = status
        _LOGGER.warning("micro_nabto: the device refused the session with status 0x%04x (%s); trying again "
                        "later", status, wire.CONNECT_STATUSES.get(status, "unknown"))

    async def _later_answer(self, sequence: int, wait: float) -> wire.DataReply | None:
        """Another answer to the request with `sequence`, if one comes within `wait` seconds."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[wire.ConnectReply | wire.DataReply] = loop.create_future()
        self._waiting = (sequence, future, asyncio.Event())
        try:
            await asyncio.wait({future}, timeout=wait)
            answer = future.result() if future.done() else None
        finally:
            self._waiting = None
            if not future.done():
                future.cancel()
        return answer if isinstance(answer, wire.DataReply) else None

    def _exchanged(self, sequence: int, sends: int, acknowledged: bool, started: float,
                   answer: bytes | None, set_aside: bool = False, waited: float = 0.0) -> None:
        now = self._clock.monotonic()
        age = now - self._session_started if self._session_started is not None else None
        self._last_exchange = Exchange(sequence, sends, acknowledged, now - started, answer,
                                       now, age, self._session_requests, set_aside, waited, self._session_reason)

    def _send(self, datagram: bytes) -> None:
        if self._transport is not None and self._address is not None and not self._transport.is_closing():
            self._transport.sendto(datagram, self._address)

    def _receive(self, data: bytes, addr: tuple[str, int]) -> None:
        answer = wire.reply(data, self._client_id)
        waiting = self._waiting
        if answer is None or waiting is None:
            return
        sequence, future, received = waiting
        if answer.sequence != sequence or future.done():
            return
        if isinstance(answer, wire.Received):
            self._received += 1
            received.set()
        else:
            future.set_result(answer)

    def _next_sequence(self) -> int:
        self._sequence = self._sequence % _SEQUENCE_MAX + 1
        return self._sequence


def describe(exchange: Exchange) -> str:
    """`exchange` in one line for a log, its answer in hex."""
    answer = exchange.answer.hex(" ") if exchange.answer is not None else "none"
    reason = f" ({exchange.session_reason})" if exchange.session_reason else ""
    session = (f"request {exchange.session_request} of a session {exchange.session_age:.1f} s old{reason}"
               if exchange.session_age is not None else "no session")
    return (f"{session}, sequence {exchange.sequence}, sent {exchange.sends} time(s), "
            f"{'acknowledged' if exchange.acknowledged else 'not acknowledged'} first, "
            f"answer after {exchange.seconds:.3f} s{_later(exchange)}: {answer}")


def _later(exchange: Exchange) -> str:
    if not exchange.waited and not exchange.set_aside:
        return ""
    found = "and took the later one" if exchange.set_aside else "and none came"
    return f", then kept open {exchange.waited:.3f} s for a later answer {found}"
