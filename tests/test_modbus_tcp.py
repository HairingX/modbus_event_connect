from dataclasses import dataclass
from typing import Any
import logging
import pytest
from datetime import UTC, datetime

from credentials import Credentials

from src.modbus_event_connect.constants import ValueLimits
from src.modbus_event_connect import MODBUS_VALUE_TYPES, ModbusPointKey
from models.modbus_tcp_test_models import ModbusTestDatapointKey, ModbusTestSetpointKey, ModbusTestTCP
import asyncio

_LOGGER = logging.getLogger(__name__)

@dataclass
class TestData:
    client: ModbusTestTCP
    data = dict[str, Any]()
    credentials: Credentials
    
@pytest.fixture(scope="session")
async def testdata():
    #setup
    credentials = Credentials(["username", "hostname"])
    client = ModbusTestTCP()
    await client.connect("DEVICE_ID", credentials.hostname)
    # 
    yield TestData(client=client, credentials=credentials)
    #teardown
    client.stop()

async def test_connect(testdata: TestData):
    client = testdata.client
    assert client.is_connected
    assert client.get_value(ModbusTestDatapointKey.MAJOR_VERSION) is not None

async def test_request_setpoint_value_type_bigint(testdata: TestData):
    key = ModbusTestSetpointKey.DATETIME
    client = testdata.client
    event = asyncio.Event()
    def callback(key: ModbusPointKey, oldval:MODBUS_VALUE_TYPES|None, newval:MODBUS_VALUE_TYPES|None):
        _LOGGER.debug(f"{key}: {oldval if oldval is not None else 'None'} -> {newval if newval is not None else 'None'}")
        event.set()
    client.subscribe(key, callback)
    await client.request_setpoint_read()
    assert await asyncio.wait_for(event.wait(), 5)
    value = client.get_value(key)
    assert value is not None
    assert isinstance(value, int)
    assert value > ValueLimits.INT16_MAX, f"Expected value greater than {ValueLimits.INT16_MAX}, got {value}"
    _LOGGER.debug(f"fromtimestamp(UTC): {datetime.fromtimestamp(value, UTC)}")
    
async def test_request_setpoint_value_type_int(testdata: TestData):
   pass # no need, MAJOR_VERSION is an int, and is already tested in test_connect

async def test_request_point_value_type_float(testdata: TestData):
    key = ModbusTestDatapointKey.TEMPERATURE
    client = testdata.client
    event = asyncio.Event()
    def callback(key: ModbusPointKey, oldval:MODBUS_VALUE_TYPES|None, newval:MODBUS_VALUE_TYPES|None):
        _LOGGER.debug(f"{key}: {oldval if oldval is not None else 'None'} -> {newval if newval is not None else 'None'}")
        event.set()
    client.subscribe(key, callback)
    await client.request_datapoint_read()
    assert await asyncio.wait_for(event.wait(), 5)
    value = client.get_value(key)
    assert value is not None
    assert isinstance(value, float), f"Expected float, got {type(value)} (value = {value})"
    
async def test_request_point_value_type_utf8(testdata: TestData):
    key = ModbusTestSetpointKey.LOCATION_NAME
    client = testdata.client
    event = asyncio.Event()
    def callback(key: ModbusPointKey, oldval:MODBUS_VALUE_TYPES|None, newval:MODBUS_VALUE_TYPES|None):
        _LOGGER.debug(f"{key}: {oldval if oldval is not None else 'None'} -> {newval if newval is not None else 'None'}")
        event.set()
    client.subscribe(key, callback)
    await client.request_setpoint_read()
    assert await asyncio.wait_for(event.wait(), 5)
    value = client.get_value(key)
    assert value is not None
    assert isinstance(value, str)
    

# async def test_request_datapoint_data_invalid_address(testdata: TestData):
#     client = testdata.client
#     event1 = asyncio.Event()
#     event2 = asyncio.Event()
#     events = {ModbusTestDatapointKey.INVALID: event1, ModbusTestDatapointKey.TEMPERATURE: event2}
#     def callback(key: ModbusPointKey, oldval:MODBUS_VALUE_TYPES|None, newval:MODBUS_VALUE_TYPES|None):
#         _LOGGER.debug(f"{key}: {oldval if oldval is not None else 'None'} -> {newval if newval is not None else 'None'}")
#         if key in events: events[key].set()
#     client.subscribe(ModbusTestDatapointKey.INVALID, callback)
#     client.subscribe(ModbusTestDatapointKey.TEMPERATURE, callback)
#     await client.request_datapoint_data()
#     await asyncio.wait_for(asyncio.gather(event1.wait(), event2.wait()), 15)
#     assert client.get_value(ModbusTestDatapointKey.INVALID) is None
#     assert client.get_value(ModbusTestDatapointKey.TEMPERATURE) is not None