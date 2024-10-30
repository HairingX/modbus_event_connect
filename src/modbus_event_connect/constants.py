from enum import Flag

NONE_BYTE = '\x00'
"""The byte value for None"""
MODBUS_VALUE_TYPES = float|int|str
"""The types of values that can be read from a Modbus device."""

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
    CELSIUS = "celsius"
    """Temperature in Celsius"""
    BOOL = "bool"
    """Boolean value"""
    BITMASK = "bitmask"
    """Bitmask value"""
    PPM = "ppm"
    """CONCENTRATION PARTS PER MILLION"""
    RPM = "rpm"
    """REVOLUTIONS PER MINUTE"""
    # INT = "int"
    # FLOAT = "float"
    PCT = "percent"
    """Percentage"""
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
class ValueLimits:
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