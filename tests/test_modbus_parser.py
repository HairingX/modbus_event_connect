from dataclasses import dataclass
import logging
import pytest


from src.modbus_event_connect.constants import ValueLimits
from src.modbus_event_connect import ModbusParser, ModbusValueType
from models.modbus_parser_test_models import PointFactory

_LOGGER = logging.getLogger(__name__)

@dataclass
class TestData:
    pass
    
@pytest.fixture(scope="session")
def testdata():
    #setup
    yield TestData()
    #teardown

def test_modbusparser_parse_value_int16_high():
    point = PointFactory.create_setpoint(read_length=2)
    values = ModbusParser.value_to_values(ValueLimits.UINT16, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, int)
    assert value == ValueLimits.UINT16

def test_modbusparser_parse_value_int16_low():
    point = PointFactory.create_setpoint(read_length=2)
    values = ModbusParser.value_to_values(11, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, int)
    assert value == 11

def test_modbusparser_parse_value_int32_high():
    point = PointFactory.create_setpoint(read_length=2)
    values = ModbusParser.value_to_values(ValueLimits.UINT32, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, int)
    assert value == ValueLimits.UINT32

def test_modbusparser_parse_value_int32_low():
    point = PointFactory.create_setpoint(read_length=2)
    values = ModbusParser.value_to_values(11, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, int)
    assert value == 11

def test_modbusparser_parse_value_int64_high():
    point = PointFactory.create_setpoint(read_length=4)
    values = ModbusParser.value_to_values(ValueLimits.UINT64, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, int)
    assert value == ValueLimits.UINT64

def test_modbusparser_parse_value_int64_low():
    point = PointFactory.create_setpoint(read_length=4)
    values = ModbusParser.value_to_values(11, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, int)
    assert value == 11

def test_modbusparser_parse_value_float32_high():
    point = PointFactory.create_setpoint(read_length=2, divider=100)
    modbus_value = ValueLimits.UINT32/point.divider
    values = ModbusParser.value_to_values(modbus_value, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, float)
    assert value == modbus_value

def test_modbusparser_parse_value_float32_low():
    point = PointFactory.create_setpoint(read_length=2, divider=100)
    modbus_value = 11/point.divider
    values = ModbusParser.value_to_values(modbus_value, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, float)
    assert value == modbus_value

def test_modbusparser_parse_value_str():
    point = PointFactory.create_setpoint(read_length=16, value_type=ModbusValueType.UTF8)
    strval = "My ØÆå String!¤%#"
    values = ModbusParser.value_to_values(strval, point)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is not None
    assert isinstance(value, str)
    assert value == strval
    
def test_modbusparser_parse_value_invalid():
    point = PointFactory.create_setpoint(read_length=2, max=ValueLimits.UINT32_MAXERR)
    modbus_value = ValueLimits.UINT32
    with pytest.raises(ValueError):
        # Value is too high, which raises an exception
        ModbusParser.value_to_values(modbus_value, point)
    values = ModbusParser.value_to_values(modbus_value, point, validate=False)
    assert values is not None
    value = ModbusParser.values_to_value(values, point)
    assert value is None