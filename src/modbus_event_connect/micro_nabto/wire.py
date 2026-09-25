"""The micro_nabto wire format: requests built and replies parsed, without any IO."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..device import Identity

DEVICE_PORT = 5570
BROADCAST = ("255.255.255.255", DEVICE_PORT)

HEADER_LENGTH = 16
DISCOVERY_REPLY = b"\x00\x80\x00\x01"
CRYPT_PAYLOAD = 0x36
ACCEPTED = b"\x00\x00\x00\x01"
PONG = b"pong"
_DEVICE_ID_AT = 19

_U_CONNECT, _DATA = 0x83, 0x16
_IPX, _CP_ID = 0x35, 0x3F
_PING, _DATAPOINT_READ, _SETPOINT_READ, _SETPOINT_WRITE = 0x11, 0x2D, 0x2A, 0x2B
_NO_SERVER_ID = bytes(4)


@dataclass(frozen=True)
class ConnectReply:
    sequence: int
    accepted: bool
    server_id: bytes


@dataclass(frozen=True)
class DataReply:
    sequence: int
    payload: bytes
    """The command's answer, without padding or checksum."""


# ================================================================================ requests


def discovery_request(device_id: str | None = None) -> bytes:
    """A discovery broadcast; only the device with `device_id` answers, or every device if None."""
    return b"\x00\x00\x00\x01" + bytes(8) + (device_id or "*").encode("ascii") + b"\x00"


def connect_request(client_id: bytes, sequence: int, email: str) -> bytes:
    ipx = bytes([_IPX, 0x00]) + (17).to_bytes(2, "big") + bytes(12) + b"\xa0"
    cp_id = bytes([_CP_ID, 0x00]) + (5 + len(email)).to_bytes(2, "big") + b"\x01" + email.encode("ascii")
    return _packet(client_id, _NO_SERVER_ID, _U_CONNECT, sequence, ipx + cp_id, checksum=False)


def data_request(client_id: bytes, server_id: bytes, sequence: int, command: bytes) -> bytes:
    crypt = (bytes([CRYPT_PAYLOAD, 0x00]) + (6 + len(command) + 3).to_bytes(2, "big")
             + b"\x00\x0a" + command + b"\x02")
    return _packet(client_id, server_id, _DATA, sequence, crypt, checksum=True)


def ping() -> bytes:
    return _command(_PING) + b"ping"


def datapoint_read(items: Sequence[tuple[int, int]]) -> bytes:
    """Reads (object, address) datapoints."""
    return _list(_DATAPOINT_READ, [bytes([obj]) + address.to_bytes(4, "big") for obj, address in items])


def setpoint_read(items: Sequence[tuple[int, int]]) -> bytes:
    """Reads (object, address) setpoints."""
    return _list(_SETPOINT_READ, [bytes([obj]) + address.to_bytes(2, "big") for obj, address in items])


def setpoint_write(items: Sequence[tuple[int, int, int]]) -> bytes:
    """Writes (object, address, register) setpoints."""
    return _list(_SETPOINT_WRITE, [bytes([obj]) + address.to_bytes(4, "big") + value.to_bytes(2, "big")
                                   for obj, address, value in items])


def _packet(client_id: bytes, server_id: bytes, kind: int, sequence: int, payload: bytes, *,
            checksum: bool) -> bytes:
    length = HEADER_LENGTH + len(payload) + (2 if checksum else 0)
    packet = (client_id + server_id + bytes([kind, 0x02, 0x00, 0x00])
              + sequence.to_bytes(2, "big") + length.to_bytes(2, "big") + payload)
    return packet + (sum(packet) & 0xFFFF).to_bytes(2, "big") if checksum else packet


def _command(code: int) -> bytes:
    return b"\x00\x00\x00" + bytes([code])


def _list(code: int, items: Sequence[bytes]) -> bytes:
    return _command(code) + len(items).to_bytes(2, "big") + b"".join(items) + b"\x01"


# ================================================================================= replies


def discovery_reply(datagram: bytes) -> str | None:
    """The device id a discovery reply carries, or None if `datagram` is not one."""
    if not datagram.startswith(DISCOVERY_REPLY) or len(datagram) <= _DEVICE_ID_AT:
        return None
    raw = datagram[_DEVICE_ID_AT:].split(b"\x00", 1)[0]
    try:
        device_id = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return device_id or None


def reply(datagram: bytes, client_id: bytes) -> ConnectReply | DataReply | None:
    """The reply `datagram` carries for `client_id`, or None if it carries none for it.

    A DATA packet without a crypt payload carries no answer; a Nilan CTS 402 sends one ahead of
    every answer. The checksum (a 16-bit sum of every byte before it) and the padding (to an
    even length, each pad byte holding the pad's length) are uNabto's own; a packet that breaks
    them was damaged on the way, and is dropped.
    """
    if len(datagram) < HEADER_LENGTH or datagram[:4] != client_id:
        return None
    kind = datagram[8]
    sequence = int.from_bytes(datagram[12:14], "big")
    if kind == _U_CONNECT:
        if len(datagram) < 28:
            return None
        return ConnectReply(sequence, datagram[20:24] == ACCEPTED, datagram[24:28])
    if kind == _DATA and len(datagram) > 24 and datagram[16] == CRYPT_PAYLOAD:
        if (int.from_bytes(datagram[18:20], "big") != len(datagram) - HEADER_LENGTH
                or int.from_bytes(datagram[-2:], "big") != sum(datagram[:-2]) & 0xFFFF):
            return None
        padded = datagram[22:-2]
        padding = padded[-1]
        if not 0 < padding <= len(padded) or padded[-padding:] != bytes([padding]) * padding:
            return None
        return DataReply(sequence, padded[:-padding])
    return None


def identity(payload: bytes) -> Identity | None:
    """The identity in a ping answer, or None if `payload` is not one."""
    if len(payload) < 24 or not payload.startswith(PONG):
        return None
    return {
        "device_number": int.from_bytes(payload[4:8], "big"),
        "device_model": int.from_bytes(payload[8:12], "big"),
        "slave_device_number": int.from_bytes(payload[16:20], "big"),
        "slave_device_model": int.from_bytes(payload[20:24], "big"),
    }


def datapoint_values(payload: bytes) -> list[int] | None:
    """The registers in a datapoint answer: none if the read was refused, None if malformed."""
    return _values(payload, 0)


def setpoint_values(payload: bytes) -> list[int] | None:
    """The registers in a setpoint answer: none if the read was refused, None if malformed."""
    return _values(payload, 1)


def _values(payload: bytes, count_at: int) -> list[int] | None:
    start = count_at + 2
    if len(payload) < start:
        return None
    count = int.from_bytes(payload[count_at:start], "big")
    if count == 0:
        return []
    if len(payload) != start + 2 * count:
        return None
    return [int.from_bytes(payload[i:i + 2], "big") for i in range(start, len(payload), 2)]
