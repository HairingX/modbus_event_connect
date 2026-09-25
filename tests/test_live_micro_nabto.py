"""Read-only tests of the micro_nabto transport against a real device.

    Configure it either way:
        set MICRO_NABTO_HOST=<device-ip>        (environment variables)
        set MICRO_NABTO_EMAIL=you@example.com

        MICRO_NABTO_HOST = "<device-ip>"        (or in mysecrets.py, which is gitignored)
        MICRO_NABTO_EMAIL = "you@example.com"

    Optional:
        MICRO_NABTO_PORT        default 5570
        MICRO_NABTO_DEVICE_ID   also finds the device again if its address changes

    Run:
        pytest tests/test_live_micro_nabto.py -v -s

NOTHING HERE WRITES TO THE DEVICE: the model has no write side, the client is read-only, and
the one path a write could take fails the test.

The addresses are a Nilan CTS 400's. On another device, expect MISSING where it has none.
"""
import asyncio
from collections import Counter
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import NoReturn

import pytest
import pytest_asyncio

from conftest import live_or_skip, live_setting
from src.modbus_event_connect._client import Client
from src.modbus_event_connect._data_type import DataType
from src.modbus_event_connect._device import Outcome
from src.modbus_event_connect.micro_nabto._access import DatapointRegister, SetpointRegister
from src.modbus_event_connect.micro_nabto._connection import MicroNabtoConnection, discover
from src.modbus_event_connect.micro_nabto._device import MicroNabtoDevice, MicroNabtoOptions
from src.modbus_event_connect._model import Model, Section
from src.modbus_event_connect._point import Point
from src.modbus_event_connect._value import DataValue, Quality

HOST = live_setting("MICRO_NABTO_HOST")
EMAIL = live_setting("MICRO_NABTO_EMAIL")
DEVICE_ID = live_setting("MICRO_NABTO_DEVICE_ID")
PORT = int(live_setting("MICRO_NABTO_PORT") or "5570")

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    *live_or_skip("micro_nabto", MICRO_NABTO_HOST=HOST, MICRO_NABTO_EMAIL=EMAIL),
]

DATAPOINTS = (23, 24, 25, 27, 28, 29, 30, 31, 46, 47, 48, 49, 50, 51, 52, 53, 56, 57, 58, 63, 64, 66, 70,
              72, 77, 91, 110)
SETPOINTS = (30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 43, 45, 50, 51, 57, 58, 59, 60, 61, 62, 63, 64,
             65, 66, 69, 70, 80)
TEMPERATURES = (27, 28, 29, 30)

LIVE_MODEL = Model(
    name="live", manufacturer="any",
    sections=[Section([*(Point(f"dp_{a}", read=DatapointRegister(a), data_type=DataType.INT16 if a in TEMPERATURES else DataType.UINT16,
                           scale=0.1 if a in TEMPERATURES else 1.0) for a in DATAPOINTS),
                   *(Point(f"sp_{a}", read=SetpointRegister(a), data_type=DataType.UINT16) for a in SETPOINTS)])],
    options=MicroNabtoOptions(), read_back_after=1.0)


@dataclass
class Live:
    client: Client
    device: MicroNabtoDevice
    connection: MicroNabtoConnection


def _single(address: int, key: str = "a") -> Point:
    return Point(key, read=DatapointRegister(address), data_type=DataType.UINT16)


def _count(live: Live, name: str) -> int:
    value = live.connection.diagnostics()[name]
    assert isinstance(value, int)
    return value


@pytest_asyncio.fixture(scope="module", loop_scope="module")  # pyright: ignore[reportUntypedFunctionDecorator, reportUnknownMemberType]
async def live() -> AsyncGenerator[Live, None]:
    assert HOST is not None and EMAIL is not None   # guarded by the skip above
    connection = MicroNabtoConnection(EMAIL, host=HOST, port=PORT, device_id=DEVICE_ID)

    async def refuse(command: bytes) -> NoReturn:
        raise AssertionError("a write was attempted; these tests are read-only")
    setattr(connection, "send", refuse)
    device = MicroNabtoDevice(connection, owns_connection=True)
    client = Client(device, LIVE_MODEL, read_only=True)
    try:
        await client.connect()
    except Exception as err:
        pytest.fail(f"Could not bring up the device: {err!r}\n"
                    f"  - Is the email the one paired with the device in its app?\n"
                    f"  - Are the host and port right? The device answers on UDP.")
    yield Live(client, device, connection)
    await client.disconnect()


# ================================================================================ safety

async def test_nothing_here_can_be_written(live: Live) -> None:
    assert not any(live.client.can_write(k) for k in live.client.points)
    with pytest.raises(AssertionError, match="read-only"):
        await live.connection.send(b"")


# ============================================================================ the picture

async def test_the_handshake_identifies_the_device(live: Live) -> None:
    identity = await live.device.connect()
    print(f"\n  identity: {identity}")
    assert identity is not None
    assert set(identity) == {"device_number", "device_model", "slave_device_number", "slave_device_model"}


async def test_every_value_is_good(live: Live) -> None:
    qualities = Counter(v.quality.name for k in live.client.points if (v := live.client.value(k)) is not None)
    print(f"\n  {len(live.client.points)} keys: {dict(qualities)}")
    assert qualities == Counter({Quality.GOOD.name: len(DATAPOINTS) + len(SETPOINTS)})


async def test_temperatures_decode_as_temperatures(live: Live) -> None:
    for address in TEMPERATURES:
        current = live.client.value(f"dp_{address}")
        print(f"  dp_{address}: {current.value if current else None}")
        assert current is not None and isinstance(current.value, float) and -40.0 < current.value < 70.0


async def test_every_subscriber_hears_its_value(live: Live) -> None:
    heard: dict[str, DataValue] = {}

    def listen(key: str, old: DataValue | None, new: DataValue) -> None:
        heard[key] = new
    for unsubscribe in [live.client.subscribe(k, listen) for k in live.client.points]:
        unsubscribe()
    assert set(heard) == set(live.client.points)


# ================================================================== what the device does

async def test_an_address_the_device_lacks_is_missing_and_the_rest_are_still_read(live: Live) -> None:
    answers = await live.device.read([_single(23, "a"), _single(9999, "absent"), _single(24, "b")])
    assert [answers[k].outcome for k in ("a", "absent", "b")] == [Outcome.OK, Outcome.MISSING, Outcome.OK]


async def test_a_read_of_108_registers_is_answered_in_one_exchange(live: Live) -> None:
    live.device.configure(MicroNabtoOptions(max_registers=108))
    try:
        points = [_single(DATAPOINTS[i % len(DATAPOINTS)], f"r{i}") for i in range(108)]
        exchanges = _count(live, "exchanges")
        answers = await live.device.read(points)
        assert {a.outcome for a in answers.values()} == {Outcome.OK}
        assert _count(live, "exchanges") == exchanges + 1
    finally:
        live.device.configure(MicroNabtoOptions())


async def test_a_session_left_unused_is_renewed_before_the_device_ends_it(live: Live) -> None:
    """The device ends a session after 15-20 idle seconds."""
    handshakes, resends = _count(live, "handshakes"), _count(live, "resends")
    await asyncio.sleep(22)
    answers = await live.device.read([_single(23)])
    assert answers["a"].outcome is Outcome.OK
    assert _count(live, "handshakes") == handshakes + 1
    assert _count(live, "resends") == resends, "no request was lost to an ended session"


async def test_discovery_asked_directly_is_answered_from_the_device_address() -> None:
    """Asked directly, so a firewall that drops answers to a broadcast cannot fail it."""
    assert HOST is not None
    found = await discover(DEVICE_ID, timeout=2.0, target=(HOST, PORT))
    assert [f.host for f in found] == [HOST]
