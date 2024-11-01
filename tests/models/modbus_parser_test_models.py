from enum import auto
from src.modbus_event_connect import *

class ModbusTestDatapointKey(ModbusDatapointKey):
    TEST = auto()
    
class ModbusTestSetpointKey(ModbusSetpointKey):
    TEST = auto()
    
class PointFactory:
    @staticmethod
    def create_setpoint(*, read_length: int = 1, max: int = 0, divider: int = 1, signed: bool = False, value_type:str = ModbusValueTypes.AUTO) -> ModbusSetpoint:
        point = ModbusSetpoint(key=ModbusTestSetpointKey.TEST, read_address=1, read_length=read_length, max=max, divider=divider, signed=signed, value_type=value_type)
        if point.max == 0: point.max = (1 << (point.read_length * 2 * 8)) - 1
        if point.max == -1: point.max = (1 << (point.read_length * 2 * 8)) - 2
        return point
