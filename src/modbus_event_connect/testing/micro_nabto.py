"""A simulated micro_nabto device on a localhost UDP port.

It answers as a Nilan CTS 402 was measured to answer.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..clock import Clock, SystemClock
from ..micro_nabto import wire

_DISCOVERY_REQUEST = b"\x00\x00\x00\x01"
_U_CONNECT, _DATA = 0x83, 0x16
_CP_ID = 0x3F
_PING, _DATAPOINT_READ, _SETPOINT_READ, _SETPOINT_WRITE = 0x11, 0x2D, 0x2A, 0x2B


@dataclass(frozen=True)
class Command:
    """One command the device received: its code and its (object, address[, register]) items."""
    code: int
    items: tuple[tuple[int, ...], ...]


@dataclass
class _Session:
    server_id: bytes
    email: str
    last_seen: float


class SimulatedMicroNabtoDevice:
    """A micro_nabto device: registers at (object, address), refusing any it does not have.

    A request naming one unknown address is refused whole. A session ends after
    `session_timeout` seconds without a request, and packets in an ended session are ignored.
    """

    def __init__(self, *, device_id: str = "simulated.device.invalid",
                 emails: frozenset[str] = frozenset({"user@example.invalid"}),
                 identity: Mapping[str, int] | None = None,
                 datapoint_registers: Mapping[tuple[int, int], int] | None = None,
                 setpoint_registers: Mapping[tuple[int, int], int] | None = None,
                 session_timeout: float | None = None, clock: Clock | None = None) -> None:
        self.device_id = device_id
        self.emails = emails
        self.identity: dict[str, int] = dict(identity or {
            "device_number": 1, "device_model": 1140, "slave_device_number": 72270, "slave_device_model": 1})
        self.datapoint_registers: dict[tuple[int, int], int] = dict(datapoint_registers or {})
        self.setpoint_registers: dict[tuple[int, int], int] = dict(setpoint_registers or {})
        self.session_timeout = session_timeout
        self.clock: Clock = clock if clock is not None else SystemClock()

        self.silent = False
        """Answer nothing at all, as a device that is off or out of reach."""
        self.drop = 0
        """Datagrams still to lose on the way in."""
        self.duplicate = False
        """Send every answer twice."""
        self.cut_short = False
        """Send read answers that stop before their last value."""

        self.datagrams = 0
        """Datagrams that reached the device, answered or not."""
        self.commands: list[Command] = []
        self.handshakes: list[bytes] = []
        """The client id of every accepted handshake."""
        self.discoveries = 0
        self.bad_checksums = 0

        self._sessions: dict[bytes, _Session] = {}
        self._next_server_id = 0x1A0
        self._transport: asyncio.DatagramTransport | None = None
        self._retired: list[asyncio.DatagramTransport] = []

    async def __aenter__(self) -> SimulatedMicroNabtoDevice:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    @property
    def address(self) -> tuple[str, int]:
        if self._transport is None:
            raise RuntimeError("the simulated device is not started")
        host, port = self._transport.get_extra_info("sockname")[:2]
        return str(host), int(port)

    async def start(self) -> tuple[str, int]:
        """Listen on a free localhost port and return its address."""
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Endpoint(self._on_datagram), local_addr=("127.0.0.1", 0))
        return self.address

    async def move(self) -> tuple[str, int]:
        """Move to another port, as a device given a new address; sessions survive.

        Discovery sent to the old port is still answered, from the new one, as a broadcast would be.
        """
        if self._transport is not None:
            self._retired.append(self._transport)
            self._transport = None
        return await self.start()

    def close(self) -> None:
        if self._transport is not None:
            self._retired.append(self._transport)
            self._transport = None
        for transport in self._retired:
            transport.close()
        self._retired.clear()

    def restart(self) -> None:
        """Forget every session, as a device that rebooted."""
        self._sessions.clear()

    def received(self, code: int) -> list[Command]:
        """The commands received with `code`, in order."""
        return [c for c in self.commands if c.code == code]

    # ---------------------------------------------------------------------- receiving

    def _on_datagram(self, data: bytes, addr: tuple[str, int], transport: asyncio.BaseTransport | None) -> None:
        self._receive(data, addr, retired=transport is not self._transport)

    def _receive(self, data: bytes, addr: tuple[str, int], *, retired: bool = False) -> None:
        self.datagrams += 1
        if self.silent:
            return
        if self.drop > 0:
            self.drop -= 1
            return
        if data.startswith(_DISCOVERY_REQUEST) and len(data) > 12:
            self._discovery(data, addr)
        elif retired:
            return
        elif len(data) >= wire.HEADER_LENGTH and data[8] == _U_CONNECT:
            self._connect(data, addr)
        elif len(data) >= wire.HEADER_LENGTH and data[8] == _DATA:
            self._data(data, addr)

    def _discovery(self, data: bytes, addr: tuple[str, int]) -> None:
        wanted = data[12:].split(b"\x00", 1)[0].decode("ascii", "replace")
        if wanted in ("*", self.device_id):
            self.discoveries += 1
            self._send(wire.DISCOVERY_REPLY + bytes(15) + self.device_id.encode("ascii") + b"\x00", addr)

    def _connect(self, data: bytes, addr: tuple[str, int]) -> None:
        client_id, sequence = data[:4], data[12:14]
        email = _email(data)
        if email is not None and email in self.emails:
            server_id = self._next_server_id.to_bytes(4, "big")
            self._next_server_id += 1
            self._sessions[client_id] = _Session(server_id, email, self.clock.monotonic())
            self.handshakes.append(client_id)
            status = wire.ACCEPTED
        else:
            server_id, status = bytes(4), bytes(4)
        body = (b"\x34\x00\x00\x0c" + status + server_id
                + b"\x3b\x00\x00\x0d" + bytes(4) + b"\x3c\x00\x00\x03\x3c")
        self._send(_header(client_id, bytes(4), _U_CONNECT, sequence, 16 + len(body)) + body, addr)

    def _data(self, data: bytes, addr: tuple[str, int]) -> None:
        if int.from_bytes(data[-2:], "big") != sum(data[:-2]) & 0xFFFF:
            self.bad_checksums += 1
            return
        client_id, server_id, sequence = data[:4], data[4:8], data[12:14]
        session = self._sessions.get(client_id)
        now = self.clock.monotonic()
        if session is None or session.server_id != server_id:
            return
        if self.session_timeout is not None and now - session.last_seen > self.session_timeout:
            del self._sessions[client_id]
            return
        session.last_seen = now
        receipt = b"\x34\x00\x00\x08\x00\x00\x00\x03"
        self._send(_header(client_id, server_id, _DATA, sequence, 16 + len(receipt)) + receipt, addr)

        length = int.from_bytes(data[18:20], "big")
        command = data[22:16 + length - 3]
        answer = self._answer(command, session)
        if answer is None:
            return
        padding = 2 - len(answer) % 2
        total = 22 + len(answer) + padding + 2
        packet = (_header(client_id, server_id, _DATA, sequence, total)
                  + b"\x36\x00" + (total - 16).to_bytes(2, "big") + b"\x00\x0a"
                  + answer + bytes([padding]) * padding)
        packet += (sum(packet) & 0xFFFF).to_bytes(2, "big")
        for _ in range(2 if self.duplicate else 1):
            self._send(packet, addr)

    def _answer(self, command: bytes, session: _Session) -> bytes | None:
        code = command[3]
        count = int.from_bytes(command[4:6], "big")
        body = command[6:]
        if code == _PING:
            self.commands.append(Command(code, ()))
            ids = self.identity
            return (wire.PONG + _u32(ids["device_number"]) + _u32(ids["device_model"]) + _u32(0)
                    + _u32(ids["slave_device_number"]) + _u32(ids["slave_device_model"])
                    + b"\x00\x22us#1:" + session.email.encode("ascii") + b":")
        if code == _DATAPOINT_READ:
            items = [(body[i], int.from_bytes(body[i + 1:i + 5], "big")) for i in range(0, 5 * count, 5)]
            self.commands.append(Command(code, tuple(items)))
            values = self._cut(_lookup(self.datapoint_registers, items))
            return (b"\x00\x00\x00\x04" if values is None
                    else count.to_bytes(2, "big") + b"".join(v.to_bytes(2, "big") for v in values))
        if code == _SETPOINT_READ:
            items = [(body[i], int.from_bytes(body[i + 1:i + 3], "big")) for i in range(0, 3 * count, 3)]
            self.commands.append(Command(code, tuple(items)))
            values = self._cut(_lookup(self.setpoint_registers, items))
            return (b"\x00\x00\x00\x04" if values is None
                    else b"\x00" + count.to_bytes(2, "big") + b"".join(v.to_bytes(2, "big") for v in values))
        if code == _SETPOINT_WRITE:
            writes = [(body[i], int.from_bytes(body[i + 1:i + 5], "big"), int.from_bytes(body[i + 5:i + 7], "big"))
                      for i in range(0, 7 * count, 7)]
            self.commands.append(Command(code, tuple(writes)))
            for obj, address, value in writes:
                if (obj, address) in self.setpoint_registers:
                    self.setpoint_registers[(obj, address)] = value
            return None
        return None

    def _cut(self, values: list[int] | None) -> list[int] | None:
        return values[:-1] if self.cut_short and values else values

    def _send(self, packet: bytes, addr: tuple[str, int]) -> None:
        if self._transport is not None:
            self._transport.sendto(packet, addr)


class _Endpoint(asyncio.DatagramProtocol):
    def __init__(self, receive: Callable[[bytes, tuple[str, int], asyncio.BaseTransport | None], None]) -> None:
        self._receive = receive
        self._transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self._receive(data, (str(addr[0]), addr[1]), self._transport)


def _header(client_id: bytes, server_id: bytes, kind: int, sequence: bytes, length: int) -> bytes:
    return client_id + server_id + bytes([kind, 0x02, 0x00, 0x01]) + sequence + length.to_bytes(2, "big")


def _email(packet: bytes) -> str | None:
    at = wire.HEADER_LENGTH
    while at + 4 <= len(packet):
        kind, length = packet[at], int.from_bytes(packet[at + 2:at + 4], "big")
        if length < 4:
            return None
        if kind == _CP_ID:
            return packet[at + 5:at + length].decode("ascii", "replace")
        at += length
    return None


def _lookup(image: Mapping[tuple[int, int], int], items: list[tuple[int, int]]) -> list[int] | None:
    if any(item not in image for item in items):
        return None
    return [image[item] for item in items]


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "big")
