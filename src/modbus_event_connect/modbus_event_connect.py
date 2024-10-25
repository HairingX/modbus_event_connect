
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Callable, Dict, List, Tuple

from .modbus_models import MODBUS_VALUE_TYPES, ModbusDatapoint, ModbusDatapointKey, ModbusSetpoint, ModbusSetpointKey
from .modbus_deviceadapter import ModbusDeviceAdapter

class ModbusEventConnect(ABC):
    _attr_adapter: ModbusDeviceAdapter
    _subscribers: Dict[ModbusDatapointKey|ModbusSetpointKey, List[Callable[[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]]] = {}
    """Callable[old_value, new_value]"""
    
    @abstractmethod
    def is_connected(self) -> bool: 
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def _request_datapoint_data(self, points: List[ModbusDatapoint]) -> AsyncGenerator[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES], None]:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def _request_setpoint_data(self, points: List[ModbusSetpoint]) -> AsyncGenerator[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES], None]:
        raise NotImplementedError("Method not implemented")
        
    @property
    def version(self): return self._attr_adapter.version
    @property
    def device_host(self): return self._attr_adapter.device_host
    @property
    def device_id(self): return self._attr_adapter.device_id
    @property
    def device_port(self): return self._attr_adapter.device_port
    @property
    def identification(self): return self._attr_adapter.identification
    @property
    def manufacturer(self): return self._attr_adapter.manufacturer
    @property
    def model_name(self) -> str: return self._attr_adapter.model_name
    
    async def request_datapoint_data(self) -> None:
        """Request the current value of all subscribed datapoints. All subscribers will be notified of the new value if changed."""
        points = self._attr_adapter.get_datapoints_for_read()
        async for point, value in self._request_datapoint_data(points):
            self._set_value(point.key, value)
            
    async def request_setpoint_data(self) -> None:
        """Request the current value of all subscribed setpoints. All subscribers will be notified of the new value if changed."""
        points = self._attr_adapter.get_setpoints_for_read()
        async for point, value in self._request_setpoint_data(points):
            self._set_value(point.key, value)
    
    def subscribe(self, key: ModbusDatapointKey|ModbusSetpointKey, update_method: Callable[[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]):
        """
            Subscribe to a datapoint or setpoint value change.
            
            :param key: The key of the datapoint or setpoint to subscribe to.
            :param update_method: The method to call when the value changes. The Callable will receive old_value and new_value as the two inputs.
            """
        if key not in self._subscribers:
            self._subscribers[key] = []
            self._attr_adapter.set_read(key, True)
        self._subscribers[key].append(update_method)
        value = self._attr_adapter.get_value(key)
        if value is not None:
            update_method(None, value)
    
    def unsubscribe(self, key: ModbusDatapointKey|ModbusSetpointKey, update_method: Callable[[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]):
        """Remove a subscription to a datapoint or setpoint value change."""
        subscribers = self._subscribers.get(key)
        if subscribers is None: return
        if update_method in subscribers:
            if len(subscribers) == 1:
                del self._subscribers[key]
                self._attr_adapter.set_read(key, False)
            else: 
                subscribers.remove(update_method)
    
    def _set_value(self, key: ModbusDatapointKey|ModbusSetpointKey, value: MODBUS_VALUE_TYPES):
        oldval, newval = self._attr_adapter.set_value(key, value)
        self._notify_subscribers(key, oldval, newval)
    
    def _notify_subscribers(self, key: ModbusDatapointKey|ModbusSetpointKey, old_value: MODBUS_VALUE_TYPES|None, new_value: MODBUS_VALUE_TYPES|None):
        subscribers = self._subscribers.get(key)
        if subscribers is None: return
        for subscriber in subscribers:
            subscriber(old_value, new_value)