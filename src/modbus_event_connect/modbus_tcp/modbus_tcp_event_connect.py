import asyncio
from enum import IntEnum
import logging
from typing import Any, Awaitable, Callable, Dict, Generator, Sequence
from ..modbus_event_connect import *
from ..modbus_models import *
from .transport import (
    EXCEPTION_ILLEGAL_DATA_ADDRESS,
    EXCEPTION_ILLEGAL_FUNCTION,
    EXCEPTION_NONE,
    EXCEPTION_SLAVE_DEVICE_BUSY,
    EXCEPTION_SLAVE_DEVICE_FAILURE,
    ModbusTransport,
    PymodbusTransport,
)

_LOGGER = logging.getLogger(__name__)

class ModbusTCPErrorCode(IntEnum):
    NONE = 0
    NAME_RESOLVE = 1
    CONNECT_FAILED = 2
    SEND_FAILED = 3
    RECV_FAILED = 4
    TIMEOUT = 5
    FRAME_FORMAT = 6
    EXCEPT_ERROR = 7
    """Look at the except error for more details"""
    MB_CRC_ERR = 8
    SOCK_CLOSED = 9
    
    UNSUPPORTED_MODEL = 99
    '''The model is not supported by the adapter'''

class ModbusTCPErrorType(StrEnum):
    #connection errors
    NONE = ''
    NAME_RESOLVE = 'name_resolve_error'
    CONNECT_FAILED = 'connect_error'
    SEND_FAILED = 'socket_send_error'
    RECV_FAILED = 'socket_recv_error'
    TIMEOUT = 'recv_timeout_occur'
    FRAME_FORMAT = 'frame_format_error'
    BAD_CRC = 'bad_CRC_on_receive_frame'
    SOCK_CLOSED = 'socket_is_closed'
    
    # Modbus except code
    EXCEPT_NONE = 'no_exception'
    EXCEPT_ILLEGAL_FUNCTION = 'illegal_function'
    EXCEPT_DATA_ADDRESS = 'illegal_data_address'
    EXCEPT_DATA_VALUE = 'illegal_data_value'
    EXCEPT_SLAVE_DEVICE_FAILURE = 'slave_device_failure'
    EXCEPT_ACKNOWLEDGE = 'acknowledge'
    EXCEPT_SLAVE_DEVICE_BUSY = 'slave_device_busy'
    EXCEPT_NEGATIVE_ACKNOWLEDGE = 'negative_acknowledge'
    EXCEPT_MEMORY_PARITY_ERROR = 'memory_parity_error'
    EXCEPT_GATEWAY_PATH_UNAVAILABLE = 'gateway_path_unavailable'
    EXCEPT_GATEWAY_TARGET_DEVICE_FAILED_TO_RESPOND = 'gateway_target_device_failed_to_respond'
    
    #local errors
    UNSUPPORTED_MODEL = "unsupported_model"

class ModbusExceptCode(IntEnum):
    NONE = 0x00
    ILLEGAL_FUNCTION = 0x01
    DATA_ADDRESS = 0x02
    DATA_VALUE = 0x03
    SLAVE_DEVICE_FAILURE = 0x04
    ACKNOWLEDGE = 0x05
    SLAVE_DEVICE_BUSY = 0x06
    NEGATIVE_ACKNOWLEDGE = 0x07
    MEMORY_PARITY_ERROR = 0x08
    GATEWAY_PATH_UNAVAILABLE = 0x0A
    GATEWAY_TARGET_DEVICE_FAILED_TO_RESPOND = 0x0B

class ModbusTCPEventConnect(ModbusEventConnect):
    
    _transport: ModbusTransport|None = None
    _device_id: str
    _connection_error: Tuple[ModbusTCPErrorType, ModbusTCPErrorCode] = (ModbusTCPErrorType.NONE, ModbusTCPErrorCode.NONE)
    '''The custom connection error that occured during the last connection attempt'''
    DEFAULT_PORT = 502
    DEFAULT_CONNECT_TIMEOUT = 10
    DEFAULT_UNIT_ID = 1
    DEFAULT_REQUEST_LENGTH_MAX = MODBUS_MAX_REQUEST_LENGTH
    """Fallback cap. The real limit comes from the device model's max_request_length."""
    BUSY_RETRY_MAX_ATTEMPTS = 4
    """How many times a request is repeated while the device answers SLAVE_DEVICE_BUSY (0x06)."""
    BUSY_RETRY_INITIAL_DELAY = 0.2
    """Seconds before the first busy retry; doubled on each further attempt."""

    def __init__(self, transport: ModbusTransport|None = None) -> None:
        """
        Args:
            transport: the connection to use. Pass one to share a connection the host already
                owns, rather than opening a second socket to the same controller. When omitted,
                connect() builds a natively-async pymodbus transport.
        """
        super().__init__()
        self._transport = transport
        self._owns_transport = transport is None

    @property
    def transport(self) -> ModbusTransport|None:
        """The connection in use, or None before connect()."""
        return self._transport

    @property
    def device_id(self) -> str: return self._device_id
    
    @property
    def port(self) -> int|None:
        return getattr(self._transport, "port", None)
    @property
    def host(self) -> str|None:
        return getattr(self._transport, "host", None)
    @property
    def unit_id(self) -> int|None:
        return getattr(self._transport, "unit_id", None)
    @property
    def is_connected(self) -> bool:
        return (self._transport is not None and self._transport.is_open
                and self._attr_adapter.ready)
    @property
    def last_except(self) -> int:
        if self._transport is None: return EXCEPTION_NONE
        return self._transport.last_exception_code
    @property
    def last_error(self) -> int:
        if self._connection_error[1] != ModbusTCPErrorCode.NONE: return self._connection_error[1]
        if self._transport is None: return ModbusTCPErrorCode.NONE
        if self._transport.last_exception_code != EXCEPTION_NONE:
            return ModbusTCPErrorCode.EXCEPT_ERROR
        if self._transport.last_error_text is None:
            return ModbusTCPErrorCode.NONE
        # A transport-level failure the Modbus layer cannot classify further. The old code
        # reported RECV_FAILED here, which claimed more than it knew.
        return ModbusTCPErrorCode.SOCK_CLOSED
    @property
    def last_error_txt(self) -> str|None:
        # Read from error-handling paths, so it must never raise itself.
        if self._connection_error[0] != ModbusTCPErrorType.NONE: return self._connection_error[0]
        if self._transport is None: return None
        return self._transport.last_error_text

    async def connect(self, device_id:str, host:str, port:int|None=None, unit_id:int|None=None, timeout:float|None=None) -> bool:
        """
        Open the connection and read the device's initial data.

        Pass a transport to the constructor instead of a host to reuse a connection the host
        application already owns.
        """
        self._connection_error = (ModbusTCPErrorType.NONE, ModbusTCPErrorCode.NONE)
        self._device_id = device_id
        if port is None or port < 1 or port > 65535: port = self.DEFAULT_PORT
        if unit_id is None or unit_id < 0: unit_id = self.DEFAULT_UNIT_ID
        if timeout is None or timeout < 1: timeout = self.DEFAULT_CONNECT_TIMEOUT

        if self._owns_transport:
            await self.stop()
            self._transport = PymodbusTransport(host=host, port=port, unit_id=unit_id, timeout=timeout)

        transport = self._transport
        if transport is None:
            _LOGGER.error("No transport available")
            return False

        if not transport.is_open and not await transport.open():
            _LOGGER.error(f"Could not connect to {host}:{port} - {transport.last_error_text}")
            self._connection_error = (ModbusTCPErrorType.CONNECT_FAILED, ModbusTCPErrorCode.CONNECT_FAILED)
            return False

        device_info = ModbusDeviceInfo(device_id=device_id,
                                       device_host=host,
                                       device_port=port,
                                       version=VersionInfo(),
                                       identification=None,
                                       )
        if not self._attr_adapter.provides_model(device_info):
            _LOGGER.error(f"No model available for {device_info}")
            self._connection_error = (ModbusTCPErrorType.UNSUPPORTED_MODEL, ModbusTCPErrorCode.UNSUPPORTED_MODEL)
            return False

        _LOGGER.debug("Going to load model")
        self._attr_adapter.load_device_model(device_info)
        _LOGGER.debug(f"Loaded model for {self._attr_adapter.model_name} - {device_info}")
        # Let a device model narrow itself to what this particular unit actually has, before
        # anything is read or any consumer decides what to build.
        await self._discover_device()
        await self.request_initial_data()
        _LOGGER.debug("Fetched initial data")
        self._set_status(ModbusStatusKey.CONNECTED, 1)
        return True

    async def stop(self) -> None:
        """Close the connection, if this client opened it."""
        self._set_status(ModbusStatusKey.CONNECTED, 0)
        self._set_status(ModbusStatusKey.DEVICE_BUSY, 0)
        if self._transport is not None and self._owns_transport:
            await self._transport.close()
        
    #region reading

    async def _call_device(self, operation: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """
        Perform one transport call, repeating it while the device answers
        SLAVE_DEVICE_BUSY (0x06).

        Devices raise 0x06 while they persist a configuration change - which happens after any
        write and after any change made at the device's own user interface - and throughout
        start-up. Device manuals require the request to simply be repeated; without this, reads
        drop out intermittently, most often right after the client has written something.
        """
        transport = self._transport
        if transport is None: return None
        delay = self.BUSY_RETRY_INITIAL_DELAY
        for attempt in range(1, self.BUSY_RETRY_MAX_ATTEMPTS + 1):
            result = await operation(*args)
            self._set_status(ModbusStatusKey.LAST_EXCEPTION_CODE, transport.last_exception_code)
            if result is not None and result is not False:
                self._set_status(ModbusStatusKey.DEVICE_BUSY, 0)
                return result
            if transport.last_exception_code != EXCEPTION_SLAVE_DEVICE_BUSY:
                self._set_status(ModbusStatusKey.DEVICE_BUSY, 0)
                return result
            # Publish the busy state so consumers can hold off instead of queueing writes.
            self._set_status(ModbusStatusKey.DEVICE_BUSY, 1)
            if attempt == self.BUSY_RETRY_MAX_ATTEMPTS:
                _LOGGER.warning(f"Device still busy after {attempt} attempts, giving up on this request")
                return result
            _LOGGER.debug(f"Device busy (0x06), retrying in {delay:.2f}s (attempt {attempt})")
            await asyncio.sleep(delay)
            delay *= 2
        return None

    async def _discover_device(self) -> None:
        """
        Hook for device models that can narrow themselves to one installation.

        Called once per connect(), after the model is loaded and before anything is read.
        The default does nothing; see WavinSentioTCPConnect.discover().
        """
        return None

    async def _probe_device(self) -> None:
        """
        One single-register read, purely to refresh the status keys.

        Uses a register the device model guarantees exists - the first point read at startup,
        normally an address-space version - so the probe cannot itself fail with
        ILLEGAL_DATA_ADDRESS and be mistaken for the device being unwell. It is read from the
        table that point declares: a model that keeps everything in holding registers would
        otherwise be probed in the input space, at an address that means something else.

        Measured at ~1 ms against a Sentio CCU-208, so polling this while waiting out a busy
        period is far cheaper than re-reading real data to find out.
        """
        transport = self._transport
        if transport is None: return
        point = self._probe_point()
        if point is None: return
        await self._reader_for(point.register_table)(point.read_address, 1)
        self._set_status(ModbusStatusKey.LAST_EXCEPTION_CODE, transport.last_exception_code)
        self._set_status(ModbusStatusKey.DEVICE_BUSY,
                         1 if transport.last_exception_code == EXCEPTION_SLAVE_DEVICE_BUSY else 0)

    def _probe_point(self) -> ModbusDatapoint|None:
        """
        Pick a register to probe with: prefer one read at startup, since those are the points
        a model declares as always present. Fall back to any readable datapoint, so a model
        without version keys still gets a working probe rather than a silent no-op.
        """
        if not self._attr_adapter.has_model: return None
        points = self._attr_adapter.get_initial_datapoints_for_read()
        if points: return points[0]
        points = self._attr_adapter.get_datapoints_for_read()
        return points[0] if points else None

    def _reader_for(self, table: RegisterTable) -> Callable[[int, int], Awaitable[List[int]|List[bool]|None]]:
        """Pick the transport method that reads the given address space."""
        transport = self._transport
        assert transport is not None
        if table == RegisterTable.INPUT: return transport.read_input_registers
        if table == RegisterTable.HOLDING: return transport.read_holding_registers
        if table == RegisterTable.DISCRETE: return transport.read_discrete_inputs
        return transport.read_coils

    async def _request_datapoint_read(self, points: List[ModbusDatapoint]) -> List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES|None]]:
        transport = self._transport
        if transport is None:
            _LOGGER.warning("Cannot read datapoints, not connected")
            return []
        return await self._request_points_read(points, "datapoints")

    async def _request_setpoint_read(self, points: List[ModbusSetpoint]) -> List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES|None]]:
        transport = self._transport
        if transport is None:
            _LOGGER.warning("Cannot read setpoints, not connected")
            return []
        return await self._request_points_read(points, "setpoints")

    async def _request_points_read(self, points: Sequence[MODBUS_POINT_TYPE], what: str) -> List[Tuple[MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES|None]]:
        kv: List[Tuple[MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES|None]] = []
        for batch in self.batch_reads(points):
            first, last = batch[0], batch[-1]
            if first.read_address is None or last.read_address is None: continue
            reader = self._reader_for(first.register_table)
            read_length = last.read_address + last.read_length - first.read_address
            data = await self._call_device(reader, first.read_address, read_length)
            data = self._normalize_read_result(data)
            if data is not None:
                self._append_data(kv, batch, data)
            else:
                await self._handle_batch_failure(kv, batch, reader, what)
        return kv

    def _normalize_read_result(self, data: "List[int]|List[bool]|None") -> List[int]|None:
        """
        read_discrete_inputs/read_coils return List[bool]; the rest of the decoding path
        (ModbusParser etc.) expects raw register ints, so bits become 0/1 here.
        """
        if data is None: return None
        return [int(v) for v in data]

    async def _handle_batch_failure(self, kv: List[Tuple[MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES|None]],
                                    batch: List[MODBUS_POINT_TYPE],
                                    reader: Callable[[int, int], Awaitable[List[int]|List[bool]|None]],
                                    what: str) -> None:
        """
        Deal with a batch the device refused.

        Only the points in THIS batch are retried. Retrying the caller's entire point list, as
        an earlier version did, re-reads unrelated batches and appends duplicate results.
        """
        transport = self._transport
        if transport is None: return
        keys = [point.key for point in batch]
        last_except = transport.last_exception_code
        if last_except == EXCEPTION_ILLEGAL_FUNCTION:
            _LOGGER.error(f"Device does not support reading {what} registers, inform developer that the device '{self.device_info}' has this error")
            return
        if last_except == EXCEPTION_SLAVE_DEVICE_FAILURE:
            # The registers exist, but the peripheral behind them is disconnected right now -
            # a Calefa DHW unit, for instance. Report the points as having no value so
            # consumers can show them unavailable, but leave them enabled: unlike a register
            # this unit simply does not have, this condition clears when the peripheral
            # reconnects. Logged at debug, or a disconnected peripheral would produce an
            # error line on every single poll.
            _LOGGER.debug(f"{what} {keys} belong to a peripheral that is disconnected")
            for point in batch:
                kv.append((point, None))
            return
        if last_except != EXCEPTION_ILLEGAL_DATA_ADDRESS:
            _LOGGER.error(f"Failed to read {what} {keys}: {transport.last_error_text}")
            return

        # ILLEGAL_DATA_ADDRESS means at least one register in the batch does not exist. Devices
        # reject the whole request in that case, so the readable points must be found one by one.
        if len(batch) == 1:
            point = batch[0]
            kv.append((point, None))
            self._handle_invalid_address(point)
            return
        _LOGGER.debug(f"Device rejected a batch of {len(batch)} {what}; reading each individually to find which register(s) this unit does not have.")
        for point in batch:
            if point.read_address is None: continue
            # Read the point's own width; a multi-register point read as 1 register decodes wrongly.
            data = await self._call_device(reader, point.read_address, point.read_length)
            data = self._normalize_read_result(data)
            if data is not None:
                self._append_data(kv, [point], data)
            elif transport.last_exception_code == EXCEPTION_ILLEGAL_DATA_ADDRESS:
                kv.append((point, None))
                self._handle_invalid_address(point)
            else:
                _LOGGER.error(f"Failed to read {what} '{point.key}': {transport.last_error_text}")

    #endregion reading
    #region writing

    async def _request_setpoint_writes(self, point_values: Sequence[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]]) -> bool:
        all_written = True
        for point, value in point_values:
            if not await self._request_setpoint_write(point, value):
                all_written = False
        return all_written

    async def _request_setpoint_write(self, point: ModbusSetpoint, value: MODBUS_VALUE_TYPES) -> bool:
        transport = self._transport
        if transport is None:
            _LOGGER.error(f"Cannot write '{point.key}', not connected")
            return False
        # 0 is a valid Modbus address; only a missing or negative address is an error.
        if point.write_address is None or point.write_address < 0:
            _LOGGER.error(f"Cannot write '{point.key}', it has no write address")
            return False

        values = self._parse_point_write_value(point, value)
        if values is None or len(values) == 0:
            _LOGGER.error(f"Cannot write '{point.key}', the value could not be encoded")
            return False

        if point.write_length == 1:
            written = await self._call_device(transport.write_register, point.write_address, values[0])
        elif point.write_length > 1:
            written = await self._call_device(transport.write_registers, point.write_address, values)
        else:
            return False

        if written is not True:
            _LOGGER.error(f"Failed to write '{point.key}': {transport.last_error_text}")
            return False
        return True

    #endregion writing

    def _append_data(self, kv: List[Tuple[MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES|None]], points: List[MODBUS_POINT_TYPE], data: List[int]) -> None:
        i = 0
        for point in points:
            values = data[i:i+point.read_length]
            value = self._parse_point_read_value(point, values)
            kv.append((point, value))
            i+= point.read_length
    
    def _max_request_length(self) -> int:
        """Registers per request allowed by the connected device, if it declares a limit."""
        try:
            return self._attr_adapter.max_request_length
        except Exception:
            return self.DEFAULT_REQUEST_LENGTH_MAX

    def batch_reads(self, points: Sequence[MODBUS_POINT_TYPE]) -> Generator[List[MODBUS_POINT_TYPE], None, None]:
        """
        Group points into runs of genuinely adjacent registers, each fetchable in one request.

        Points are first grouped by `register_table`: INPUT, HOLDING, DISCRETE and COIL are
        four separate address spaces, so address 1 in INPUT and address 1 in HOLDING are
        unrelated registers and must never be merged into the same request.

        Within a table, a point may only join a batch if it starts exactly where the previous
        one ends (`read_address + read_length`). Stepping by 1 instead lets a multi-register
        point overlap its neighbour, and `_append_data` then slices the response wrongly - the
        neighbour silently decodes to 0. It also splits runs that are in fact contiguous.

        Batches are capped at the device's documented maximum request length. Devices commonly
        allow far fewer than the protocol's 125 registers.
        """
        max_length = self._max_request_length()
        readable: List[MODBUS_POINT_TYPE] = [p for p in points if p.read_address is not None]
        by_table: Dict[RegisterTable, List[MODBUS_POINT_TYPE]] = {}
        for point in readable:
            by_table.setdefault(point.register_table, []).append(point)

        for table_points in by_table.values():
            ordered: List[MODBUS_POINT_TYPE] = sorted(table_points, key=lambda x: x.read_address or 0)
            batch: List[MODBUS_POINT_TYPE] = []
            for point in ordered:
                if not batch:
                    batch = [point]
                    continue
                previous, first = batch[-1], batch[0]
                contiguous = previous.read_address + previous.read_length == point.read_address  # type: ignore
                span = point.read_address + point.read_length - first.read_address              # type: ignore
                if contiguous and span <= max_length:
                    batch.append(point)
                else:
                    yield batch
                    batch = [point]
            if batch:
                yield batch
