import logging

from ..modbus_event_connect import *
from ..modbus_models import *
from .micro_nabto_connection import CONNECT_TIMEOUT, DEVICE_PORT,  MicroNabtoConnection, MicroNabtoConnectionErrorType

_LOGGER = logging.getLogger(__name__)

class MicroNabtoErrorType(StrEnum):
    #connection errors
    AUTHENTICATION_ERROR = MicroNabtoConnectionErrorType.AUTHENTICATION_ERROR
    INVALID_ACTION = MicroNabtoConnectionErrorType.INVALID_ACTION
    LISTEN_THREAD_CLOSED = MicroNabtoConnectionErrorType.LISTEN_THREAD_CLOSED
    SOCKET_CLOSED = MicroNabtoConnectionErrorType.SOCKET_CLOSED
    TIMEOUT = MicroNabtoConnectionErrorType.TIMEOUT
    #local errors
    UNSUPPORTED_MODEL = "unsupported_model"
    
class MicroNabtoEventConnect(ModbusEventConnect):
    
    def __init__(self) -> None:
        self._client = MicroNabtoConnection()
    
    @property
    def is_connected(self): return self._client.is_connected and self._attr_adapter.ready
    @property
    def last_error(self): return self._client.last_error
    @property
    def discovered_devices(self): return self._client.discovered_devices
         
    async def connect(self, email:str, device_id:str, device_host:str|None=None, device_port:int|None=DEVICE_PORT, timeout:float = CONNECT_TIMEOUT) -> bool:
        device_info = await self._client.connect(email, device_id, device_host, device_port, timeout)
        if device_info is None:
            return False
                
        if self._attr_adapter.provides_model(device_info):
            _LOGGER.debug(f"Going to load model")
            self._attr_adapter.load_device_model(device_info)
            _LOGGER.debug(f"Loaded model for {self._attr_adapter.model_name} - {device_info}")
            await self.request_initial_data()
            _LOGGER.debug(f"Fetched initial data")
            return True
        else:
            _LOGGER.error(f"No model available for {device_info}")
            self._connection_error = MicroNabtoErrorType.UNSUPPORTED_MODEL
            return False
    
    def stop(self) -> None:
        self._client.stop_listening()
        
    async def _request_datapoint_data(self, points: List[ModbusDatapoint]) -> List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES|None]]:
        data = await self._client.request_datapoint_data(points)
        kv:List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES|None]] = []
        if data is not None: 
            for point, value in data.values():
                kv.append((point, value))
        else:
            last_error = self._client.last_error 
            if last_error == MicroNabtoConnectionErrorType.INVALID_ADDRESS: 
                if len(points) == 1:
                    point = points[0]
                    kv.append((point, None))
                    self._handle_invalid_address(point)
                else:
                    _LOGGER.warning(f"Device failed to read {len(points)} datapoints. Some datapoints may not be available. Checking each datapoint individually.")
                    for point in points:
                        data = await self._client.request_datapoint_data([point])
                        if data is not None:
                            kv.append(data[point.key])
                        elif self._client.last_error == MicroNabtoConnectionErrorType.INVALID_ADDRESS:
                            kv.append((point, None))
                            self._handle_invalid_address(point)
            else: 
                _LOGGER.error(f"Failed to read data for {[point.key for point in points]}")
        return kv
    
    def _handle_invalid_address(self, point: ModbusDatapoint|ModbusSetpoint) -> None:
        _LOGGER.error(f"Failed to read data for '{point.key}', the address '{point.read_obj}:{point.read_address}' is not available. Inform developer that the device '{self.device_info}' has this error.")
        self._attr_adapter.set_read(point.key, False, force=True)
    
    async def _request_setpoint_data(self, points: List[ModbusSetpoint]) -> List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES|None]]:
        data = await self._client.request_setpoint_data(points)
        kv:List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES|None]] = []
        if data is not None: 
            for point, value in data.values():
                kv.append((point, value))
        else:
            last_error = self._client.last_error 
            if last_error == MicroNabtoConnectionErrorType.INVALID_ADDRESS: 
                if len(points) == 1:
                    point = points[0]
                    kv.append((point, None))
                    self._handle_invalid_address(point)
                else:
                    _LOGGER.warning(f"Device failed to read {len(points)} setpoints. Some setpoints may not be available. Checking each setpoint individually.")
                    for point in points:
                        data = await self._client.request_setpoint_data([point])
                        if data is not None:
                            kv.append(data[point.key])
                        elif self._client.last_error == MicroNabtoConnectionErrorType.INVALID_ADDRESS:
                            kv.append((point, None))
                            self._handle_invalid_address(point)
            else: 
                _LOGGER.error(f"Failed to read data for {[point.key for point in points]}")
        return kv