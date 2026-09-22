import logging
from typing import Dict, List, Set, Tuple
from collections.abc import Callable

from .modbus_models import ( MODBUS_VALUE_TYPES, ModbusDeviceInfo, ModbusPointKey, ModbusDevice, ModbusDatapoint, ModbusDatapointKey, ModbusSetpoint, ModbusSetpointKey )

_LOGGER = logging.getLogger(__name__)


class ModbusNoModelLoadedError(RuntimeError):
    """Raised when the client is used before connect() has loaded a device model."""


class ModbusDeviceAdapter(ModbusDevice):
    _device_info: ModbusDeviceInfo

    def __init__(self):
        # Must be initialised here. Without it every accessor raised AttributeError instead of
        # reporting "no model loaded", which broke subscribing before connecting.
        self._loaded_model: ModbusDevice|None = None

    def load_device_model(self, device_info: ModbusDeviceInfo) -> None:
        self._device_info = device_info
        model_to_load = self._translate_to_model(device_info)
        if model_to_load == None:
            raise Exception("Invalid model")
        loaded_model = model_to_load(device_info)
        loaded_model.instantiate()
        self._loaded_model = loaded_model
     
    def _translate_to_model(self, device_info: ModbusDeviceInfo) -> Callable[[ModbusDeviceInfo], ModbusDevice]|None:
        """Translate the model to the correct device model, must be implemented in the subclass"""
        raise NotImplementedError("Method not implemented")

    def provides_model(self, device_info: ModbusDeviceInfo) -> bool:
        return self._translate_to_model(device_info) is not None
    
    #region ModbusDevice
    
    @property
    def has_model(self) -> bool:
        """True once a device model has been loaded. Never raises."""
        return getattr(self, "_loaded_model", None) is not None

    def _get_loaded_model(self) -> ModbusDevice:
        # getattr guards against a subclass __init__ that forgets super().__init__().
        loaded_model = getattr(self, "_loaded_model", None)
        if loaded_model is None:
            raise ModbusNoModelLoadedError(
                "No device model loaded. Call connect() before using the client.")
        return loaded_model

    def get_datapoint(self, key: ModbusDatapointKey) -> ModbusDatapoint|None:
        return self._get_loaded_model().get_datapoint(key)

    def get_datapoints_for_read(self) -> List[ModbusDatapoint]:
        return self._get_loaded_model().get_datapoints_for_read()

    def get_initial_datapoints_for_read(self) -> List[ModbusDatapoint]:
        return self._get_loaded_model().get_initial_datapoints_for_read()
    def get_initial_setpoints_for_read(self) -> List[ModbusSetpoint]:
        return self._get_loaded_model().get_initial_setpoints_for_read()

    @property
    def ready(self):
        loaded_model = getattr(self, "_loaded_model", None)
        return loaded_model is not None and loaded_model.ready
    @property
    def device_info(self): return self._get_loaded_model().device_info
    @property
    def manufacturer(self): return self._get_loaded_model().manufacturer
    @property
    def model_name(self): return self._get_loaded_model().model_name
    @property
    def max_request_length(self): return self._get_loaded_model().max_request_length
    
    def get_max_value(self, key: ModbusSetpointKey) -> float|int|None:
        return self._get_loaded_model().get_max_value(key)

    def get_min_value(self, key: ModbusSetpointKey) -> float|int|None:
        return self._get_loaded_model().get_min_value(key)

    def get_setpoint(self, key: ModbusSetpointKey) -> ModbusSetpoint|None:
        return self._get_loaded_model().get_setpoint(key)

    def get_setpoint_step(self, key: ModbusSetpointKey) -> float|int:
        return self._get_loaded_model().get_setpoint_step(key)

    def get_setpoints_for_read(self) -> List[ModbusSetpoint]:
        return self._get_loaded_model().get_setpoints_for_read()
    
    def get_unit_of_measure(self, key: ModbusPointKey) -> str|None:
        return self._get_loaded_model().get_unit_of_measure(key)
    
    def get_value(self, key: ModbusPointKey) -> MODBUS_VALUE_TYPES|None:
        return self._get_loaded_model().get_value(key)
   
    def get_values(self) -> Dict[ModbusPointKey, MODBUS_VALUE_TYPES|None]:
        return self._get_loaded_model().get_values()
    
    def get_keys(self) -> Set[ModbusPointKey]:
        return self._get_loaded_model().get_keys()

    def has_value(self, key: ModbusPointKey) -> bool:
        return self._get_loaded_model().has_value(key)

    def provides(self, key: ModbusPointKey) -> bool:
        return self._get_loaded_model().provides(key)

    def reads_always(self, key: ModbusPointKey) -> bool:
        return self._get_loaded_model().reads_always(key)

    def set_read(self, key: ModbusPointKey, read: bool) -> bool:
        return self._get_loaded_model().set_read(key, read)

    def set_values(self, kv: List[Tuple[ModbusPointKey, MODBUS_VALUE_TYPES|None]]) -> Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]:
        return self._get_loaded_model().set_values(kv)
    
    #endregion ModbusDevice