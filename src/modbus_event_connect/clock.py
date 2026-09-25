"""The two clocks the library uses, behind one object that can be replaced."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards; only differences mean anything."""
        ...

    def now(self) -> datetime:
        """The current time, timezone-aware UTC."""
        ...


class SystemClock:
    """The real clocks."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)
