"""Protocol-level tests for micro_nabto. No hardware, so these run everywhere.

Anything that needs a real device lives in test_live_micro_nabto.py, where the write
guard is.
"""
import asyncio
import concurrent.futures
import logging
import threading

from src.modbus_event_connect.micro_nabto.micro_nabto_connection import Request

_LOGGER = logging.getLogger(__name__)

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
