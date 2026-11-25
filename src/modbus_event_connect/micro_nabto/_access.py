"""micro_nabto address spaces: datapoints (read-only) and setpoints (read and write)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Hashable

from .._point import Access


@dataclass(frozen=True)
class DatapointRegister(Access):
    """A read-only value, read with the datapoint request."""
    obj: int = 0
    table_name: ClassVar[str] = "datapoint"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.obj < 0:
            raise ValueError(f"an object number cannot be negative, got {self.obj}")

    @property
    def space(self) -> Hashable:
        return (type(self), self.obj)

    @property
    def table(self) -> str:
        """The space by name, with the object after a colon where it is not 0: "datapoint:1"."""
        return self.table_name if self.obj == 0 else f"{self.table_name}:{self.obj}"


@dataclass(frozen=True)
class SetpointRegister(Access):
    """A setting, read with the setpoint request and written with the setpoint write."""
    obj: int = 0
    writable: ClassVar[bool] = True
    table_name: ClassVar[str] = "setpoint"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.obj < 0:
            raise ValueError(f"an object number cannot be negative, got {self.obj}")

    @property
    def space(self) -> Hashable:
        return (type(self), self.obj)

    @property
    def table(self) -> str:
        """The space by name, with the object after a colon where it is not 0: "datapoint:1"."""
        return self.table_name if self.obj == 0 else f"{self.table_name}:{self.obj}"
