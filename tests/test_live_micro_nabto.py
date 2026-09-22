"""Read-only tests of the micro_nabto transport against a real device.

This library has two transports, and each one gets a live test named after it. Which product
answers is not the library's business - point it at whatever you have.

    Configure it either way:
        set MICRO_NABTO_HOST=<device-ip>        (environment variables)
        set MICRO_NABTO_EMAIL=you@example.com

        MICRO_NABTO_HOST = "<device-ip>"        (or in mysecrets_micro_nabto.py, gitignored,
        MICRO_NABTO_EMAIL = "you@example.com"      falling back to mysecrets.py)

    Optional:
        MICRO_NABTO_PORT        default 5570
        MICRO_NABTO_DEVICE_ID   only a dictionary key while a host is given

    Run:
        pytest tests/test_live_micro_nabto.py -v -s

NOTHING HERE WRITES TO THE DEVICE. A guard in the fixture replaces every write path with one
that fails the test, and a test checks the guard itself works.

These do not assert particular values - they report what the device says about itself. That is
the point: a Nabto device hands over its identity during the handshake, and that identity is
what a device model branches on, so it has to be seen before it can be designed against.
"""
import logging
import os
from dataclasses import dataclass

import pytest
import pytest_asyncio

from conftest import live_or_skip, live_setting
from src.modbus_event_connect.micro_nabto.micro_nabto_connection import (
    MicroNabtoConnectionErrorType,
)
from models.micro_nabto_test_models import ModbusTestDatapointKey, ModbusTestMicroNabto

_LOGGER = logging.getLogger(__name__)


MY = "mysecrets_micro_nabto"

HOST = live_setting("MICRO_NABTO_HOST", MY)
EMAIL = live_setting("MICRO_NABTO_EMAIL", MY)
DEVICE_ID = live_setting("MICRO_NABTO_DEVICE_ID", MY) or "device"
PORT = int(live_setting("MICRO_NABTO_PORT", MY) or "5570")

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    *live_or_skip("micro_nabto", MICRO_NABTO_HOST=HOST, MICRO_NABTO_EMAIL=EMAIL),
]


@dataclass
class Live:
    client: ModbusTestMicroNabto


def _forbid_writes(client: ModbusTestMicroNabto) -> None:
    """Make any write attempt fail loudly instead of reaching the device."""
    def refuse(*args, **kwargs):
        raise AssertionError("A write was attempted. These tests are read-only.")

    connection = client._client
    connection.request_setpoint_write = refuse   # type: ignore[assignment]
    connection.request_setpoint_writes = refuse  # type: ignore[assignment]
    client._request_setpoint_write = refuse      # type: ignore[assignment]
    client._request_setpoint_writes = refuse     # type: ignore[assignment]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live():
    client = ModbusTestMicroNabto()
    assert EMAIL is not None and HOST is not None   # guarded by the skipif above
    connected = await client.connect(EMAIL, DEVICE_ID, HOST, PORT)
    if not connected:
        client.stop()
        # The error says which half failed, so report that rather than a list of guesses.
        error = client.last_error
        if error == MicroNabtoConnectionErrorType.AUTHENTICATION_ERROR:
            detail = ("The device answered and rejected us, so the network path is fine.\n"
                      "  MICRO_NABTO_EMAIL must be an address authorised on the device through\n"
                      "  its vendor's app.")
        elif error == MicroNabtoConnectionErrorType.TIMEOUT:
            detail = ("No answer at all.\n"
                      "  - Is MICRO_NABTO_HOST right, and is the device on this subnet?\n"
                      "  - Without a host the client broadcasts to find it, which needs UDP\n"
                      "    broadcast allowed through the firewall.")
        else:
            detail = "Unexpected failure."
        pytest.fail(f"Could not connect at {HOST}:{PORT}.\n  {detail}\n  last error: {error}")
    _forbid_writes(client)
    yield Live(client=client)
    client.stop()


async def test_connects_and_reports_its_identity(live: Live):
    """The handshake carries the identity, so a model can be chosen without reading anything."""
    client = live.client
    assert client.is_connected

    info = client.device_info
    _LOGGER.info(f"device_info: {info}")
    print(f"\n  device_info        {info}")
    print(f"  manufacturer       {client.manufacturer}")
    print(f"  model_name         {client.model_name}")
    print(f"  version            {info.version}")

    assert info.device_id


async def test_reads_a_datapoint(live: Live):
    """A value arrives, and the version the model declares is among what startup read."""
    client = live.client
    value = client.get_value(ModbusTestDatapointKey.MAJOR_VERSION)
    print(f"\n  MAJOR_VERSION      {value}")
    assert value is not None, "startup did not read the model's version datapoint"


async def test_an_absent_register_does_not_break_the_others(live: Live):
    """
    The test model declares address 9191, which the device is not expected to have.

    A point the device does not have must not cost the points read alongside it. The Modbus TCP
    side gives that guarantee and has tests for it; this transport has never been checked.
    """
    client = live.client
    for key in (ModbusTestDatapointKey.TEMPERATURE, ModbusTestDatapointKey.INVALID):
        client.set_read(key, True)
    await client.request_datapoint_read()

    temperature = client.get_value(ModbusTestDatapointKey.TEMPERATURE)
    invalid = client.get_value(ModbusTestDatapointKey.INVALID)
    print(f"\n  TEMPERATURE        {temperature}")
    print(f"  INVALID (9191)     {invalid}")

    assert invalid is None, "address 9191 should not have produced a value"
    assert temperature is not None, "a real register was lost alongside the absent one"


async def test_the_write_guard_is_active(live: Live):
    """The guard itself must work, or every other test here is worthless."""
    with pytest.raises(AssertionError, match="read-only"):
        live.client._client.request_setpoint_writes([])
