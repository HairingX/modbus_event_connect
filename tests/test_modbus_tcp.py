import concurrent.futures
from dataclasses import dataclass
# import json
import threading
from typing import Any
import pytest

from credentials import Credentials
import logging

from src.modbus_event_connect.micro_nabto.micro_nabto_connection import Request
from src.modbus_event_connect import MODBUS_VALUE_TYPES
from models.modbus_tcp_test_models import ModbusTestDatapointKey, ModbusTestTCP
import asyncio

_LOGGER = logging.getLogger(__name__)

@dataclass
class TestData:
    client: ModbusTestTCP
    data = dict[str, Any]()
    credentials: Credentials
    
@pytest.fixture
def testdata():
    #setup
    credentials = Credentials(["username", "hostname"])
    client = ModbusTestTCP()
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
    credentials = testdata.credentials
    client = testdata.client
    success = await client.connect("DEVICE_ID", credentials.hostname)
    assert success

async def test_datapoint(testdata: TestData):
    credentials = testdata.credentials
    client = testdata.client
    success = await client.connect("DEVICE_ID", credentials.hostname)
    assert success
    event = asyncio.Event()
    def callback(oldval:MODBUS_VALUE_TYPES|None, newval:MODBUS_VALUE_TYPES|None):
        _LOGGER.debug(f"{ModbusTestDatapointKey.MAJOR_VERSION}: {oldval if oldval is not None else 'None'} -> {newval if newval is not None else 'None'}")
        event.set()
    client.subscribe(ModbusTestDatapointKey.MAJOR_VERSION, callback)
    await client.request_datapoint_data()
    assert await event.wait()