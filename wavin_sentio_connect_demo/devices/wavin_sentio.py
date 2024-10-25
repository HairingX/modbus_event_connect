from src.modbus_event_connect import *

class WavinModbusDatapointKey(ModbusDatapointKey):
    """
    Datapoints that can be read.
    """
    MODBUS_MODE = "modbus_mode"

class WavinModbusSetpointKey(ModbusSetpointKey):
    """
    Setpoints that can be read/written.
    """
    MODBUS_PASSWORD = "modbus_password"
    
DEFAULT_EXTRAS = {
    WavinModbusDatapointKey.MODBUS_MODE: {"unit_of_measurement": UOM.UNKNOWN},
    WavinModbusSetpointKey.MODBUS_PASSWORD: {"unit_of_measurement": UOM.UNKNOWN},
}

class WavinSentio(ModbusDeviceBase):
    def __init__(self, device_info: ModbusDeviceInfo):
        super().__init__(device_info)

        self._attr_manufacturer="Wavin"
        self._attr_model_name="Sentio"
        self._attr_default_extras = DEFAULT_EXTRAS
        self._attr_datapoints = [
            ModbusDatapoint(key=WavinModbusDatapointKey.MODBUS_MODE, read_address=5, divider=1, signed=True, extra={"always_read": True}),
        ]
        self._attr_setpoints = [
            ModbusSetpoint(key=WavinModbusSetpointKey.MODBUS_PASSWORD, read_address=0, write_address=6, divider=1, min=1, max=65535, signed=True, extra={"always_read": True}),
        ]

        #place config modifiers here