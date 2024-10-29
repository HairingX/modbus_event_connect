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
VALUE_UNSIGNED_1BYTES_MAX = 255
VALUE_UNSIGNED_2BYTES_MAX = 65535
VALUE_UNSIGNED_4BYTES_MAX = 4294967295
VALUE_SIGNED_4BYTES_MAX = 2147483647
VALUE_SIGNED_4BYTES_MIN = -2147483648
VALUE_SIGNED_2BYTES_MAX = 32767
VALUE_SIGNED_2BYTES_MIN = -32768
VALUE_SIGNED_1BYTES_MAX = 127
VALUE_SIGNED_1BYTES_MIN = -128
