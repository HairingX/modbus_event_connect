from collections.abc import Callable
from enum import auto
from src.modbus_event_connect import *

class ModbusTestDatapointKey(ModbusDatapointKey):
    MAJOR_VERSION = auto()
    TEMPERATURE = auto()
    
class ModbusTestSetpointKey(ModbusSetpointKey):
    LOCATION_NAME = auto()
    DATETIME = auto()
    
class ModbusTestDevice(ModbusDeviceBase):
    def __init__(self, device_info: ModbusDeviceInfo):
        super().__init__(device_info)

        self._attr_manufacturer="TEST"
        self._attr_model_name="TEST"
        self._attr_version_keys = VersionInfoKeys(datapoint_major=ModbusTestDatapointKey.MAJOR_VERSION)
        self._attr_datapoints = [
            ModbusDatapoint(key=ModbusTestDatapointKey.MAJOR_VERSION, read_address=1, divider=1, signed=False),
            ModbusDatapoint(key=ModbusTestDatapointKey.TEMPERATURE, read_address=104, max=-1, divider=100, signed=True),
        ]
        self._attr_setpoints = [
            ModbusSetpoint(key=ModbusTestSetpointKey.LOCATION_NAME, read_address=10, read_length=16, signed=False, value_type=ModbusValueType.UTF8),
            ModbusSetpoint(key=ModbusTestSetpointKey.DATETIME,      read_address=28, read_length=2, signed=False),
        ]

class ModbusTestDeviceAdapter(ModbusDeviceAdapter):

    def _translate_to_model(self, device_info: ModbusDeviceInfo) -> Callable[[ModbusDeviceInfo], ModbusDevice]|None:
        return ModbusTestDevice

class ModbusTestTCP(ModbusTCPEventConnect):
   _attr_adapter = ModbusTestDeviceAdapter()