from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Dict, List, NotRequired, Optional, Tuple, TypedDict

MODBUS_VALUE_TYPES = float|int|str

class ModbusPointKey(StrEnum):
    pass

class ModbusDatapointKey(ModbusPointKey):
    pass

class ModbusSetpointKey(ModbusPointKey):
    pass

class ModbusVersionPointKey(ModbusDatapointKey):
    """Default datapoint keys, which is used to identify the version of the device"""
    MAJOR_VERSION = "major_version"
    MINOR_VERSION = "minor_version"
    PATCH_VERSION = "patch_version"
    
class UOM:
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"
    MONTHS = "months"
    YEARS = "years"
    CELSIUS = "celsius"
    BOOL = "bool"
    BITMASK = "bitmask"
    PPM = "ppm"
    """CONCENTRATION PARTS PER MILLION"""
    RPM = "rpm"
    """REVOLUTIONS PER MINUTE"""
    # INT = "int"
    # FLOAT = "float"
    PCT = "percent"
    TEXT = "text"
    UNKNOWN = None
    
class ModbusPointExtras(TypedDict):
    unit_of_measurement: NotRequired[str|None]
    """Unit of measurement for the value, UOM class contains the standard units, defaults to None"""
    always_read: NotRequired[bool|None]
    """Indication if the point should always be read from the device. If not True, the point will only be read if explicitly requested"""
    
@dataclass(kw_only=True)
class ModbusDatapoint:
    key: ModbusDatapointKey
    extra: Optional[ModbusPointExtras|None] = None
    #read
    read_address: int
    signed: bool
    """indication of the data being signed or unsigned (default=True)"""
    divider: Optional[int|None] = None
    """Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    offset: Optional[int|None] = None
    """Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    read_modifier: Optional[Callable[[float|int], float|int]|None] = None
    """Modifier applied to value after it has been parsed by the system. can be used to alter hours to days etc. or round floating values
    Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    read_obj: Optional[int|None] = None
    """default is 0"""

@dataclass(kw_only=True)
class ModbusSetpoint:
    key: ModbusSetpointKey
    extra: Optional[ModbusPointExtras|None] = None
    #read
    read_address: Optional[int|None] = None
    signed: bool
    """indication of the data being signed or unsigned (default=True)"""
    divider: Optional[int|None] = None
    """Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    offset: Optional[int|None] = None
    """Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    read_modifier: Optional[Callable[[float|int], float|int]|None] = None
    """Modifier applied to value after it has been parsed by the system. can be used to alter hours to days etc. or round floating values
    Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    read_obj: Optional[int|None] = None
    """default is 0"""
    #write
    max: int
    """max value in the register"""
    min: int
    """min value in the register"""
    write_address: int|None = None
    step: Optional[int|None] = None
    """step size in register value, if unset will default to the divider"""
    write_modifier: Optional[Callable[[float|int], float|int]|None] = None
    """Modifier applied to value before it has been parsed back to register type. can be used to alter hours to days etc. or round floating values"""
    write_obj: Optional[int|None] = None
    """default is 0"""

    
class ModbusDatapointData:
    point: ModbusDatapoint
    read: bool
    value: MODBUS_VALUE_TYPES|None = None
    unit_of_measurement: str|None = None
    def __init__(self, point: ModbusDatapoint):
        self.point = point
        self.read = point.extra is not None and "always_read" in point.extra and point.extra["always_read"] == True
        self.unit_of_measurement = point.extra["unit_of_measurement"] if point.extra is not None and "unit_of_measurement" in point.extra else None


class ModbusSetpointData:
    point: ModbusSetpoint
    read: bool
    value: MODBUS_VALUE_TYPES|None = None
    unit_of_measurement: str|None = None
    def __init__(self, point: ModbusSetpoint):
        self.point = point
        self.read = point.extra is not None and "always_read" in point.extra and point.extra["always_read"] == True
        self.unit_of_measurement = point.extra["unit_of_measurement"] if point.extra is not None and "unit_of_measurement" in point.extra else None

@dataclass()
class VersionInfo:
    major: int
    minor: int
    patch: int
    
@dataclass(kw_only=True)
class ModbusDeviceIdenfication:
    vendor_name: str|None
    product_code: str|None
    major_minor_revision: str|None
    vendor_url: str|None
    product_name: str|None
    model_name: str|None
    user_application_name: str|None
    
@dataclass(kw_only=True)
class ModbusDeviceInfo:
    device_id: str
    device_host: str
    device_port: int
    version: VersionInfo
    identification: ModbusDeviceIdenfication|None

class ModbusDevice(ABC):
    _ready: bool = False
    _device_info: ModbusDeviceInfo
    
    def __init__(self, device_info: ModbusDeviceInfo) -> None:
        self._device_info = device_info
    
    def instantiate(self, device_info: ModbusDeviceInfo) -> None:
        """
        Sets up the device for usage and raises errors if device has issues.
        Raises:
            ValueError: If the an attribute is not set.
        """
        self._ready = True
    
    @property
    def ready(self) -> bool: return self._ready
    @property
    def device_id(self) -> str: return self._device_info.device_id
    @property
    def device_host(self) -> str: return self._device_info.device_host
    @property
    def device_port(self) -> int: return self._device_info.device_port
    @property
    def identification(self) -> ModbusDeviceIdenfication|None: return self._device_info.identification
    @property
    def version(self) -> VersionInfo: return self._device_info.version
    @property
    @abstractmethod
    def manufacturer(self) -> str:
        raise NotImplementedError("Method not implemented")
    @property
    @abstractmethod
    def model_name(self) -> str:
        raise NotImplementedError("Method not implemented")
    
    @abstractmethod
    def get_datapoint(self, key: ModbusDatapointKey) -> ModbusDatapoint|None:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_datapoints_for_read(self) -> List[ModbusDatapoint]:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_max_value(self, key: ModbusSetpointKey) -> float|int|None:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_min_value(self, key: ModbusSetpointKey) -> float|int|None:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_setpoint(self, key: ModbusSetpointKey) -> ModbusSetpoint|None:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_setpoint_step(self, key: ModbusSetpointKey) -> float|int:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_setpoints_for_read(self) -> List[ModbusSetpoint]:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_unit_of_measure(self, key: ModbusPointKey) -> str|None:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_value(self, key: ModbusPointKey) -> MODBUS_VALUE_TYPES|None:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_values(self) -> Dict[ModbusPointKey, MODBUS_VALUE_TYPES|None]:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def has_value(self, key: ModbusPointKey) -> bool:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def provides(self, key: ModbusPointKey) -> bool:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def set_read(self, key: ModbusPointKey, read: bool) -> bool:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def set_values(self, kv: List[Tuple[ModbusPointKey, MODBUS_VALUE_TYPES]]) -> Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]:
        """Sets the values for the keys and returns a list with the old and new values"""
        raise NotImplementedError("Method not implemented")
    
class ModbusDeviceBase(ModbusDevice):
    _attr_manufacturer:str = ""
    """Manufacturer of the device. Must be assigned in the __init__ method"""
    _attr_model_name:str = ""
    """Model name of the device. Must be assigned in the __init__ method"""
    _attr_datapoints: List[ModbusDatapoint]
    """Datapoints for the device. Must be assigned in the __init__ method"""
    _attr_setpoints: List[ModbusSetpoint]
    """Setpoints for the device. Must be assigned in the __init__ method"""
    _attr_default_extras = dict[ModbusPointKey, ModbusPointExtras]()
    """Default extras for the device. Can be assigned in the __init__ method"""
    
    _datapoints = dict[ModbusDatapointKey, ModbusDatapointData]()
    _setpoints = dict[ModbusSetpointKey, ModbusSetpointData]()

    def __init__(self, device_info: ModbusDeviceInfo) -> None:
        super().__init__(device_info)

    @property
    def manufacturer(self) -> str:
        return self._attr_manufacturer
    @property
    def model_name(self) -> str:
        return self._attr_model_name
    
    def instantiate(self, device_info: ModbusDeviceInfo) -> None:
        if not self._attr_manufacturer:
            raise ValueError("Manufacturer not set")
        if not self._attr_model_name:
            raise ValueError("Model name not set")
        if not hasattr(self, '_attr_datapoints'):
            raise ValueError("Datapoints not set")
        if not hasattr(self, '_attr_setpoints'):
            raise ValueError("Setpoints not set")
        
        for point in self._attr_datapoints:
            point.extra = point.extra or self._attr_default_extras.get(point.key)
        self._datapoints = {point.key: ModbusDatapointData(point) for point in self._attr_datapoints}
        
        for point in self._attr_setpoints:
            point.extra = point.extra or self._attr_default_extras.get(point.key)
        self._setpoints = {point.key: ModbusSetpointData(point) for point in self._attr_setpoints}
    
    def get_datapoint(self, key: ModbusDatapointKey) -> ModbusDatapoint | None:
        data = self._datapoints.get(key)
        return data.point if data is not None else None
    
    def get_datapoints_for_read(self) -> List[ModbusDatapoint]:
        return [value.point for value in self._datapoints.values() if value.read]

    def get_max_value(self, key: ModbusSetpointKey) -> float | int | None:
        if self.provides(key):
            point = self._setpoints[key].point
            return ModbusParser.parse_from_modbus_value(point=point, value=point.max)
        return None

    def get_min_value(self, key: ModbusSetpointKey) -> float | int | None:
        if self.provides(key):
            point = self._setpoints[key].point
            return ModbusParser.parse_from_modbus_value(point=point, value=point.min)
        return None

    def get_setpoint(self, key: ModbusSetpointKey) -> ModbusSetpoint | None:
        data = self._setpoints.get(key)
        return data.point if data is not None else None
    
    def get_setpoint_step(self, key: ModbusSetpointKey) -> float|int:
        data = self._setpoints.get(key)
        if data is not None:
            divider = ModbusParser.get_point_divider(data.point)    
            step = ModbusParser.get_point_step(data.point) 
            if divider > 1: return step / divider
            return step
        return 1
    
    def get_setpoints_for_read(self) -> List[ModbusSetpoint]:
        return [value.point for value in self._setpoints.values() if value.read]
    
    def get_unit_of_measure(self, key: ModbusPointKey) -> str | None:
        if isinstance(key, ModbusDatapointKey):
            data = self._datapoints.get(key)
            if data is not None:
                return data.unit_of_measurement
        elif isinstance(key, ModbusSetpointKey):
            data = self._setpoints.get(key)
            if data is not None:
                return data.unit_of_measurement
        return None

    def get_value(self, key: ModbusPointKey) -> MODBUS_VALUE_TYPES|None:
        if isinstance(key, ModbusDatapointKey):
            point = self._datapoints.get(key)
            if point is not None:
                return point.value
        elif isinstance(key, ModbusSetpointKey):
            point = self._setpoints.get(key)
            if point is not None:
                return point.value
        return None

    def get_values(self) -> Dict[ModbusPointKey, MODBUS_VALUE_TYPES|None]:
        datapoints = {key: value.value for key, value in self._datapoints.items() if value.read}
        setpoints = {key: value.value for key, value in self._setpoints.items() if value.read}
        return {**datapoints, **setpoints}
    
    def has_value(self, key: ModbusPointKey) -> bool:
        if isinstance(key, ModbusDatapointKey):
            return key in self._datapoints
        elif isinstance(key, ModbusSetpointKey):
            return key in self._setpoints
        return False

    def provides(self, key: ModbusPointKey) -> bool:
        return key in self._datapoints or key in self._setpoints

    def set_read(self, key: ModbusPointKey, read: bool) -> bool:
        """
        Sets the read state for the point. Returns the new read state.
        If the device sets the always_read to True in the configuration for the key, the read state can never be set to False.
        """
        if key in self._datapoints:
            self._datapoints[key].read = read
            return True
        elif key in self._setpoints:
            data = self._setpoints[key]
            if data.point.read_address is not None:
                data.read = read
                return True
        return False
    
    def _set_value(self, key: ModbusPointKey, value: MODBUS_VALUE_TYPES) -> Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]:
        old_value:MODBUS_VALUE_TYPES|None = None
        assigned_value:MODBUS_VALUE_TYPES|None = None
        if key in self._datapoints:
            data = self._datapoints.get(key)
            if data is not None:
                old_value = data.value
                assigned_value = data.value = value
        elif key in self._setpoints:
            data = self._setpoints.get(key)
            if data is not None:
                assigned_value = data.value = value
        return (old_value, assigned_value)
    
    #TODO: Implement this method, to allow us to check version changes in a single go, istead of 3 individual events
    #make sure the vsion change can be overriden by the device, to allow for custom version handling. Ex. add or remove features based on version.
    def set_values(self, kv: List[Tuple[ModbusPointKey, MODBUS_VALUE_TYPES]]) -> Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]:
        result = dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]()
        for key, value in kv:
            old_value, new_value = self._set_value(key, value)
            result[key] = (old_value, new_value)
        
        self._update_if_version_changed(result)
        return result
    
    def _update_if_version_changed(self, valuesset:Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]) -> None:
        major = valuesset.get(ModbusVersionPointKey.MAJOR_VERSION)
        minor = valuesset.get(ModbusVersionPointKey.MINOR_VERSION)
        patch = valuesset.get(ModbusVersionPointKey.PATCH_VERSION)
        
        if major is not None or minor is not None or patch is not None:
            major_val = major[1] if major is not None and major[1] is not None else 0
            minor_val = minor[1] if minor is not None and minor[1] is not None else 0
            patch_val = patch[1] if patch is not None and patch[1] is not None else 0
            self._device_info.version = VersionInfo(major=int(major_val), minor=int(minor_val), patch=int(patch_val))
            
        
class MODIFIER:
    @staticmethod
    def flip_bool(value:float|int) -> float|int:
        """Flips the true/false state 
        - 1 -> 0
        - 0 -> 1"""
        return 1-value
    
    @staticmethod
    def seconds_to_minutes(value:float|int) -> float|int:
        return round(value/60)
    
    @staticmethod
    def hours_to_days(value:float|int) -> float|int:
        return round(value/24)
    
class ModbusParser:
    @staticmethod
    def parse_to_modbus_value(point:ModbusSetpoint, value: float|int) -> int:
        divider = ModbusParser.get_point_divider(point)
        offset = ModbusParser.get_point_offset(point)
        modifier = ModbusParser.get_point_write_modifier(point)
        new_value:float|int = value
        if modifier is not None: new_value = modifier(new_value)
        if divider > 1: new_value *= divider
        if offset != 0: new_value -= offset 
        return int(new_value) #cast to int, modbus writes only accept an int

    @staticmethod
    def parse_from_modbus_value(point:ModbusDatapoint|ModbusSetpoint, value: int) -> float|int:
        divider = ModbusParser.get_point_divider(point)
        offset = ModbusParser.get_point_offset(point)
        modifier = ModbusParser.get_point_read_modifier(point)
        new_value:float|int = value
        if offset != 0: new_value += offset 
        if divider > 1: new_value /= divider
        if modifier is not None: new_value = modifier(new_value)
        return new_value

    @staticmethod
    def get_point_divider(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        return 1 if point.divider is None else point.divider
    @staticmethod
    def get_point_offset(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        return 0 if point.offset is None else point.offset
    @staticmethod
    def get_point_read_address(point:ModbusDatapoint|ModbusSetpoint) -> int|None: 
        return point.read_address
    @staticmethod
    def get_point_read_obj(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        return 0 if point.read_obj is None else point.read_obj
    @staticmethod
    def get_point_write_address(point:ModbusSetpoint) -> int|None: 
        return point.write_address
    @staticmethod
    def get_point_write_obj(point:ModbusSetpoint) -> int: 
        return 0 if point.write_obj is None else point.write_obj
    @staticmethod
    def get_point_signed(point:ModbusDatapoint|ModbusSetpoint) -> bool: 
        return point.signed
    @staticmethod
    def get_point_step(point:ModbusSetpoint) -> int: 
        return 1 if point.step is None else point.step
    @staticmethod
    def get_point_max(point:ModbusSetpoint) -> int: 
        return point.max
    @staticmethod
    def get_point_min(point:ModbusSetpoint) -> int: 
        return point.min
    @staticmethod
    def get_point_read_modifier(point:ModbusDatapoint|ModbusSetpoint) -> Callable[[float|int], float|int]|None: 
        return None if point.read_modifier is None else point.read_modifier
    @staticmethod
    def get_point_write_modifier(point: ModbusSetpoint) -> Callable[[float|int], float|int]|None: 
        return None if point.write_modifier is None else point.write_modifier