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
        self._connection = MicroNabtoConnection()
    
    def is_connected(self, device_id:str|None=None): return self._connection.is_connected() and self._attr_adapter.ready
    def get_connection_error(self): return self._connection.get_connection_error()
    def get_discovered_devices(self): return self._connection.get_discovered_devices()
         
    async def connect(self, email:str, device_id:str, device_host:str|None=None, device_port:int|None=DEVICE_PORT, timeout:float = CONNECT_TIMEOUT) -> bool:
        device_info = await self._connection.connect(email, device_id, device_host, device_port, timeout)
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
        self._connection.stop_listening()
        
    async def _request_datapoint_data(self, points: List[ModbusDatapoint]) -> List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES]]:
        data = await self._connection.request_datapoint_data(points)
        kv:List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES]] = []
        for point, value in data.values():
            kv.append((point, value))
        return kv
    
    async def _request_setpoint_data(self, points: List[ModbusSetpoint]) -> List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]]:
        data = await self._connection.request_setpoint_data(points)
        kv:List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]] = []
        for point, value in data.values():
            kv.append((point, value))
        return kv