"""A clock for tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class FakeClock:
    """A clock that moves only when told to, so scheduling can be tested without waiting."""

    def __init__(self, monotonic: float = 1000.0,
                 now: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)) -> None:
        if now.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start time")
        self._monotonic = monotonic
        self._now = now

    def monotonic(self) -> float:
        return self._monotonic

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock cannot be advanced backwards; use jump_wall() for that")
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)

    def jump_wall(self, seconds: float) -> None:
        """Move only the wall clock, forwards or backwards."""
        self._now += timedelta(seconds=seconds)
