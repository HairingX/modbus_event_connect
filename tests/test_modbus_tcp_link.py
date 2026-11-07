"""`ModbusTcpConnection` with the real pymodbus client, against a minimal Modbus TCP server on a
localhost port."""
import asyncio
import struct
from collections.abc import AsyncIterator

import pytest

from modbus_event_connect.modbus import FunctionCode, ModbusTcpConnection, Request

READ = Request(1, FunctionCode.READ_HOLDING_REGISTERS, 0, count=2)


class Server:
    """Answers every read of holding registers with zeros, and keeps count of its links."""

    def __init__(self) -> None:
        self.links: set[asyncio.StreamWriter] = set()
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.drop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def drop(self) -> None:
        """Close every link from the server's side, as a device that restarts does."""
        for link in list(self.links):
            link.close()

    async def settle(self) -> None:
        """Wait until the links have had time to open and close."""
        await asyncio.sleep(0.5)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.links.add(writer)
        try:
            while True:
                transaction, _, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                pdu = await reader.readexactly(length - 1)
                count = struct.unpack(">H", pdu[3:5])[0]
                body = bytes([FunctionCode.READ_HOLDING_REGISTERS, count * 2]) + bytes(count * 2)
                writer.write(struct.pack(">HHHB", transaction, 0, len(body) + 1, unit) + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.links.discard(writer)
            writer.close()


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    found = Server()
    await found.start()
    yield found
    await found.stop()


async def test_a_link_the_device_dropped_is_reopened_once_and_closed_with_the_connection(server: Server) -> None:
    conn = ModbusTcpConnection("127.0.0.1", server.port, timeout=1.0)
    assert (await conn.request(READ)).ok
    server.drop()
    await asyncio.sleep(0.02)
    assert (await conn.request(READ)).ok
    await server.settle()
    assert len(server.links) == 1
    await conn.close()
    await server.settle()
    assert server.links == set()
