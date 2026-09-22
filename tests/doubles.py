"""Test doubles shared by the test modules.

Two transports, for two different questions:

- `RecordingTransport` answers "what was asked?" - it records calls and fails on demand.
- `SimulatedDevice` answers "what would a real device have said?" - it serves a register image
  and refuses requests the way real units do.

Both are annotated against `ModbusTransport`, which means a signature that drifts away from the
protocol is caught by the type checker instead of only by an `isinstance` call - a
`runtime_checkable` Protocol answers that from method names alone.
"""
from collections.abc import Mapping
from typing import Dict, List, Sequence

import asyncio

from src.modbus_event_connect import ModbusDeviceInfo, RegisterTable, VersionInfo
from src.modbus_event_connect.modbus_tcp.transport import (
    EXCEPTION_ILLEGAL_DATA_ADDRESS,
    EXCEPTION_NONE,
    EXCEPTION_SLAVE_DEVICE_BUSY,
)

TransportCall = tuple[str, int, int | bool]
"""What was asked of the transport: the kind of call, the address, and a count or a value."""


class RecordingTransport:
    """
    Records which reader was called, against which address space, and can fail on demand.

    :param fail: every read returns None and every write returns False.
    :param exception: the Modbus exception code reported while failing.
    :param delay: awaited inside each read, to model a slow device.
    """

    def __init__(self, *, fail: bool = True,
                 exception: int = EXCEPTION_ILLEGAL_DATA_ADDRESS,
                 delay: float = 0.0) -> None:
        self.calls: List[TransportCall] = []
        self.fail = fail
        self.delay = delay
        self._exception = exception if fail else EXCEPTION_NONE

    @property
    def is_open(self) -> bool: return True
    @property
    def last_exception_code(self) -> int: return self._exception
    @property
    def last_error_text(self) -> str | None: return "recorded failure" if self.fail else None

    async def open(self) -> bool: return True
    async def close(self) -> None: return None

    @property
    def kinds(self) -> set[str]:
        """The distinct kinds of call recorded, which is what most tests assert on."""
        return {call[0] for call in self.calls}

    async def _record(self, kind: str, address: int, count: int) -> bool:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append((kind, address, count))
        return not self.fail

    async def read_input_registers(self, address: int, count: int) -> List[int] | None:
        return [0] * count if await self._record("input", address, count) else None

    async def read_holding_registers(self, address: int, count: int) -> List[int] | None:
        return [0] * count if await self._record("holding", address, count) else None

    async def read_discrete_inputs(self, address: int, count: int) -> List[bool] | None:
        return [False] * count if await self._record("discrete", address, count) else None

    async def read_coils(self, address: int, count: int) -> List[bool] | None:
        return [False] * count if await self._record("coils", address, count) else None

    async def write_coil(self, address: int, value: bool) -> bool:
        self.calls.append(("write_coil", address, value))
        return not self.fail

    async def write_register(self, address: int, value: int) -> bool:
        self.calls.append(("write_register", address, value))
        return not self.fail

    async def write_registers(self, address: int, values: Sequence[int]) -> bool:
        self.calls.append(("write_registers", address, len(values)))
        return not self.fail


WireCall = tuple[int, int, int | bool]
"""What reached the wire: the Modbus function code, the address, and a count or a value."""

READ_FUNCTION_CODE: Dict[RegisterTable, int] = {
    RegisterTable.COIL: 0x01,
    RegisterTable.DISCRETE: 0x02,
    RegisterTable.HOLDING: 0x03,
    RegisterTable.INPUT: 0x04,
}
"""The function code a read of each table must use."""

NO_ANSWER = 0
"""A fault code meaning the device never answered - how a timeout reaches the client."""


class SimulatedDevice:
    """
    A device with a register image, answering the way a real unit does.

    - Values come from the image, one dict per register table. Bits are stored as 0 / 1.
    - A request touching any address the image lacks is refused as a whole with 0x02, which is
      what real devices do and why the client has to isolate absent registers itself.
    - `faults` plants an exception code on an address; `NO_ANSWER` there models a timeout.
    - `busy_for` answers 0x06 to that many calls first, whatever they are.
    - A write lands in the image only if it is accepted, so a test can check where it went.

    Every call is recorded with its Modbus function code in `calls`.
    """

    def __init__(self, image: Mapping[RegisterTable, Mapping[int, int]], *,
                 faults: Mapping[tuple[RegisterTable, int], int] | None = None,
                 busy_for: int = 0, delay: float = 0.0) -> None:
        self.image: Dict[RegisterTable, Dict[int, int]] = {
            table: dict(image.get(table, {})) for table in RegisterTable}
        self.faults: Dict[tuple[RegisterTable, int], int] = dict(faults or {})
        self.calls: List[WireCall] = []
        self.delay = delay
        self._busy_left = busy_for
        self._exception = EXCEPTION_NONE
        self._failed = False

    @property
    def function_codes(self) -> set[int]:
        """The distinct function codes that reached the wire."""
        return {call[0] for call in self.calls}

    @property
    def is_open(self) -> bool: return True
    @property
    def last_exception_code(self) -> int: return self._exception
    @property
    def last_error_text(self) -> str | None:
        if not self._failed: return None
        return "no answer" if self._exception == NO_ANSWER else f"exception {self._exception:#04x}"

    async def open(self) -> bool: return True
    async def close(self) -> None: return None

    async def _answer(self, call: WireCall, table: RegisterTable, address: int, count: int) -> bool:
        """Record the call and decide whether the device answers it. False means refused."""
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(call)
        refusal: int | None = None
        if self._busy_left > 0:
            self._busy_left -= 1
            refusal = EXCEPTION_SLAVE_DEVICE_BUSY
        else:
            for each in range(address, address + count):
                refusal = self.faults.get((table, each))
                if refusal is None and each not in self.image[table]:
                    refusal = EXCEPTION_ILLEGAL_DATA_ADDRESS
                if refusal is not None:
                    break
        self._failed = refusal is not None
        self._exception = EXCEPTION_NONE if refusal is None else refusal
        return refusal is None

    async def _read(self, table: RegisterTable, address: int, count: int) -> List[int] | None:
        if not await self._answer((READ_FUNCTION_CODE[table], address, count), table, address, count):
            return None
        return [self.image[table][each] for each in range(address, address + count)]

    async def _write(self, function_code: int, table: RegisterTable, address: int,
                     values: Sequence[int]) -> bool:
        recorded: int | bool = values[0] if len(values) == 1 else len(values)
        if not await self._answer((function_code, address, recorded), table, address, len(values)):
            return False
        for offset, value in enumerate(values):
            self.image[table][address + offset] = value
        return True

    async def read_input_registers(self, address: int, count: int) -> List[int] | None:
        return await self._read(RegisterTable.INPUT, address, count)

    async def read_holding_registers(self, address: int, count: int) -> List[int] | None:
        return await self._read(RegisterTable.HOLDING, address, count)

    async def read_discrete_inputs(self, address: int, count: int) -> List[bool] | None:
        bits = await self._read(RegisterTable.DISCRETE, address, count)
        return None if bits is None else [bool(bit) for bit in bits]

    async def read_coils(self, address: int, count: int) -> List[bool] | None:
        bits = await self._read(RegisterTable.COIL, address, count)
        return None if bits is None else [bool(bit) for bit in bits]

    async def write_coil(self, address: int, value: bool) -> bool:
        return await self._write(0x05, RegisterTable.COIL, address, [int(value)])

    async def write_register(self, address: int, value: int) -> bool:
        return await self._write(0x06, RegisterTable.HOLDING, address, [value])

    async def write_registers(self, address: int, values: Sequence[int]) -> bool:
        return await self._write(0x10, RegisterTable.HOLDING, address, list(values))


def device_info(device_id: str = "test") -> ModbusDeviceInfo:
    """A device description with no real network details in it."""
    return ModbusDeviceInfo(device_id=device_id, device_host="h", device_port=502,
                            version=VersionInfo(), identification=None)
