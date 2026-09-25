"""A value together with how far it can be trusted and when the device said it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

Value = float | int | str | bool | None
"""What a point holds once decoded."""


class Quality(Enum):
    GOOD = auto()
    """The device answered with a valid value."""
    NO_DATA = auto()
    """The answer means "no reading": a sentinel, an out-of-range value, or an unnamed enum.

    Show as unknown.
    """
    OFFLINE = auto()
    """The register exists, but what is behind it is not answering. Expected to come back."""
    STALE = auto()
    """The last attempt failed - timeout, busy, another error. The value is the last good one."""
    MISSING = auto()
    """This unit does not have the register (0x02). Permanent until a rescan."""


@dataclass(frozen=True)
class DataValue:
    value: Value
    quality: Quality
    timestamp: datetime
    """When the device answered - timezone-aware UTC. For STALE, when it last answered."""

    @property
    def is_good(self) -> bool:
        return self.quality is Quality.GOOD
