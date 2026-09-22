from enum import Flag, StrEnum, auto

NONE_BYTE = '\x00'
"""The byte value for None"""
MODBUS_VALUE_TYPES = float|int|str
"""The types of values that can be read from a Modbus device."""

MODBUS_MAX_REQUEST_LENGTH = 125
"""Maximum registers per read request allowed by the Modbus protocol itself.
Individual devices may document a lower limit; see ModbusDeviceBase._attr_max_request_length."""

class UOM:
    SECONDS = "seconds"
    """Time in seconds"""
    MINUTES = "minutes"
    """Time in minutes"""
    HOURS = "hours"
    """Time in hours"""
    DAYS = "days"
    """Time in days"""
    MONTHS = "months"
    """Time in months"""
    YEARS = "years"
    """Time in years"""
    BOOL = "bool"
    """Boolean value"""
    BITMASK = "bitmask"
    """Bitmask value"""
    CELSIUS = "celsius"
    """Temperature in Celsius"""
    PCT = "percent"
    """Percentage"""
    PPM = "ppm"
    """CONCENTRATION PARTS PER MILLION"""
    RPM = "rpm"
    """REVOLUTIONS PER MINUTE"""
    # INT = "int"
    # FLOAT = "float"
    TEXT = "text"
    """Text"""
    UNKNOWN = None
    """Unknown unit of measure (Default)"""
    
    
class ModbusValueType:
    AUTO = "auto"
    """Automatically determine the value type (float|int) (Default)"""
    ASCII = "ascii"
    """Text encoded in ASCII"""
    INT = "int"
    """Integer number"""
    FLOAT = "float"
    """Floating point number"""
    UTF8 = "utf-8"
    """Text encoded in UTF-8"""

class WordOrder(StrEnum):
    HIGH_FIRST = auto()
    """register[0] holds the most significant word (Default, current behaviour)"""
    LOW_FIRST = auto()
    """register[0] holds the least significant word"""

class ByteOrder(StrEnum):
    BIG = auto()
    """register[0] holds the most significant byte and so on, i.e. the standard Modbus byte order within a register (Default, current behaviour)"""
    LITTLE = auto()
    """the two bytes are swapped within each 16-bit register"""

class RegisterTable(StrEnum):
    INPUT = auto()
    """FC 0x04, read-only 16-bit registers"""
    HOLDING = auto()
    """FC 0x03 read, 0x06/0x10 write, read-write 16-bit registers"""
    DISCRETE = auto()
    """FC 0x02, read-only single bits"""
    COIL = auto()
    """FC 0x01 read, 0x05 write, read-write single bits"""

class Read(Flag):
    REQUESTED = 0b0001
    """Read when requested (default)"""
    ALWAYS = 0b0010
    """Always read the value when reading point values"""
    STARTUP = 0b0100
    """Read during startup and when REQUESTED"""
    STARTUP_ALWAYS = ALWAYS | STARTUP
    """Read during startup and ALWAYS"""
    
# Value limits
class ValueLimit:
    UINT8 = 255
    UINT16 = 65535
    UINT32 = 4294967295
    UINT64 = 18446744073709551615
    INT8_MIN = -128
    INT8_MAX = 127
    INT16_MIN = -32768
    INT16_MAX = 32767
    INT32_MIN = -2147483648
    INT32_MAX = 2147483647
    INT64_MIN = -9223372036854775808
    INT64_MAX = 9223372036854775807
    INT128_MAX = 170141183460469231731687303715884105727
    INT256_MAX = 57896044618658097711785492504343953926634992332820282019728792003956564819967
    
    UINT8_MAXERR = UINT8-1
    """Maximum valid value for an 8-bit unsigned integer minus 1. If the value is the maximum value, it is considered invalid."""
    UINT16_MAXERR = UINT16-1
    """Maximum valid value for a 16-bit unsigned integer minus 1. If the value is the maximum value, it is considered invalid."""
    UINT32_MAXERR = UINT32-1
    """Maximum valid value for a 32-bit unsigned integer minus 1. If the value is the maximum value, it is considered invalid."""
    UINT64_MAXERR = UINT64-1
    """Maximum valid value for a 64-bit unsigned integer minus 1. If the value is the maximum value, it is considered invalid."""
    INT8_MINERR = INT8_MIN-1
    """Minimum valid value for an 8-bit signed integer minus 1. If the value is the minimum value, it is considered invalid."""
    INT8_MAXERR = INT8_MAX-1
    """Maximum valid value for an 8-bit signed integer minus 1. If the value is the maximum value, it is considered invalid."""
    INT16_MINERR = INT16_MIN+1
    """Minimum valid value for a 16-bit signed integer plus 1. If the value is the minimum value, it is considered invalid."""
    INT16_MAXERR = INT16_MAX-1
    """Maximum valid value for a 16-bit signed integer minus 1. If the value is the maximum value, it is considered invalid."""
    INT32_MINERR = INT32_MIN+1
    """Minimum valid value for a 32-bit signed integer plus 1. If the value is the minimum value, it is considered invalid."""
    INT32_MAXERR = INT32_MAX-1
    """Maximum valid value for a 32-bit signed integer minus 1. If the value is the maximum value, it is considered invalid."""
    INT64_MINERR = INT64_MIN+1
    """Minimum valid value for a 64-bit signed integer plus 1. If the value is the minimum value, it is considered invalid."""
    INT64_MAXERR = INT64_MAX-1
    """Maximum valid value for a 64-bit signed integer minus 1. If the value is the maximum value, it is considered invalid."""