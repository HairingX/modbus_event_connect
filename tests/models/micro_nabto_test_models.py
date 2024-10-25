import logging
from collections.abc import Callable
from src.modbus_event_connect import *

_LOGGER = logging.getLogger(__name__)

class ModbusTestDatapointKey(ModbusDatapointKey):
    MAJOR_VERSION = "major_version"
    
class ModbusTestDevice(ModbusDeviceBase):
    def __init__(self, device_info: ModbusDeviceInfo):
        super().__init__(device_info)

        self._attr_manufacturer="TEST"
        self._attr_model_name="TEST"
        self._attr_datapoints = [
            ModbusDatapoint(key=ModbusTestDatapointKey.MAJOR_VERSION, read_address=1, divider=1, signed=True),
        ]
        self._attr_setpoints = []

class ModbusTestDeviceAdapter(ModbusDeviceAdapter):

    def _translate_to_model(self, device_info: ModbusDeviceInfo) -> Callable[[ModbusDeviceInfo], ModbusDevice]|None:
        if isinstance(device_info, MicroNabtoModbusDeviceInfo):
            _LOGGER.debug(f"Device info has micro nabto data: {device_info}")
        return ModbusTestDevice

class ModbusTestMicroNabto(MicroNabtoEventConnect):
   _attr_adapter = ModbusTestDeviceAdapter()