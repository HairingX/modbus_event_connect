"""The Modbus link: `ModbusTcpConnection` against a fake pymodbus client, so nothing here touches
a network."""
import asyncio
from collections.abc import Callable
from typing import Any

import pymodbus.client
import pytest

from src.modbus_event_connect.modbus.connection import (
    ExceptionCode,
    FunctionCode,
    ModbusConnection,
    ModbusTcpConnection,
    Request,
    Response,
)
from src.modbus_event_connect.testing.clock import FakeClock

HOST = "modbus.invalid"


# ================================================================================ doubles


class FakeReply:
    """What pymodbus returns: registers or bits, or an exception response."""

    def __init__(self, registers: list[int] | None = None, bits: list[bool] | None = None,
                 exception_code: int = 0) -> None:
        self.registers = registers or []
        self.bits = bits or []
        self.exception_code = exception_code

    def isError(self) -> bool:
        return self.exception_code != 0


Answer = FakeReply | BaseException | None
Responder = Callable[[str, int, dict[str, Any]], Answer]


class FakeClient:
    """Stands in for pymodbus's AsyncModbusTcpClient; the unit goes in `device_id`."""

    def __init__(self, responder: Responder | None = None, *, connect_result: bool = True,
                 hang: bool = False) -> None:
        self.responder: Responder = responder or (lambda name, address, kw: FakeReply(registers=[0] * kw.get("count", 1)))
        self.connect_result = connect_result
        self.hang = hang
        self.connected = False
        self.connects = 0
        self.closes = 0
        self.calls: list[tuple[str, int, dict[str, Any]]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def connect(self) -> bool:
        self.connects += 1
        self.connected = self.connect_result
        return self.connect_result

    def close(self) -> None:
        self.closes += 1
        self.connected = False

    async def _handle(self, name: str, address: int, **kwargs: Any) -> Any:
        self.calls.append((name, address, kwargs))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)                      # let concurrent callers interleave if they can
            if self.hang:
                await asyncio.Event().wait()
            answer = self.responder(name, address, kwargs)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        finally:
            self.in_flight -= 1

    async def read_coils(self, address: int, *, count: int = 1, device_id: int = 1) -> Any:
        return await self._handle("read_coils", address, count=count, device_id=device_id)

    async def read_discrete_inputs(self, address: int, *, count: int = 1, device_id: int = 1) -> Any:
        return await self._handle("read_discrete_inputs", address, count=count, device_id=device_id)

    async def read_holding_registers(self, address: int, *, count: int = 1, device_id: int = 1) -> Any:
        return await self._handle("read_holding_registers", address, count=count, device_id=device_id)

    async def read_input_registers(self, address: int, *, count: int = 1, device_id: int = 1) -> Any:
        return await self._handle("read_input_registers", address, count=count, device_id=device_id)

    async def write_coil(self, address: int, value: bool, *, device_id: int = 1) -> Any:
        return await self._handle("write_coil", address, value=value, device_id=device_id)

    async def write_register(self, address: int, value: int, *, device_id: int = 1) -> Any:
        return await self._handle("write_register", address, value=value, device_id=device_id)

    async def write_coils(self, address: int, values: list[bool], *, device_id: int = 1) -> Any:
        return await self._handle("write_coils", address, values=values, device_id=device_id)

    async def write_registers(self, address: int, values: list[int], *, device_id: int = 1) -> Any:
        return await self._handle("write_registers", address, values=values, device_id=device_id)

    async def mask_write_register(self, *, address: int = 0, and_mask: int = 0xFFFF, or_mask: int = 0,
                                  device_id: int = 1) -> Any:
        return await self._handle("mask_write_register", address, and_mask=and_mask, or_mask=or_mask,
                                  device_id=device_id)


class OldFakeClient(FakeClient):
    """An older pymodbus, where the unit went in `slave`."""

    async def read_holding_registers(self, address: int, *, count: int = 1, slave: int = 1) -> Any:  # type: ignore[override]
        return await self._handle("read_holding_registers", address, count=count, slave=slave)

    async def write_register(self, address: int, value: int, *, slave: int = 1) -> Any:  # type: ignore[override]
        return await self._handle("write_register", address, value=value, slave=slave)


class NoUnitClient(FakeClient):
    async def read_holding_registers(self, address: int, *, count: int = 1) -> Any:  # type: ignore[override]
        return await self._handle("read_holding_registers", address, count=count)


def holding(unit: int, address: int = 0, count: int = 1) -> Request:
    return Request(unit, FunctionCode.READ_HOLDING_REGISTERS, address, count=count)


# ================================================================================ the unit


async def test_the_unit_travels_with_every_request_as_device_id() -> None:
    client = FakeClient()
    conn = ModbusTcpConnection(HOST, client=client)
    await conn.request(holding(3, 10))
    await conn.request(holding(7, 10))
    assert [(c[0], c[2]["device_id"]) for c in client.calls] == [
        ("read_holding_registers", 3), ("read_holding_registers", 7)]


async def test_an_older_pymodbus_gets_the_unit_as_slave() -> None:
    client = OldFakeClient()
    conn = ModbusTcpConnection(HOST, client=client)
    await conn.request(holding(5))
    await conn.request(Request(9, FunctionCode.WRITE_SINGLE_REGISTER, 4, values=(1,)))
    assert [c[2]["slave"] for c in client.calls] == [5, 9]
    assert all("device_id" not in c[2] for c in client.calls)


async def test_a_pymodbus_that_takes_no_unit_is_refused_rather_than_guessed() -> None:
    conn = ModbusTcpConnection(HOST, client=NoUnitClient())
    with pytest.raises(TypeError):
        await conn.request(holding(1))


# ================================================================== function code mapping


@pytest.mark.parametrize("request_, call, kwargs", [
    (Request(1, FunctionCode.WRITE_SINGLE_COIL, 5, values=(1,)), "write_coil", {"value": True}),
    (Request(1, FunctionCode.WRITE_SINGLE_COIL, 5, values=(0,)), "write_coil", {"value": False}),
    (Request(1, FunctionCode.WRITE_SINGLE_REGISTER, 5, values=(513,)), "write_register", {"value": 513}),
    (Request(1, FunctionCode.WRITE_MULTIPLE_COILS, 5, values=(1, 0, 1)), "write_coils",
     {"values": [True, False, True]}),
    (Request(1, FunctionCode.WRITE_MULTIPLE_REGISTERS, 5, count=2, values=(1, 2)), "write_registers",
     {"values": [1, 2]}),
    (Request(1, FunctionCode.MASK_WRITE_REGISTER, 5, and_mask=0xFFF7, or_mask=0x0008), "mask_write_register",
     {"and_mask": 0xFFF7, "or_mask": 0x0008}),
])
async def test_every_write_reaches_its_pymodbus_method(request_: Request, call: str, kwargs: dict[str, Any]) -> None:
    client = FakeClient(lambda name, address, kw: FakeReply())
    response = await ModbusTcpConnection(HOST, client=client).request(request_)
    assert response == Response(ok=True)
    name, address, sent = client.calls[0]
    assert (name, address) == (call, 5)
    assert {k: sent[k] for k in kwargs} == kwargs


@pytest.mark.parametrize("function, call", [
    (FunctionCode.READ_HOLDING_REGISTERS, "read_holding_registers"),
    (FunctionCode.READ_INPUT_REGISTERS, "read_input_registers"),
])
async def test_register_reads_return_the_registers(function: FunctionCode, call: str) -> None:
    client = FakeClient(lambda name, address, kw: FakeReply(registers=[11, 22, 33]))
    response = await ModbusTcpConnection(HOST, client=client).request(Request(1, function, 100, count=3))
    assert response == Response(ok=True, registers=(11, 22, 33))
    assert client.calls[0][:2] == (call, 100) and client.calls[0][2]["count"] == 3


@pytest.mark.parametrize("function, call", [
    (FunctionCode.READ_COILS, "read_coils"),
    (FunctionCode.READ_DISCRETE_INPUTS, "read_discrete_inputs"),
])
async def test_bit_reads_are_zero_or_one_and_cut_to_the_count(function: FunctionCode, call: str) -> None:
    # pymodbus pads bits to a whole byte: eight come back for three asked.
    client = FakeClient(lambda name, address, kw: FakeReply(bits=[True, False, True, True, True, True, True, True]))
    response = await ModbusTcpConnection(HOST, client=client).request(Request(1, function, 0, count=3))
    assert response.registers == (1, 0, 1)
    assert client.calls[0][0] == call


async def test_a_short_reply_is_an_error_but_not_a_missing_answer() -> None:
    client = FakeClient(lambda name, address, kw: FakeReply(registers=[1]))
    response = await ModbusTcpConnection(HOST, client=client).request(holding(1, count=3))
    assert not response.ok and not response.no_answer and response.exception_code == 0


# ================================================================================ failures


@pytest.mark.parametrize("code", [0x01, 0x02, 0x04, 0x06, 0x0B])
async def test_an_exception_response_carries_its_code(code: int) -> None:
    client = FakeClient(lambda name, address, kw: FakeReply(exception_code=code))
    response = await ModbusTcpConnection(HOST, client=client).request(holding(1))
    assert (response.ok, response.exception_code, response.no_answer) == (False, code, False)


async def test_a_raised_exception_is_no_answer_and_does_not_leak_the_host() -> None:
    client = FakeClient(lambda name, address, kw: ConnectionError(f"Connection to {HOST}:502 lost"))
    response = await ModbusTcpConnection(HOST, client=client).request(holding(1))
    assert response.no_answer and not response.ok
    assert HOST not in response.detail


async def test_a_timeout_raised_by_pymodbus_is_no_answer() -> None:
    client = FakeClient(lambda name, address, kw: asyncio.TimeoutError())
    response = await ModbusTcpConnection(HOST, client=client).request(holding(1))
    assert response.no_answer


async def test_a_client_that_never_answers_is_given_up_on_and_the_link_dropped() -> None:
    # A reply arriving after we gave up must not be read as the answer to the next request.
    client = FakeClient(hang=True)
    client.connected = True
    response = await ModbusTcpConnection(HOST, client=client, timeout=0.01).request(holding(1))
    assert response.no_answer
    assert client.closes == 1


async def test_a_none_reply_is_no_answer() -> None:
    client = FakeClient(lambda name, address, kw: None)
    assert (await ModbusTcpConnection(HOST, client=client).request(holding(1))).no_answer


# ============================================================================ serialisation


async def test_concurrent_callers_are_sent_one_at_a_time() -> None:
    client = FakeClient()
    conn = ModbusTcpConnection(HOST, client=client)
    responses = await asyncio.gather(*(conn.request(holding(unit)) for unit in range(1, 9)))
    assert all(r.ok for r in responses)
    assert len(client.calls) == 8
    assert client.max_in_flight == 1


async def test_the_frame_gap_is_kept_after_each_answer() -> None:
    clock = FakeClock()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    conn = ModbusTcpConnection(HOST, client=FakeClient(), frame_gap=0.05, clock=clock, sleep=sleep)
    await conn.request(holding(1))
    assert waits == []                            # nothing before the first request
    await conn.request(holding(1))
    assert len(waits) == 1 and abs(waits[0] - 0.05) < 1e-9
    clock.advance(0.03)                           # part of the gap passed on its own
    await conn.request(holding(1))
    assert len(waits) == 2 and abs(waits[1] - 0.02) < 1e-9
    clock.advance(1.0)                            # all of it passed
    await conn.request(holding(1))
    assert len(waits) == 2


async def test_no_frame_gap_means_no_waiting() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    conn = ModbusTcpConnection(HOST, client=FakeClient(), clock=FakeClock(), sleep=sleep)
    for _ in range(3):
        await conn.request(holding(1))
    assert waits == []


# ======================================================================= opening, lazily


async def test_a_request_on_a_closed_link_opens_it_first() -> None:
    client = FakeClient()
    conn = ModbusTcpConnection(HOST, client=client)
    assert not conn.connected
    assert (await conn.request(holding(1))).ok
    assert client.connects == 1 and conn.connected
    await conn.request(holding(1))
    assert client.connects == 1                   # open stays open


async def test_a_link_that_will_not_open_is_no_answer_without_sending() -> None:
    client = FakeClient(connect_result=False)
    response = await ModbusTcpConnection(HOST, client=client).request(holding(1))
    assert response.no_answer
    assert client.connects == 1                   # tried once
    assert client.calls == []


async def test_open_and_close() -> None:
    client = FakeClient()
    conn = ModbusTcpConnection(HOST, client=client)
    assert await conn.open() and conn.connected
    assert await conn.open() and client.connects == 1
    await conn.close()
    assert not conn.connected and client.closes == 1


async def test_open_reports_a_failure_instead_of_raising() -> None:
    client = FakeClient()

    async def refuse() -> bool:
        raise OSError(f"cannot reach {HOST}")

    client.connect = refuse  # type: ignore[method-assign]
    assert await ModbusTcpConnection(HOST, client=client).open() is False


def test_nothing_is_created_by_the_constructor_so_it_works_outside_a_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Any] = []

    def factory(*args: Any, **kwargs: Any) -> None:
        made.append((args, kwargs))

    monkeypatch.setattr(pymodbus.client, "AsyncModbusTcpClient", factory)
    conn = ModbusTcpConnection(HOST, 1502, timeout=2.5)
    assert conn.client is None and made == [] and not conn.connected


async def test_the_client_is_created_on_first_use_with_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def factory(*args: Any, **kwargs: Any) -> FakeClient:
        made.append((args, kwargs))
        return FakeClient()

    monkeypatch.setattr(pymodbus.client, "AsyncModbusTcpClient", factory)
    conn = ModbusTcpConnection(HOST, 1502, timeout=2.5)
    await conn.close()                            # closing before first use creates nothing
    assert made == []
    assert (await conn.request(holding(1))).ok
    assert made == [((HOST,), {"port": 1502, "timeout": 2.5, "retries": 0})]
    await conn.request(holding(1))
    assert len(made) == 1


# ================================================================================ contract


def test_pymodbus_connection_is_a_modbus_connection() -> None:
    assert isinstance(ModbusTcpConnection(HOST), ModbusConnection)


def test_requests_are_checked_when_made() -> None:
    with pytest.raises(ValueError):
        Request(256, FunctionCode.READ_COILS, 0, count=1)
    with pytest.raises(ValueError):
        Request(1, FunctionCode.READ_COILS, 0x10000, count=1)
    with pytest.raises(ValueError):
        Request(1, FunctionCode.READ_HOLDING_REGISTERS, 0)          # a read of nothing
    with pytest.raises(ValueError):
        Request(1, FunctionCode.WRITE_SINGLE_REGISTER, 0, values=(0x10000,))


def test_the_codes_are_the_modbus_ones() -> None:
    assert FunctionCode.MASK_WRITE_REGISTER == 0x16 and FunctionCode.WRITE_MULTIPLE_COILS == 0x0F
    assert ExceptionCode.GATEWAY_TARGET_FAILED == 0x0B and ExceptionCode.SERVER_DEVICE_BUSY == 0x06
