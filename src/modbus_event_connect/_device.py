"""The contract between the core and a device, whatever protocol reaches it."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Protocol, runtime_checkable

from ._point import Access, Point

Identity = Mapping[str, int | float | str | bool | None]
"""What a device says about itself: from a protocol handshake, or read from identity points."""


class Outcome(Enum):
    """What happened to one request, in terms the core acts on."""
    OK = auto()
    MISSING = auto()
    """The device does not have this address (Modbus 0x02). Recorded as unavailable."""
    UNSUPPORTED = auto()
    """The device does not implement this kind of request at all (Modbus 0x01)."""
    OFFLINE = auto()
    """The address exists, but what is behind it does not respond (Modbus 0x04).

    The device itself is reachable.
    """
    BUSY = auto()
    """Still busy after the protocol's own retries (Modbus 0x06)."""
    NO_ANSWER = auto()
    """No answer at all: timeout, connection down, or gateway relay failure.

    The device itself is unreachable.
    """
    ERROR = auto()
    """Anything else - another exception code, a malformed reply."""


@dataclass(frozen=True)
class ReadResult:
    """The raw answer for one point."""
    outcome: Outcome
    registers: tuple[int, ...] = ()
    """For OK: the registers the point spans, or one 0/1 bit; empty otherwise."""
    exception_code: int = 0
    """The protocol's own code, for logs and diagnostics. The core never branches on it."""
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.outcome is Outcome.OK) != bool(self.registers):
            raise ValueError("registers are present exactly when the outcome is OK")


@dataclass(frozen=True)
class WriteResult:
    outcome: Outcome
    exception_code: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@dataclass(frozen=True)
class EncodedWrite:
    """What to write, as the data type encodes it.

    Whole registers, or for a BIT data type one bit to set or clear.
    """
    registers: tuple[int, ...] = ()
    bit_index: int | None = None
    bit_value: bool = False

    def __post_init__(self) -> None:
        if (self.bit_index is None) == (not self.registers):
            raise ValueError("an EncodedWrite holds either registers or one bit, not both or neither")
        if self.bit_index is not None and not 0 <= self.bit_index <= 15:
            raise ValueError(f"bit_index must be 0-15, got {self.bit_index}")
        if any(not 0 <= r <= 0xFFFF for r in self.registers):
            raise ValueError(f"registers must be 16-bit values, got {self.registers}")


class ProtocolOptions:
    """What a model states in its protocol's terms.

    The base class states nothing; a protocol supplies its own subclass.
    """

    def problems(self, point: Point[Any]) -> list[str]:
        """What is wrong with `point` under these options. Empty when nothing is."""
        return []

    def address(self, access: Access) -> int:
        """The address sent for `access`. Raises ValueError if it has none."""
        return access.address


@runtime_checkable
class Device(Protocol):
    """One device, reached through one protocol. The core talks to this and nothing below it."""

    async def connect(self) -> Identity | None:
        """Reach the device, opening the connection if this device owns it.

        Returns:
            The handshake result, or None if the device cannot be reached.
        """
        ...

    async def disconnect(self) -> None:
        """Let go of the device. Closes the connection only if this device opened it."""
        ...

    def configure(self, options: ProtocolOptions) -> None:
        """Apply the chosen model's options. Called once the model is known, before any read."""
        ...

    async def read(self, points: Sequence[Point[Any]]) -> Mapping[str, ReadResult]:
        """Read the read side of each point. Every key gets an entry; raises for nothing the device can do."""
        ...

    async def write(self, point: Point[Any], value: EncodedWrite) -> WriteResult:
        """Write to the write side of `point`, reporting the outcome rather than raising."""
        ...

    def diagnostics(self) -> Mapping[str, object]:
        """Counters for a bug report: requests, failures by outcome, latency. No addresses."""
        ...
