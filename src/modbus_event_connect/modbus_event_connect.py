import logging

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Tuple

from .modbus_models import MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES, ModbusDatapoint, ModbusPointKey, ModbusSetpoint
from .modbus_deviceadapter import ModbusDeviceAdapter

_LOGGER = logging.getLogger(__name__)
class ModbusEventConnect(ABC):
    _attr_adapter: ModbusDeviceAdapter
    _subscribers: Dict[ModbusPointKey, List[Callable[[ModbusPointKey, MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]]] = {}
    """Callable[old_value, new_value]"""
    
    @property
    @abstractmethod
    def is_connected(self) -> bool: 
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    async def _request_datapoint_data(self, points: List[ModbusDatapoint]) -> List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES|None]]:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    async def _request_setpoint_data(self, points: List[ModbusSetpoint]) -> List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES|None]]:
        raise NotImplementedError("Method not implemented")
        
    @property
    def device_info(self): return self._attr_adapter.device_info
    @property
    def manufacturer(self): return self._attr_adapter.manufacturer
    @property
    def model_name(self) -> str: return self._attr_adapter.model_name
    
    async def request_initial_data(self) -> None:
        """Request the current value of all points used in initialization, ex. version."""
        values:List[Tuple[Any, MODBUS_VALUE_TYPES|None]] = []
        datapoints = self._attr_adapter.get_initial_datapoints_for_read()
        if len(datapoints) > 0: 
            values.extend(await self._request_datapoint_data(datapoints))
        setpoints = self._attr_adapter.get_initial_setpoints_for_read()
        if len(setpoints) > 0: 
            values.extend(await self._request_setpoint_data(setpoints))
        self._set_values(values)
    
    async def request_datapoint_data(self) -> None:
        """Request the current value of all subscribed datapoints. All subscribers will be notified of the new value if changed."""
        points = self._attr_adapter.get_datapoints_for_read()
        if len(points) == 0: return
        self._set_values(await self._request_datapoint_data(points))
            
    async def request_setpoint_data(self) -> None:
        """Request the current value of all subscribed setpoints. All subscribers will be notified of the new value if changed."""
        points = self._attr_adapter.get_setpoints_for_read()
        if len(points) == 0: return
        self._set_values(await self._request_setpoint_data(points))
        
    def get_value(self, key: ModbusPointKey) -> MODBUS_VALUE_TYPES|None:
        return self._attr_adapter.get_value(key)
    
    def subscribe(self, key: ModbusPointKey, update_method: Callable[[ModbusPointKey, MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]):
        """
            Subscribe to a datapoint or setpoint value change.
            
            :param key: The key of the datapoint or setpoint to subscribe to.
            :param update_method: The method to call when the value changes. The Callable will receive the key, old_value and new_value as the inputs.
            """
        if key not in self._subscribers:
            self._subscribers[key] = []
            self._attr_adapter.set_read(key, True)
        self._subscribers[key].append(update_method)
        value = self._attr_adapter.get_value(key)
        if value is not None:
            update_method(key, None, value)
    
    def unsubscribe(self, key: ModbusPointKey, update_method: Callable[[ModbusPointKey, MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]):
        """Remove a subscription to a datapoint or setpoint value change."""
        subscribers = self._subscribers.get(key)
        if subscribers is None: return
        if update_method in subscribers:
            if len(subscribers) == 1:
                del self._subscribers[key]
                self._attr_adapter.set_read(key, False)
            else: 
                subscribers.remove(update_method)
    
    def _set_values(self, kv: List[Tuple[MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES|None]]) -> None:
        result = self._attr_adapter.set_values([(point.key, value) for point, value in kv])
        self._notify_subscribers(result)
    
    def _notify_subscribers(self, kv: Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]) -> None:
        for key, (old_value, new_value) in kv.items():
            subscribers = self._subscribers.get(key)
            if subscribers is None: return
            for subscriber in subscribers:
                subscriber(key, old_value, new_value)
                
    def _handle_invalid_address(self, point: ModbusDatapoint|ModbusSetpoint) -> None:
        _LOGGER.error(f"Failed to read data for '{point.key}', the address '{point.read_obj}:{point.read_address}{None if point.read_length == 1 or point.read_address is None else f'-{point.read_address+point.read_length}'}' is not available. Inform developer that the device '{self.device_info}' has this error.")
        self._attr_adapter.set_read(point.key, False, force=True)