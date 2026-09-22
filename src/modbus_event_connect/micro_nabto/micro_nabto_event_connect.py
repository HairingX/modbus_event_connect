import logging


from ..modbus_event_connect import *
from ..modbus_models import *
from .micro_nabto_connection import CONNECT_TIMEOUT, DEVICE_PORT,  MicroNabtoConnection, MicroNabtoConnectionErrorType, MicroNabtoModbusDeviceInfo

_LOGGER = logging.getLogger(__name__)


def _identify(device_info: ModbusDeviceInfo) -> str:
    """
    Describe a device by what it is, never by where it lives.

    These strings reach a consumer's log, and logs get pasted into public bug reports. The
    host and device id used to be in them, which tells a reader the shape of someone's home
    network. Model and hardware numbers are what a developer needs anyway.
    """
    if isinstance(device_info, MicroNabtoModbusDeviceInfo):
        return (f"device model {device_info.device_model}, "
                f"slave model {device_info.slave_device_model}")
    return f"{device_info.manufacturer} {device_info.model_name}".strip() or "unknown device"

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
            _LOGGER.debug(f"Loaded model for {self._attr_adapter.model_name} - {_identify(device_info)}")
            await self.request_initial_data()
            _LOGGER.debug(f"Fetched initial data")
            return True
        else:
            _LOGGER.error(f"No model available for {_identify(device_info)}")
            self._connection_error = MicroNabtoErrorType.UNSUPPORTED_MODEL
            return False
    
    def stop(self) -> None:
        self._client.stop_listening()
        
    async def _request_datapoint_read(self, points: List[ModbusDatapoint]) -> List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES|None]]:
        data = await self._client.request_datapoint_read(points)
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
                        data = await self._client.request_datapoint_read([point])
                        if data is not None:
                            kv.append(data[point.key])
                        elif self._client.last_error == MicroNabtoConnectionErrorType.INVALID_ADDRESS:
                            kv.append((point, None))
                            self._handle_invalid_address(point)
            else: 
                _LOGGER.error(f"Failed to read data for {[point.key for point in points]}")
        return kv
    
    async def _request_setpoint_read(self, points: List[ModbusSetpoint]) -> List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES|None]]:
        data = await self._client.request_setpoint_read(points)
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
                        data = await self._client.request_setpoint_read([point])
                        if data is not None:
                            kv.append(data[point.key])
                        elif self._client.last_error == MicroNabtoConnectionErrorType.INVALID_ADDRESS:
                            kv.append((point, None))
                            self._handle_invalid_address(point)
            else: 
                _LOGGER.error(f"Failed to read data for {[point.key for point in points]}")
        return kv
    
    def _handle_invalid_address(self, point: ModbusDatapoint|ModbusSetpoint) -> None:
        # Identify the device by what it is, never by where it lives: this line reaches a
        # consumer's log, and logs get pasted into public bug reports. The host was in here.
        _LOGGER.info(f"'{point.key}' is not available on this device "
                     f"(address {point.read_obj}:{point.read_address}); it will not be read "
                     f"again. {_identify(self.device_info)}")
        self.set_available(point.key, False,
                           reason=f"address {point.read_obj}:{point.read_address} not available")
    
    def _request_setpoint_writes(self, point_values: Sequence[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]]) -> bool:
        pv = list[Tuple[ModbusSetpoint, List[int]]]()
        for point, value in point_values:
            parsed_value = ModbusParser.value_to_values(value, point)
            if parsed_value is None: continue
            pv.append((point, parsed_value))
        success = self._client.request_setpoint_writes(pv)
        return success