from collections.abc import Callable
from enum import auto
from src.modbus_event_connect import *

class ModbusTestDatapointKey(ModbusDatapointKey):
    MAJOR_VERSION = auto()
    
class ModbusTestSetpointKey(ModbusSetpointKey):
    MY_SETPOINT = auto()
    
class ModbusTestDevice(ModbusDeviceBase):
    def __init__(self, device_info: ModbusDeviceInfo):
        super().__init__(device_info)

        self._attr_manufacturer="TEST"
        self._attr_model_name="TEST"
        self._attr_datapoints = [
            ModbusDatapoint(key=ModbusTestDatapointKey.MAJOR_VERSION, read_address=1, divider=1, signed=True),
        ]
        self._attr_setpoints = [
            ModbusSetpoint(key=ModbusTestSetpointKey.MY_SETPOINT, read_address=1, write_address=1 ,divider=1, min=1, max=10, signed=True),
        ]

class ModbusTestDeviceAdapter(ModbusDeviceAdapter):

    def _translate_to_model(self, device_info: ModbusDeviceInfo) -> Callable[[ModbusDeviceInfo], ModbusDevice]|None:
        return ModbusTestDevice

class ModbusTestTCP(ModbusTCPEventConnect):
   _attr_adapter = ModbusTestDeviceAdapter()