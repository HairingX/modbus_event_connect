# import logging
from collections.abc import Callable

#TODO: modify import for a module import when moving to own package
from src.modbus_event_connect import *
from .devices import *

# _LOGGER = logging.getLogger(__name__)

class WavinSentioDeviceAdapter(ModbusDeviceAdapter):

    def _translate_to_model(self, device_info: ModbusDeviceInfo) -> Callable[[ModbusDeviceInfo], ModbusDevice]|None:
        return WavinSentio

    def provides_model(self, device_info: ModbusDeviceInfo) -> bool:
        return self._translate_to_model(device_info) is not None


class WavinSentioConnect(MicroNabtoEventConnect):
   _attr_adapter = WavinSentioDeviceAdapter()