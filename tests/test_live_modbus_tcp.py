"""Read-only tests of the Modbus TCP transport against a real device.

    Configure it either way:
        set MODBUS_TCP_HOST=<device-ip>          (environment variable)
        MODBUS_TCP_HOST = "<device-ip>"          (in mysecrets.py, which is gitignored)

    Optional:
        MODBUS_TCP_PORT      default 502
        MODBUS_TCP_UNIT_ID   default 1

    Run:
        pytest tests/test_live_modbus_tcp.py -v -s

NOTHING HERE WRITES TO THE DEVICE: the model has no write side, the client is read-only, and
any request that is not a read fails the test before it is sent.

The addresses are a Wavin Sentio's. On another device, expect MISSING where it has none.
"""
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import pytest
import pytest_asyncio

from conftest import live_or_skip, live_setting
from modbus_event_connect._client import Client
from modbus_event_connect._data_type import DataType
from modbus_event_connect._device import Outcome
from modbus_event_connect._key import Key
from modbus_event_connect._model import Model, Section
from modbus_event_connect._point import Point
from modbus_event_connect._value import Quality
from modbus_event_connect.modbus._access import (
    HoldingRegister,
    InputRegister,
    ModbusOptions,
    plain,
)
from modbus_event_connect.modbus._connection import ModbusTcpConnection, Request, Response
from modbus_event_connect.modbus._device import ModbusDevice

HOST = live_setting("MODBUS_TCP_HOST")
PORT = int(live_setting("MODBUS_TCP_PORT") or "502")
UNIT_ID = int(live_setting("MODBUS_TCP_UNIT_ID") or "1")

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    *live_or_skip("Modbus TCP", MODBUS_TCP_HOST=HOST),
]

LIVE_MODEL = Model(name="live", manufacturer="any", sections=[Section([
    Point(Key("u16", int), read=InputRegister(1), data_type=DataType.UINT16),
    Point(Key("s16_scaled", float), read=InputRegister(104), data_type=DataType.INT16, scale=0.01, no_data=(0x7FFF,)),
    Point(Key("text", str), read=HoldingRegister(10), data_type=DataType.string(16)),
    Point(Key("u32", int), read=HoldingRegister(28), data_type=DataType.UINT32),
])], options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=1.0)


@dataclass
class Live:
    client: Client
    device: ModbusDevice


@pytest_asyncio.fixture(scope="module", loop_scope="module")  # pyright: ignore[reportUntypedFunctionDecorator, reportUnknownMemberType]
async def live() -> AsyncGenerator[Live, None]:
    assert HOST is not None   # guarded by the skip above
    connection = ModbusTcpConnection(HOST, PORT)
    read = connection.request

    async def only_reads(request: Request) -> Response:
        if not request.function.is_read:
            raise AssertionError("a write was attempted; these tests are read-only")
        return await read(request)
    setattr(connection, "request", only_reads)
    device = ModbusDevice(connection, UNIT_ID, owns_connection=True)
    client = Client(device, LIVE_MODEL, read_only=True)
    try:
        await client.connect()
    except Exception as err:
        pytest.fail(f"Could not bring up the device (unit id {UNIT_ID}): {err!r}\n"
                    f"  - Is Modbus TCP enabled on the device?\n"
                    f"  - Some devices answer on unit id 255: set MODBUS_TCP_UNIT_ID=255")
    yield Live(client, device)
    await client.disconnect()


def _good[V](client: Client, key: Key[V]) -> V | None:
    current = client.value(key)
    assert current is not None and current.quality is Quality.GOOD, f"{key}: {current}"
    print(f"  {key}: {current.value!r}")
    return current.value


async def test_nothing_here_can_be_written(live: Live) -> None:
    assert not any(live.client.can_write(k) for k in live.client.points)


async def test_a_register_reads_as_an_integer(live: Live) -> None:
    assert isinstance(_good(live.client, Key("u16", int)), int)


async def test_a_signed_scaled_register_reads_as_a_float(live: Live) -> None:
    assert isinstance(_good(live.client, Key("s16_scaled", float)), float)


async def test_sixteen_registers_read_as_text(live: Live) -> None:
    assert isinstance(_good(live.client, Key("text", str)), str)


async def test_two_registers_read_as_one_32_bit_number(live: Live) -> None:
    value = _good(live.client, Key("u32", int))
    assert isinstance(value, int) and value > 0xFFFF


async def test_an_address_the_device_lacks_is_missing_and_the_rest_are_still_read(live: Live) -> None:
    answers = await live.device.read([Point(Key("a", int), read=InputRegister(1), data_type=DataType.UINT16),
                                      Point(Key("absent", int), read=InputRegister(9999), data_type=DataType.UINT16),
                                      Point(Key("b", int), read=InputRegister(2), data_type=DataType.UINT16)])
    print(f"\n  {({k: a.outcome.name for k, a in answers.items()})}")
    assert (answers["a"].outcome, answers["b"].outcome) == (Outcome.OK, Outcome.OK)
    assert answers["absent"].outcome is Outcome.MISSING
