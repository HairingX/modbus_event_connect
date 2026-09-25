"""micro_nabto address spaces: datapoints (read-only) and setpoints (read and write)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Hashable

from ..point import Access


@dataclass(frozen=True)
class DatapointRegister(Access):
    """A read-only value, read with the datapoint request."""
    obj: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.obj < 0:
            raise ValueError(f"an object number cannot be negative, got {self.obj}")

    @property
    def space(self) -> Hashable:
        return (type(self), self.obj)


@dataclass(frozen=True)
class SetpointRegister(Access):
    """A setting, read with the setpoint request and written with the setpoint write."""
    obj: int = 0
    writable: ClassVar[bool] = True

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.obj < 0:
            raise ValueError(f"an object number cannot be negative, got {self.obj}")

    @property
    def space(self) -> Hashable:
        return (type(self), self.obj)
