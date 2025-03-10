from enum import IntEnum
import logging
from typing import Generator, Sequence
from pyModbusTCP.client import ModbusClient # type: ignore

from ..modbus_event_connect import *
from ..modbus_models import *

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
    
    _client: ModbusClient|None = None# ModbusClient(host="", port=0, unit_id=0, auto_open=False, auto_close=False)
    _device_id: str
    _connection_error: Tuple[ModbusTCPErrorType, ModbusTCPErrorCode] = (ModbusTCPErrorType.NONE, ModbusTCPErrorCode.NONE)
    '''The custom connection error that occured during the last connection attempt'''
    DEFAULT_PORT = 502
    DEFAULT_CONNECT_TIMEOUT = 10
    DEFAULT_UNIT_ID = 1
    DEFAULT_AUTO_OPEN = True
    DEFAULT_AUTO_CLOSE = False
    DEFAULT_REQUEST_LENGTH_MAX = 125

    @property
    def device_id(self) -> str: return self._device_id
    
    @property
    def port(self) -> int|None:
        if self._client is None:
            return None
        return self._client.port
    @property
    def host(self) -> str|None:
        if self._client is None:
            return None
        return self._client.host
    @property
    def unit_id(self) -> int|None:
        if self._client is None:
            return None
        return self._client.unit_id
    @property
    def auto_open(self) -> bool|None:
        if self._client is None:
            return None
        return self._client.auto_open
    @property
    def auto_close(self) -> bool|None:
        if self._client is None:
            return None
        return self._client.auto_close
    @property
    def is_connected(self) -> bool: return self._client is not None and self._client.is_open and self._attr_adapter.ready
    @property
    def last_error(self) -> int: 
        if self._connection_error[1] != ModbusTCPErrorCode.NONE: return self._connection_error[1]
        if self._client is None: return ModbusTCPErrorCode.NONE
        return int(self._client.last_error)  # type: ignore
    @property
    def last_except(self) -> int: 
        if self._client is None: return ModbusExceptCode.NONE
        return int(self._client.last_except)  # type: ignore
    @property
    def last_error_txt(self) -> str|None: 
        if self._connection_error[0] != ModbusTCPErrorType.NONE: return self._connection_error[0]
        if self._client is None: return None
        if int(self._client.last_error) == ModbusTCPErrorCode.NONE: return None # type: ignore
        if int(self._client.last_error) == ModbusTCPErrorCode.EXCEPT_ERROR: return self._client.last_except_as_txt # type: ignore
        return self._client.last_error_as_txt # type: ignore
         
    async def connect(self, device_id:str, host:str, port:int|None=None, unit_id:int|None=None, timeout:float|None=None, auto_open:bool|None=None, auto_close:bool|None=None) -> bool:
        self._connection_error = (ModbusTCPErrorType.NONE, ModbusTCPErrorCode.NONE)
        self._device_id = device_id
        if port is None or port < 1 or port > 65535: port = self.DEFAULT_PORT
        if unit_id is None or unit_id < 1: unit_id = self.DEFAULT_UNIT_ID
        if timeout is None or timeout < 1: timeout = self.DEFAULT_CONNECT_TIMEOUT
        if auto_open is None or auto_open < 1: auto_open = self.DEFAULT_AUTO_OPEN
        if auto_close is None or auto_close < 1: auto_close = self.DEFAULT_AUTO_CLOSE
        
        self.stop()
        self._client = ModbusClient(host=host, port=port, unit_id=unit_id, timeout=timeout, auto_open=auto_open, auto_close=auto_close)
        success = self._client.open()
        if not success: return False
        device_identification = self._client.read_device_identification()
        identification = None
        if device_identification is not None:
            vendor_name = None if device_identification.vendor_name is None else device_identification.vendor_name.decode('ascii')
            product_code = None if device_identification.product_code is None else device_identification.product_code.decode('ascii')
            major_minor_revision = None if device_identification.major_minor_revision is None else device_identification.major_minor_revision.decode('ascii')
            vendor_url = None if device_identification.vendor_url is None else device_identification.vendor_url.decode('ascii')
            product_name = None if device_identification.product_name is None else device_identification.product_name.decode('ascii')
            model_name = None if device_identification.model_name is None else device_identification.model_name.decode('ascii')
            user_application_name = None if device_identification.user_application_name is None else device_identification.user_application_name.decode('ascii')
            identification = ModbusDeviceIdenfication(vendor_name=vendor_name, product_code=product_code, major_minor_revision=major_minor_revision, vendor_url=vendor_url, product_name=product_name, model_name=model_name, user_application_name=user_application_name)
            
        _LOGGER.debug(f"Device identification: {identification if identification is not None else self._client.last_except_as_full_txt}")
        
        device_info = ModbusDeviceInfo(device_id=device_id, 
                                       device_host=host,
                                       device_port=port,
                                       version=VersionInfo(),
                                       identification=identification,
                                       )
        if self._attr_adapter.provides_model(device_info):
            _LOGGER.debug(f"Going to load model")
            self._attr_adapter.load_device_model(device_info)
            _LOGGER.debug(f"Loaded model for {self._attr_adapter.model_name} - {device_info}")
            await self.request_initial_data()
            _LOGGER.debug(f"Fetched initial data")
            return True
        else:
            _LOGGER.error(f"No model available for {device_info}")
            self._connection_error = (ModbusTCPErrorType.UNSUPPORTED_MODEL, ModbusTCPErrorCode.UNSUPPORTED_MODEL) 
            return False
    
    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
        
    async def _request_datapoint_read(self, points: List[ModbusDatapoint]) -> List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES|None]]:
        kv:List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES|None]] = []
        if self._client is None: return kv
        for batch in self.batch_reads(points):
            last = batch[-1]
            first = batch[0]
            read_length = last.read_address+last.read_length - first.read_address
            data: List[int] | None = self._client.read_input_registers(batch[0].read_address, read_length)  # type: ignore
            if data is not None:
                self._append_data(kv, batch, data)
            else:
                last_error: int|None = self._client.last_error # type: ignore
                if last_error == ModbusTCPErrorCode.EXCEPT_ERROR: 
                    last_except: int|None = self._client.last_except # type: ignore
                    if last_except == ModbusExceptCode.ILLEGAL_FUNCTION:
                        _LOGGER.error(f"Device does not support reading datapoints registers, inform developer that the device '{self.device_info}' has this error")
                    if last_except == ModbusExceptCode.DATA_ADDRESS:
                        if len(points) == 1:
                            point = points[0]
                            kv.append((points[0], None))
                            self._handle_invalid_address(point)
                        else:
                            _LOGGER.warning(f"Device failed to read {len(points)} datapoints. Some datapoints may not be available. Checking each datapoint individually.")
                            for point in points:
                                data = self._client.read_input_registers(point.read_address, 1)  # type: ignore
                                if data is not None:
                                    self._append_data(kv, [point], data)
                                elif self._client.last_error == ModbusTCPErrorCode.EXCEPT_ERROR and self._client.last_except == ModbusExceptCode.DATA_ADDRESS: # type: ignore
                                    kv.append((point, None))
                                    self._handle_invalid_address(point)
                    else: 
                        _LOGGER.error(f"Failed to read data for {[point.key for point in points]}")
                else: 
                    _LOGGER.error(f"Failed to read data for {[point.key for point in points]}")
        return kv
    
    async def _request_setpoint_read(self, points: List[ModbusSetpoint]) -> List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES|None]]:
        kv:List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES|None]] = []
        for batch in self.batch_reads(points):
            last = batch[-1]
            first = batch[0]
            if last.read_address is None or first.read_address is None: continue # never true, just to make typecheck happy
            read_length = last.read_address+last.read_length - first.read_address
            data:List[int]|None = self._client.read_holding_registers(batch[0].read_address, read_length) # type: ignore
            if data is not None:
                self._append_data(kv, batch, data)
            else:
                _LOGGER.error(f"Failed to read data for {batch[0].read_address}-{batch[-1].read_address}")
                last_error: int|None = self._client.last_error # type: ignore
                if last_error == ModbusTCPErrorCode.EXCEPT_ERROR: 
                    last_except: int|None = self._client.last_except # type: ignore
                    if last_except == ModbusExceptCode.ILLEGAL_FUNCTION:
                        _LOGGER.error(f"Device does not support reading setpoints registers, inform developer that the device '{self.device_info}' has this error")
                    if last_except == ModbusExceptCode.DATA_ADDRESS:
                        if len(points) == 1:
                            point = points[0]
                            kv.append((points[0], None))
                            self._handle_invalid_address(point)
                        else:
                            _LOGGER.warning(f"Device failed to read {len(points)} setpoints. Some setpoints may not be available. Checking each setpoint individually.")
                            for point in points:
                                data = self._client.read_input_registers(point.read_address, 1)  # type: ignore
                                if data is not None:
                                    self._append_data(kv, [point], data)
                                elif self._client.last_error == ModbusTCPErrorCode.EXCEPT_ERROR and self._client.last_except == ModbusExceptCode.DATA_ADDRESS: # type: ignore
                                    kv.append((point, None))
                                    self._handle_invalid_address(point)
                    else: 
                        _LOGGER.error(f"Failed to read data for {[point.key for point in points]}")
                else: 
                    _LOGGER.error(f"Failed to read data for {[point.key for point in points]}")
        return kv    
    
    def _request_setpoint_writes(self, point_values: Sequence[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]]) -> bool:
        for point, value in point_values:
            self._request_setpoint_write(point, value)
        return True
    
    def _request_setpoint_write(self, point: ModbusSetpoint, value: MODBUS_VALUE_TYPES) -> bool:
        if point.write_address is None or point.write_address < 1 or self._client is None: return False
        
        values = self._parse_point_write_value(point, value)
        if values is None or len(values) == 0: return False
        
        if point.write_length == 1:
            return self._client.write_single_register(point.write_address, values[0]) == True # type: ignore
        elif point.write_length > 1:
            return self._client.write_multiple_registers(point.write_address, values) == True # type: ignore
        
        return False
    
    def _append_data(self, kv: List[Tuple[MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES|None]], points: List[MODBUS_POINT_TYPE], data: List[int]) -> None:
        i = 0
        for point in points:
            values = data[i:i+point.read_length]
            value = self._parse_point_read_value(point, values)
            kv.append((point, value))
            i+= point.read_length
    
    def batch_reads(self, points:Sequence[MODBUS_POINT_TYPE]) -> Generator[List[MODBUS_POINT_TYPE], None, None]:
        """
        Returns the points to read in batches, where the read_address values are in sequence. 
        If the sequence is broken, a new batch is started
        """
        points = sorted(points, key=lambda x: x.read_address if x.read_address is not None else -1)
        batch = []
        for point in points:
            if point.read_address is None: 
                continue
            elif len(batch) == 0:
                batch.append(point)
            elif (batch[-1].read_address is not None and batch[0].read_address is not None and # they are never none.
                  batch[-1].read_address + 1 == point.read_address and ( point.read_address + point.read_length - batch[0].read_address) <= self.DEFAULT_REQUEST_LENGTH_MAX):
                batch.append(point)
            else:
                yield batch
                batch = [point]
        if len(batch) > 0:
            yield batch
    
    
    
    
    #  def batch_reads(self, points:List[ModbusDatapoint]) -> List[List[ModbusDatapoint]]:
    #     """
    #     Returns the points to read in batches, where the read_address values are in sequence. 
    #     If the sequence is broken, a new batch is started
    #     """
    #     points = sorted(points, key=lambda x: x.read_address)
    #     batch:List[ModbusDatapoint] = []
    #     batchreads:List[List[ModbusDatapoint]] = []
    #     for point in points:
    #         if len(batch) == 0:
    #             batch.append(point)
    #         elif batch[-1].read_address + 1 == point.read_address:
    #             batch.append(point)
    #         else:
    #             batchreads.append(batch)
    #             batch = [point]
    #     if len(batch) > 0:
    #         batchreads.append(batch)
    #     return batchreads