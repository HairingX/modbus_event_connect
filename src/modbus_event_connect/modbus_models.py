from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import reduce
from typing import Dict, List, NotRequired, Optional, Set, Tuple, TypeVar, TypedDict

from .constants import *

class ModbusPointKey(StrEnum):
    pass
class ModbusDatapointKey(ModbusPointKey):
    """
    Datapoint keys, which is used to identify the datapoints. 
    
    Assign all keys 'auto()' ex. 'MY_KEY = auto()'
    """
    #name:  The name of the member being defined (e.g. ‘RED’).
    #start: The start value for the Enum; the default is 1.
    #count: The number of members currently defined, not including this one.
    #last_values: A list of the previous values.
    @staticmethod
    def _generate_next_value_(name:str, start:int, count:int, last_values:List[str]) -> str:
        return f"datapoint_{name.lower()}"

class ModbusSetpointKey(ModbusPointKey):
    """
    Setpoint keys, which is used to identify the setpoints. 
    
    Assign all keys 'auto()' ex. 'MY_KEY = auto()'
    """
    @staticmethod
    def _generate_next_value_(name:str, start:int, count:int, last_values:List[str]) -> str:
        return f"setpoint_{name.lower()}"

class ModbusPointExtras(TypedDict):
    unit_of_measurement: NotRequired[str|None]
    """Unit of measurement for the value, UOM class contains the standard units, defaults to None"""
    read: NotRequired[Read|None]
    """Flags for the read operation, defaults to REQUESTED"""

@dataclass(kw_only=True)
class ModbusDatapoint:
    key: ModbusDatapointKey
    extra: Optional[ModbusPointExtras] = None
    #read
    read_address: int
    read_length: int = 1
    """Number of registers the point is using. Defaults to 1"""
    signed: bool = False
    """indication of the data being signed or unsigned (positive only). Defaults to False"""
    divider: int = 1
    """Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    max: int = 0
    """
    max value in the register. If the value is greater than this, it will be seen as invalid. 
    Defaults to read_length * 16bit (signed max value).
    
    If set to -1, the value will be set to (read_length * 2^16) - 1. the max value for the value will be seen as invalid.
    """
    min: int = 0
    """min value in the register. If the value is less than this, it will be seen as invalid."""
    offset: int = 0
    """Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    read_modifier: Optional[Callable[[float|int], float|int]] = None
    """Modifier applied to value after it has been parsed by the system. can be used to alter hours to days etc. or round floating values
    Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    read_obj: int = 0
    """default is 0"""
    value_type: str = ModbusValueType.AUTO
    """The type of value the point is using"""

@dataclass(kw_only=True)
class ModbusSetpoint:
    key: ModbusSetpointKey
    extra: Optional[ModbusPointExtras] = None
    #read
    read_address: Optional[int] = None
    read_length: int = 1
    """Number of registers the point is using. Defaults to 1"""
    signed: bool = False
    """indication of the data being signed or unsigned (positive only). Defaults to False"""
    divider: int = 1
    """Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    max: int = 0
    """
    max value in the register. If the value is greater than this, it will be seen as invalid. 
    Defaults to read_length * 16bit (signed max value).
    
    If set to -1, the value will be set to (read_length * 2^16) - 1. the max value for the value will be seen as invalid.
    """
    min: int = 0
    """min value in the register. If the value is less than this, it will be seen as invalid"""
    offset: int = 0
    """Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    read_modifier: Optional[Callable[[float|int], float|int]] = None
    """Modifier applied to value after it has been parsed by the system. can be used to alter hours to days etc. or round floating values
    Applied to the register value in the order: 1: divider, 2: offset, 3: modifier"""
    read_obj: int = 0
    """default is 0"""
    #write
    value_type: str = ModbusValueType.AUTO
    """The type of value the point is using"""
    write_address: int|None = None
    write_length: int = 1
    """Number of registers the point is using. Defaults to 1"""
    step: Optional[int] = None
    """step size in register value, if unset will default to the divider"""
    write_modifier: Optional[Callable[[float|int], float|int]] = None
    """Modifier applied to value before it has been parsed back to register type. can be used to alter hours to days etc. or round floating values"""
    write_obj: int = 0
    """default is 0"""

    
class ModbusDatapointData:
    point: ModbusDatapoint
    read: bool = False
    read_flags: Read = Read.REQUESTED
    value: MODBUS_VALUE_TYPES|None = None
    unit_of_measurement: str|None = None
    def __init__(self, point: ModbusDatapoint):
        self.point = point
        self.read_flags = self.read_flags if point.extra is None or "read" not in point.extra or point.extra["read"] is None else point.extra["read"]
        self.unit_of_measurement = point.extra["unit_of_measurement"] if point.extra is not None and "unit_of_measurement" in point.extra else None


class ModbusSetpointData:
    point: ModbusSetpoint
    read: bool = False
    read_flags: Read = Read.REQUESTED
    value: MODBUS_VALUE_TYPES|None = None
    unit_of_measurement: str|None = None
    def __init__(self, point: ModbusSetpoint):
        self.point = point
        self.read_flags = self.read_flags if point.extra is None or "read" not in point.extra or point.extra["read"] is None else point.extra["read"]
        self.unit_of_measurement = point.extra["unit_of_measurement"] if point.extra is not None and "unit_of_measurement" in point.extra else None
    
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
class VersionInfo:
    datapoint_major: int = 0
    """Datapoint address space, major version number. Often used for backwards compatible changes"""
    datapoint_minor: int = 0
    """Datapoint address space, minor version number. Often used for backwards compatible changes"""
    datapoint_patch: int = 0
    """Datapoint address space, patch version number. Often used for bug fixes"""
    hardware_major: int = 0
    """Hardware version, major version number"""
    hardware_minor: int = 0
    """Hardware version, minor version number"""
    setpoint_major: int = 0
    """Setpoint version, major version number. Often used for backwards compatible changes"""
    setpoint_minor: int = 0
    """Setpoint version, minor version number. Often used for backwards compatible changes"""
    setpoint_patch: int = 0
    """Setpoint version, patch version number. Often used for bug fixes"""
    software_major: int = 0
    """Software version, major version number. Often used for backwards compatible changes"""
    software_minor: int = 0
    """Software version, minor version number. Often used for backwards compatible changes"""
    software_patch: int = 0
    """Software version, patch version number. Often used for bug fixes"""

@dataclass(kw_only=True)
class VersionInfoKeys:
    datapoint_major: ModbusPointKey|None=None
    datapoint_minor: ModbusPointKey|None=None
    datapoint_patch: ModbusPointKey|None=None
    hardware_major: ModbusPointKey|None=None
    hardware_minor: ModbusPointKey|None=None
    setpoint_major: ModbusPointKey|None=None
    setpoint_minor: ModbusPointKey|None=None
    setpoint_patch: ModbusPointKey|None=None
    software_major: ModbusPointKey|None=None
    software_minor: ModbusPointKey|None=None
    software_patch: ModbusPointKey|None=None
    def to_set(self) -> Set[ModbusPointKey]:
        return set(key for key in [self.datapoint_major, self.datapoint_minor, self.datapoint_patch, 
                                self.hardware_major, self.hardware_minor, 
                                self.setpoint_major, self.setpoint_minor, self.setpoint_patch,
                                self.software_major, self.software_minor, self.software_patch
                                ] 
                if key is not None)

@dataclass(kw_only=True)
class ModbusDeviceInfo:
    device_id: str
    device_host: str
    device_port: int
    version: VersionInfo
    identification: ModbusDeviceIdenfication|None
    manufacturer:str = ""
    model_name:str = ""
    
MODBUS_POINT_TYPE = TypeVar('MODBUS_POINT_TYPE', bound=ModbusDatapoint|ModbusSetpoint)

class ModbusDevice(ABC):
    _ready: bool = False
    _device_info: ModbusDeviceInfo
    
    def __init__(self, device_info: ModbusDeviceInfo) -> None:
        self._device_info = device_info
    
    def instantiate(self) -> None:
        """
        Sets up the device for usage and raises errors if device has issues.
        Raises:
            ValueError: If the an attribute is not set.
        """
        self._ready = True
    
    @property
    def ready(self) -> bool: return self._ready
    @property
    def device_info(self) -> ModbusDeviceInfo: return self._device_info
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
    def get_initial_datapoints_for_read(self) -> List[ModbusDatapoint]:
        """Returns the initial datapoints to read during startup, these are normally the version datapoints"""
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def get_initial_setpoints_for_read(self) -> List[ModbusSetpoint]:
        """Returns the initial setpoints to read during startup, these are normally the version datapoints"""
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
    def set_read(self, key: ModbusPointKey, read: bool, *, force: bool=False) -> bool:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def set_values(self, kv: List[Tuple[ModbusPointKey, MODBUS_VALUE_TYPES|None]]) -> Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]:
        """Sets the values for the keys and returns a list with the old and new values"""
        raise NotImplementedError("Method not implemented")

class ModbusDeviceBase(ModbusDevice):
    _attr_datapoints: List[ModbusDatapoint]
    """Datapoints for the device. Must be assigned in the __init__ method"""
    _attr_default_extras = dict[ModbusPointKey, ModbusPointExtras]()
    """Default extras for the device. Can be assigned in the __init__ method"""
    _attr_manufacturer:str
    """Manufacturer of the device. Must be assigned in the __init__ method"""
    _attr_model_name:str
    """Model name of the device. Must be assigned in the __init__ method"""
    _attr_version_keys: VersionInfoKeys 
    """Keys used to get the version info"""
    _attr_setpoints: List[ModbusSetpoint]
    """Setpoints for the device. Must be assigned in the __init__ method"""
    
    _version_point_keys = set[ModbusPointKey]()
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
    
    def instantiate(self) -> None:
        if not self._attr_manufacturer:
            raise ValueError("Manufacturer not set")
        if not self._attr_model_name:
            raise ValueError("Model name not set")
        if not hasattr(self, '_attr_datapoints'):
            raise ValueError("Datapoints not set")
        if not hasattr(self, '_attr_setpoints'):
            raise ValueError("Setpoints not set")
        if not hasattr(self, '_attr_version_keys'):
            raise ValueError("Version keys not set")
        
        #check for shared errors in the datapoints and setpoints
        for point in self._attr_datapoints + self._attr_setpoints:
            if point.read_length < 1: raise ValueError(f"Setpoint {point.key} has a read_length less than 1")
            if point.max == 0: point.max = (1 << (point.read_length * 2 * 8)) - 1 #set max to the max value for the register length
            if point.max == -1: point.max = (1 << (point.read_length * 2 * 8)) - 2 #set max to the max value for the register length -1 
            if point.max < 0: raise ValueError(f"Setpoint {point.key} has an invalid negative max value")
            if point.min > point.max: raise ValueError(f"Setpoint {point.key} has min value greater than max value")
            if point.value_type not in [ModbusValueType.AUTO, ModbusValueType.FLOAT, ModbusValueType.INT]:
                # value_type is a string encoding
                if point.read_modifier is not None: raise ValueError(f"Setpoint {point.key} has a read_modifier set, but value_type is a string encoding. Modifiers are not supported for strings.")
            
        #check for errors in the setpoints
        for point in self._attr_setpoints:
            if point.read_address is None and point.write_address is None:
                raise ValueError(f"Setpoint {point.key} has no read_address or write_address")
            if point.read_modifier is not None and point.read_address is None:
                raise ValueError(f"Setpoint {point.key} has a read_modifier but no read_address")
            if point.write_modifier is not None and point.write_address is None:
                raise ValueError(f"Setpoint {point.key} has a write_modifier but no write_address")
            if point.write_length < 1: raise ValueError(f"Setpoint {point.key} has a write_length less than 1")
         
        self._device_info.manufacturer = self._attr_manufacturer
        self._device_info.model_name = self._attr_model_name
        
        for point in self._attr_datapoints:
            point.extra = point.extra or self._attr_default_extras.get(point.key)
        self._datapoints = {point.key: ModbusDatapointData(point) for point in self._attr_datapoints}
        
        for point in self._attr_setpoints:
            point.extra = point.extra or self._attr_default_extras.get(point.key)
        self._setpoints = {point.key: ModbusSetpointData(point) for point in self._attr_setpoints}
        
        self._version_point_keys = self._attr_version_keys.to_set()
        self._ready = True
    
    def get_datapoint(self, key: ModbusDatapointKey) -> ModbusDatapoint | None:
        data = self._datapoints.get(key)
        return data.point if data is not None else None
    
    def get_datapoints_for_read(self) -> List[ModbusDatapoint]:
        return [value.point for value in self._datapoints.values() if value.read]
    
    def get_initial_datapoints_for_read(self) -> List[ModbusDatapoint]:
        result:List[ModbusDatapoint] = [value.point for key, value in self._datapoints.items() 
                                        if value.read_flags & Read.STARTUP or key in self._version_point_keys]
        return result

    def get_initial_setpoints_for_read(self) -> List[ModbusSetpoint]:
        result:List[ModbusSetpoint] = [value.point for key, value in self._setpoints.items() 
                                        if value.read_flags & Read.STARTUP or key in self._version_point_keys]
        return result

    def get_max_value(self, key: ModbusSetpointKey) -> float | int | None:
        if self.provides(key):
            point = self._setpoints[key].point
            return ModbusParser.apply_offset_divider_modifier(point=point, value=point.max)
        return None

    def get_min_value(self, key: ModbusSetpointKey) -> float | int | None:
        if self.provides(key):
            point = self._setpoints[key].point
            return ModbusParser.apply_offset_divider_modifier(point=point, value=point.min)
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
        if isinstance(key, ModbusDatapointKey):
            return key in self._datapoints
        elif isinstance(key, ModbusSetpointKey):
            return key in self._setpoints
        return False
    
    def set_read(self, key: ModbusPointKey, read: bool, *, force: bool=False) -> bool:
        """
        Sets the read state for the point. Returns the new read state.
        
        Args:
            key: The key of the datapoint or setpoint to set the read state for.
            read: The new read state.
            force: If the read state should be forced to the new state, even if the point is set to always read.
        """
        if isinstance(key, ModbusDatapointKey):
            pointdata = self._datapoints.get(key)
            if pointdata is not None:
                if force: pointdata.read = read
                else: pointdata.read = read or bool(pointdata.read_flags & (Read.ALWAYS))
                #if point is only having value when requested, and read is set to False, then clear the value
                if not pointdata.read and bool(pointdata.read_flags == Read.REQUESTED): pointdata.value = None
            return True
        elif isinstance(key, ModbusSetpointKey):
            pointdata = self._setpoints.get(key)
            if pointdata is not None and pointdata.point.read_address is not None:
                if force: pointdata.read = read
                else: pointdata.read = read or bool(pointdata.read_flags & (Read.ALWAYS))
                #if point is only having value when requested, and read is set to False, then clear the value
                if not pointdata.read and bool(pointdata.read_flags == Read.REQUESTED): pointdata.value = None
                return True
        return False
    
    def _set_value(self, key: ModbusPointKey, value: MODBUS_VALUE_TYPES|None) -> Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]:
        old_value:MODBUS_VALUE_TYPES|None = None
        assigned_value:MODBUS_VALUE_TYPES|None = None
        if isinstance(key, ModbusDatapointKey):
            data = self._datapoints.get(key)
            if data is not None:
                old_value = data.value
                assigned_value = data.value = value
        elif isinstance(key, ModbusSetpointKey):
            data = self._setpoints.get(key)
            if data is not None:
                assigned_value = data.value = value
        return (old_value, assigned_value)
    
    def set_values(self, kv: List[Tuple[ModbusPointKey, MODBUS_VALUE_TYPES|None]]) -> Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]:
        result = dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]()
        for key, value in kv:
            old_value, new_value = self._set_value(key, value)
            result[key] = (old_value, new_value)
        
        self._set_version_if_changed(result)
        return result
    
    def _set_version_if_changed(self, valuesset:Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]) -> None:
        address_major = self._extract_version_value(self._attr_version_keys.datapoint_major, valuesset, 0)
        address_minor = self._extract_version_value(self._attr_version_keys.datapoint_minor, valuesset, 0)
        address_patch = self._extract_version_value(self._attr_version_keys.datapoint_patch, valuesset, 0)
        hardware_major = self._extract_version_value(self._attr_version_keys.hardware_major, valuesset, 0)
        hardware_minor = self._extract_version_value(self._attr_version_keys.hardware_minor, valuesset, 0)
        setpoint_major = self._extract_version_value(self._attr_version_keys.setpoint_major, valuesset, 0)
        setpoint_minor = self._extract_version_value(self._attr_version_keys.setpoint_minor, valuesset, 0)
        setpoint_patch = self._extract_version_value(self._attr_version_keys.setpoint_patch, valuesset, 0)
        software_major = self._extract_version_value(self._attr_version_keys.software_major, valuesset, 0)
        software_minor = self._extract_version_value(self._attr_version_keys.software_minor, valuesset, 0)
        software_patch = self._extract_version_value(self._attr_version_keys.software_patch, valuesset, 0)
        
        new_version = VersionInfo(datapoint_major=address_major, datapoint_minor=address_minor, datapoint_patch=address_patch, 
                                  hardware_major=hardware_major, hardware_minor=hardware_minor, 
                                  setpoint_major=setpoint_major, setpoint_minor=setpoint_minor, setpoint_patch=setpoint_patch,
                                  software_major=software_major, software_minor=software_minor, software_patch=software_patch)
        old_version = self._device_info.version
        if new_version != old_version:
            self._device_info.version = new_version
            self._version_changed(old_version, new_version)
        
                
    def _extract_version_value(self, key: ModbusPointKey|None, valuesset:Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]], default:int) -> int:
        newval = valuesset.get(key, (None, None))[1] if key is not None else None
        currentval = self.get_value(key) if key is not None else None
        if currentval is None: currentval = default
        val = newval if newval is not None else currentval
        return int(val)
        
    
    def _version_changed(self, old_version: VersionInfo, new_version: VersionInfo) -> None:
        """
        Called when the version of the device has changed, including during initial data read.
        It can be used to update the device with new features or remove old ones.
        
        Remember to call 'self.instantiate()' if changes have been made to the device,
        to setup the device for usage with the new changes.
        
        During the initial data read, old_version is Major=0, Minor=0, Patch=0.
        """
        pass 
        
class Modifier:
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
    def combine_values(values: List[int]) -> int:
        # Combine the list of 16-bit integers into a single integer
        return reduce(lambda acc, val: (acc << 16) | val, values, 0)

    @staticmethod
    def apply_offset_divider_modifier(point:ModbusDatapoint|ModbusSetpoint, value: int) -> float|int:
        divider = ModbusParser.get_point_divider(point)
        offset = ModbusParser.get_point_offset(point)
        modifier = ModbusParser.get_point_read_modifier(point)
        new_value:float|int = value
        if offset != 0: new_value += offset 
        if divider > 1: new_value /= divider
        if modifier is not None: new_value = modifier(new_value)
        return new_value
    
    @staticmethod
    def revert_offset_divider_modifier(point:ModbusSetpoint, value: float|int) -> float|int:
        divider = ModbusParser.get_point_divider(point)
        offset = ModbusParser.get_point_offset(point)
        modifier = ModbusParser.get_point_write_modifier(point)
        new_value:float|int = value
        if modifier is not None: new_value = modifier(new_value)
        if divider > 1: new_value *= divider
        if offset != 0: new_value -= offset 
        return new_value
    
    @staticmethod
    def bytes_to_values(byte_array: bytes, bytes_length: int) -> List[int]:
        # Ensure the byte array length is even
        if len(byte_array) % 2 != 0:
            byte_array += b'\x00'
        # Split the byte array into 2-byte integers
        return [int.from_bytes(byte_array[i:i+2], "big") for i in range(0, bytes_length, 2)]
    
    @staticmethod
    def value_to_values(value: MODBUS_VALUE_TYPES, point: ModbusSetpoint, validate: bool = True) -> list[int]|None:
        if (point.value_type == ModbusValueType.AUTO or 
            point.value_type == ModbusValueType.INT or
            point.value_type == ModbusValueType.FLOAT):
            if isinstance(value, str): raise ValueError(f"Point {point.key} expects {point.value_type}, but value is a string: {value}")
            return ModbusParser.number_to_values(value, point, validate)
        else:# point.value_type == ModbusValueType.UTF8 or point.value_type == ModbusValueType.ASCII or some other str encoding
            if not isinstance(value, str): raise ValueError(f"Point {point.key} expects {point.value_type}, but value is not a string: {value}")
            return ModbusParser.str_to_values(value, point)
    
    @staticmethod
    def number_to_values(value: float|int, point: ModbusSetpoint, validate: bool = True) -> List[int]:
        # Revert the offset, divider and modifier from the value, this will ex. make float back to integer
        result = ModbusParser.revert_offset_divider_modifier(point, value)
        if isinstance(result, float): 
            result = round(result)
        # Ensure the result is within the valid range
        if validate:
            if result > ModbusParser.get_point_max(point) or result < ModbusParser.get_point_min(point):
                raise ValueError("Value out of range")
        bytes_length = ModbusParser.get_point_read_length_bytes(point)
        byte_array = result.to_bytes(bytes_length, byteorder='big', signed=point.signed)
        return ModbusParser.bytes_to_values(byte_array, bytes_length)

    @staticmethod
    def str_to_values(value: str, point: ModbusDatapoint|ModbusSetpoint) -> List[int]:
        byte_array = value.encode(point.value_type)
        return ModbusParser.bytes_to_values(byte_array, ModbusParser.get_point_read_length_bytes(point))

    @staticmethod
    def values_to_value(value: list[int], point: ModbusDatapoint|ModbusSetpoint) -> MODBUS_VALUE_TYPES|None:
        if point.value_type == ModbusValueType.AUTO:
            if point.divider == 1:
                return ModbusParser.values_to_int(value, point)
            return ModbusParser.values_to_float(value, point)
        if point.value_type == ModbusValueType.INT:
            return ModbusParser.values_to_int(value, point)
        if point.value_type == ModbusValueType.FLOAT:
            return ModbusParser.values_to_float(value, point)
        else:# point.value_type == ModbusValueType.UTF8 or point.value_type == ModbusValueType.ASCII or some other str encoding
            return ModbusParser.values_to_str(value, point)
       
    @staticmethod
    def values_to_float(value: list[int], point: ModbusDatapoint|ModbusSetpoint) -> float|None:
        # Combine the list of 16-bit integers into a single integer
        result = ModbusParser.combine_values(value)
        if point.signed:
            # Calculate the total number of bits
            total_bits = point.read_length * 2 * 8
            # Check if the sign bit is set
            if result & (1 << (total_bits - 1)):
                # Adjust for signed value
                result -= 1 << total_bits
        if result > ModbusParser.get_point_max(point) or result < ModbusParser.get_point_min(point):
            return None
        result = ModbusParser.apply_offset_divider_modifier(point, result)
        return result

    @staticmethod
    def values_to_int(value: list[int], point: ModbusDatapoint|ModbusSetpoint) -> int|None:
        result = ModbusParser.values_to_float(value, point)
        if result is None: return None
        return int(result)
    
    @staticmethod
    def values_to_str(value: list[int], point: ModbusDatapoint|ModbusSetpoint) -> str|None:
        result = ModbusParser.combine_values(value)
        total_bytes = point.read_length * 2
        # Convert list of 16-bit integers to a byte array using join with a generator expression
        byte_array = result.to_bytes(total_bytes, byteorder='big')
        # Decode the byte array as a UTF-8 string and strip null bytes
        return byte_array.decode(point.value_type, errors='replace').rstrip(NONE_BYTE)
    
    @staticmethod
    def get_point_divider(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        return point.divider
    @staticmethod
    def get_point_offset(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        return point.offset
    @staticmethod
    def get_point_read_address(point:ModbusDatapoint|ModbusSetpoint) -> int|None: 
        return point.read_address
    @staticmethod
    def get_point_read_length_bytes(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        """Returns the number of bytes the point is using"""
        return (point.read_length*16 + 7) // 8 #round up to the nearest byte
    @staticmethod
    def get_point_read_obj(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        return point.read_obj
    @staticmethod
    def get_point_write_address(point:ModbusSetpoint) -> int|None: 
        return point.write_address
    @staticmethod
    def get_point_write_obj(point:ModbusSetpoint) -> int: 
        return point.write_obj
    @staticmethod
    def get_point_signed(point:ModbusDatapoint|ModbusSetpoint) -> bool: 
        return point.signed
    @staticmethod
    def get_point_step(point:ModbusSetpoint) -> int: 
        return point.divider if point.step is None else point.step
    @staticmethod
    def get_point_max(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        return point.max
    @staticmethod
    def get_point_min(point:ModbusDatapoint|ModbusSetpoint) -> int: 
        return point.min
    @staticmethod
    def get_point_read_modifier(point:ModbusDatapoint|ModbusSetpoint) -> Callable[[float|int], float|int]|None: 
        return None if point.read_modifier is None else point.read_modifier
    @staticmethod
    def get_point_write_modifier(point: ModbusSetpoint) -> Callable[[float|int], float|int]|None: 
        return None if point.write_modifier is None else point.write_modifier