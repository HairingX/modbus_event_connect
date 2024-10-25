from enum import Enum
import logging
from typing import Generator, Sequence, TypeVar
from pyModbusTCP.client import ModbusClient # type: ignore

from ..modbus_event_connect import *
from ..modbus_models import *

_LOGGER = logging.getLogger(__name__)

DEVICE_PORT = 502
CONNECT_TIMEOUT = 10
UNIT_ID = 1
AUTO_OPEN = True
AUTO_CLOSE = False

class ModbusTCPErrorCode(Enum):
    NO_ERROR = 0
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

class ModbusTCPErrorType(StrEnum):
    #connection errors
    NAME_RESOLVE = 'name_resolve_error'
    CONNECT_FAILED = 'connect_error'
    SEND_FAILED = 'socket_send_error'
    RECV_FAILED = 'socket_recv_error'
    TIMEOUT = 'recv_timeout_occur'
    FRAME_FORMAT = 'frame_format_error'
    BAD_CRC = 'bad_CRC_on_receive_frame'
    SOCK_CLOSED = 'socket_is_closed'
    
    # Modbus except code
    EXP_NONE = 'no_exception'
    EXP_ILLEGAL_FUNCTION = 'illegal_function'
    EXP_DATA_ADDRESS = 'illegal_data_address'
    EXP_DATA_VALUE = 'illegal_data_value'
    EXP_SLAVE_DEVICE_FAILURE = 'slave_device_failure'
    EXP_ACKNOWLEDGE = 'acknowledge'
    EXP_SLAVE_DEVICE_BUSY = 'slave_device_busy'
    EXP_NEGATIVE_ACKNOWLEDGE = 'negative_acknowledge'
    EXP_MEMORY_PARITY_ERROR = 'memory_parity_error'
    EXP_GATEWAY_PATH_UNAVAILABLE = 'gateway_path_unavailable'
    EXP_GATEWAY_TARGET_DEVICE_FAILED_TO_RESPOND = 'gateway_target_device_failed_to_respond'
    
    #local errors
    UNSUPPORTED_MODEL = "unsupported_model"

class ModbusExceptCode(Enum):
    EXP_NONE = 0x00
    EXP_ILLEGAL_FUNCTION = 0x01
    EXP_DATA_ADDRESS = 0x02
    EXP_DATA_VALUE = 0x03
    EXP_SLAVE_DEVICE_FAILURE = 0x04
    EXP_ACKNOWLEDGE = 0x05
    EXP_SLAVE_DEVICE_BUSY = 0x06
    EXP_NEGATIVE_ACKNOWLEDGE = 0x07
    EXP_MEMORY_PARITY_ERROR = 0x08
    EXP_GATEWAY_PATH_UNAVAILABLE = 0x0A
    EXP_GATEWAY_TARGET_DEVICE_FAILED_TO_RESPOND = 0x0B

class ModbusTCPEventConnect(ModbusEventConnect):
    
    _client: ModbusClient|None = None# ModbusClient(host="", port=0, unit_id=0, auto_open=False, auto_close=False)
    
    def is_connected(self) -> bool: return self._client is not None and self._client.is_open and self._attr_adapter.ready
    def get_connection_error(self) -> str|None: 
        if self._client is None: return None
        if int(self._client.last_error) == ModbusTCPErrorCode.NO_ERROR: return None # type: ignore
        if int(self._client.last_error) == ModbusTCPErrorCode.EXCEPT_ERROR: return self._client.last_except_as_txt # type: ignore
        return self._client.last_error_as_txt # type: ignore
         
    async def connect(self, device_id:str, device_host:str, device_port:int=DEVICE_PORT, unit_id:int=UNIT_ID, auto_open:bool=AUTO_OPEN, auto_close:bool=AUTO_CLOSE, timeout:float = CONNECT_TIMEOUT) -> bool:
        self.stop()
        self._client = ModbusClient(host=device_host, port=device_port, unit_id=unit_id, auto_open=auto_open, auto_close=auto_close)
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
                                       device_host=device_host,
                                       device_port=device_port,
                                       version=VersionInfo(0, 0, 0),
                                       identification=identification,
                                       )
        if self._attr_adapter.provides_model(device_info):
            _LOGGER.debug(f"Going to load model")
            self._attr_adapter.instantiate(device_info)
            _LOGGER.debug(f"Loaded model for {self._attr_adapter.model_name} - {device_info}")
            return True
        else:
            _LOGGER.error(f"No model available for {device_info}")
            self._connection_error = ModbusTCPErrorType.UNSUPPORTED_MODEL
            return False
    
    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
        
    async def _request_datapoint_data(self, points: List[ModbusDatapoint]) -> List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES]]:
        kv:List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES]] = []
        for batch in self.batch_reads(points):
            data: List[int] | None = self._client.read_holding_registers(batch[0].read_address, len(batch))  # type: ignore
            if data is None:
                _LOGGER.error(f"Failed to read data for {batch[0].read_address}-{batch[-1].read_address}")
                continue
            for i, value in enumerate(data):
                kv.append((batch[i], value))
        return kv
    
    async def _request_setpoint_data(self, points: List[ModbusSetpoint]) -> List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]]:
        kv:List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]] = []
        for batch in self.batch_reads(points):
            data:List[int]|None = self._client.read_input_registers(batch[0].read_address, len(batch)) # type: ignore
            if data is None: 
                _LOGGER.error(f"Failed to read data for {batch[0].read_address}-{batch[-1].read_address}")
                continue
            for i, value in enumerate(data):
                kv.append((batch[i], value))
        return kv    
    
    DATAPOINT_TYPE = TypeVar('DATAPOINT_TYPE', bound=ModbusDatapoint|ModbusSetpoint)
    def batch_reads(self, points:Sequence[DATAPOINT_TYPE]) -> Generator[List[DATAPOINT_TYPE], None, None]:
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
            elif batch[-1].read_address is None: continue # just here to stop the compiler from complaining, never true.
            elif batch[-1].read_address + 1 == point.read_address:
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