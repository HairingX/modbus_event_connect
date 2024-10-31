import concurrent.futures
from dataclasses import dataclass
# import json
import threading
from typing import Any
import pytest

from credentials import Credentials
import logging

from src.modbus_event_connect.micro_nabto.micro_nabto_connection import Request
from src.modbus_event_connect import MODBUS_VALUE_TYPES, ModbusPointKey
from models.micro_nabto_test_models import ModbusTestDatapointKey, ModbusTestMicroNabto
import asyncio

_LOGGER = logging.getLogger(__name__)

@dataclass
class TestData:
    client: ModbusTestMicroNabto
    data = dict[str, Any]()
    credentials: Credentials
    
@pytest.fixture(scope="session")
async def testdata():
    #setup
    credentials = Credentials(["username", "hostname"])
    client = ModbusTestMicroNabto()
    await client.connect(credentials.username, "DEVICE_ID", credentials.hostname)
    # 
    yield TestData(client=client, credentials=credentials)
    #teardown
    client.stop()

async def test_connection_request():
    request = Request(1)
    responsetask = request.wait_for_response()
    request.notify_waiters()
    await responsetask
    
async def test_connection_request_thread():
    request = Request( 1)
    def thread_method():
        asyncio.run(asyncio.sleep(1))
        request.notify_waiters()
    _listen_thread = threading.Thread(target=thread_method)
    _listen_thread.start()
    await request.wait_for_response()
    
async def test_connection_request_asyncio_loop():
    request = Request(1)
    def thread_method():
        asyncio.run(asyncio.sleep(1))
        request.notify_waiters()
    _loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        _loop.run_in_executor(pool, thread_method)
    await request.wait_for_response()

async def test_connect(testdata: TestData):
    client = testdata.client
    assert client.is_connected
    assert client.get_value(ModbusTestDatapointKey.MAJOR_VERSION) is not None

async def test_request_datapoint_data(testdata: TestData):
    client = testdata.client
    event = asyncio.Event()
    def callback(key: ModbusPointKey, oldval:MODBUS_VALUE_TYPES|None, newval:MODBUS_VALUE_TYPES|None):
        _LOGGER.debug(f"{key}: {oldval if oldval is not None else 'None'} -> {newval if newval is not None else 'None'}")
        event.set()
    client.subscribe(ModbusTestDatapointKey.TEMPERATURE, callback)
    await client.request_datapoint_read()
    assert await asyncio.wait_for(event.wait(), 5)
    assert client.get_value(ModbusTestDatapointKey.TEMPERATURE) is not None

async def test_request_datapoint_data_invalid_address(testdata: TestData):
    client = testdata.client
    event1 = asyncio.Event()
    event2 = asyncio.Event()
    events = {ModbusTestDatapointKey.INVALID: event1, ModbusTestDatapointKey.TEMPERATURE: event2}
    def callback(key: ModbusPointKey, oldval:MODBUS_VALUE_TYPES|None, newval:MODBUS_VALUE_TYPES|None):
        _LOGGER.debug(f"{key}: {oldval if oldval is not None else 'None'} -> {newval if newval is not None else 'None'}")
        if key in events: events[key].set()
    client.subscribe(ModbusTestDatapointKey.INVALID, callback)
    client.subscribe(ModbusTestDatapointKey.TEMPERATURE, callback)
    await client.request_datapoint_read()
    await asyncio.wait_for(asyncio.gather(event1.wait(), event2.wait()), 15)
    assert client.get_value(ModbusTestDatapointKey.INVALID) is None
    assert client.get_value(ModbusTestDatapointKey.TEMPERATURE) is not None