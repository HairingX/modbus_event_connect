# import contextlib
from dataclasses import dataclass
import threading
import time
import asyncio
from random import randint
import socket
from typing import  Dict, List, Sequence, Tuple
import logging

from ..modbus_event_connect import *
from ..modbus_models import *
from .protocol import *

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PORT = 5570
DEVICE_PORT = 5570
CONNECT_TIMEOUT = 3
SOCKET_TIMEOUT = 1 # How long to wait for socket data, before moving on to other listen thread tasks.
SOCKET_BUFFER_SIZE = 512
KEEP_ALIVE_TIMEOUT = 20 # Seconds with no response to try reconnecting
REQUEST_TIMEOUT = 10
SEQUENCE_ID_MAX = 65535 # 65535 = 2^16-1 = 0xFFFF

class MicroNabtoConnectionErrorType(StrEnum):
    AUTHENTICATION_ERROR = "authentication_error"
    INVALID_ACTION = "invalid_action"
    LISTEN_THREAD_CLOSED = "listen_thread_closed"
    SOCKET_CLOSED = "socket_closed"
    TIMEOUT = "timeout"

class DeviceInfo:
    device_id:str
    address:tuple[str, int]
    def __init__(self, device_id: str, ip:str, port:int) -> None:
        self.device_id = device_id
        self.set_address((ip, port))
    def set_address(self, address: tuple[str, int]) -> None:
        self.address = address

@dataclass(kw_only=True)    
class MicroNabtoConnectionConfigValues:
    socket_timeout:float=SOCKET_TIMEOUT
    socket_buffer_size:int=SOCKET_BUFFER_SIZE
    socket_port:int=DEVICE_PORT
    discovery_port:int=DISCOVERY_PORT
    keep_alive_timeout:int=KEEP_ALIVE_TIMEOUT
    
DEFAULT_CONFIG_VALUES = MicroNabtoConnectionConfigValues()

@dataclass(kw_only=True)
class MicroNabtoModbusDeviceInfo(ModbusDeviceInfo):
    device_model:int
    device_number:int
    slave_device_model:int
    slave_device_number:int
    
class Request:
    _event: asyncio.Event
    request_time:float
    sequence_id:int
    response_time:float|None = None
    _loop:asyncio.AbstractEventLoop|None = None
    """Reason for using this loop, is that we need to be able to call the event.set() from the socket thread, which is not the same as the asyncio event loop thread waiting."""
    def __init__(self, sequence_id:int) -> None:
        self._event = asyncio.Event()
        self.request_time = time.time()
        self.sequence_id = sequence_id
    async def wait_for_response(self, timeout:float = REQUEST_TIMEOUT) -> bool:
        self._loop = asyncio.get_running_loop()
        await self._event.wait()
        self.response_time = time.time()
        return self._event.is_set()
    def notify_waiters(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._event.set)
        else:
            self._event.set()
            
class RequestConnection(Request):
    device: DeviceInfo
    _data: MicroNabtoModbusDeviceInfo|None = None
    points: Sequence[ModbusDatapoint | ModbusSetpoint]
    def __init__(self, sequence_id:int, device:DeviceInfo) -> None:
        super().__init__(sequence_id)
        self.device = device
    def set_data(self, value:MicroNabtoModbusDeviceInfo) -> None:
        self._data = value
        self.notify_waiters()
    def get_data(self) -> MicroNabtoModbusDeviceInfo|None:
        return self._data
        
class RequestData(Request):
    _data: List[MODBUS_VALUE_TYPES]
    points: Sequence[ModbusDatapoint | ModbusSetpoint]= []
    def __init__(self, sequence_id:int, points:List[ModbusDatapoint]|List[ModbusSetpoint]) -> None:
        super().__init__(sequence_id)
        self.points = points
    def set_data(self, value:List[MODBUS_VALUE_TYPES]) -> None:
        self._data = value
        self.notify_waiters()
    def get_values(self) -> List[MODBUS_VALUE_TYPES]:
        if len(self.points) != len(self._data):
            raise ValueError("Number of points and response data does not match")
        return self._data
        
        
class MicroNabtoConnection:
    _requests = dict[int, RequestData]()
    _connection_request: RequestConnection|None = None
    _sequence_id = 0 # Sequence id for requests, will be incremented for each request and reset to 1 if it reaches the SEQUENCE_ID_MAX.
    
    _client_id:bytes = randint(0,0xffffffff).to_bytes(4, 'big') # Our client ID can be anything.
    _server_id:bytes = b'\x00\x00\x00\x00' # This is our ID optained from the uNabto service on device.
    _connection_error:MicroNabtoConnectionErrorType|None = None
    _socket:socket.socket|None = None
    _listen_thread_open = False
    _discovered_devices:Dict[str, DeviceInfo] =  {}
    _last_reconnect:float = 0 # Last time we tried to reconnect
    _connected: RequestConnection|None = None
    
    def __init__(self, config_values: MicroNabtoConnectionConfigValues|None=None) -> None:
        """Initializes the connection and starts listening for incoming data"""
        self._config_values = config_values if config_values is not None else DEFAULT_CONFIG_VALUES
        self.start_listening()
  
    def is_connected(self): return self._connected is not None
    def get_discovered_devices(self): return self._discovered_devices
    def get_connection_error(self): return self._connection_error
   
    def start_listening(self) -> bool:
        """If not already listening, start listening for incoming data"""
        if self._listen_thread_open: return False
        if self._socket == None: 
            self._open_socket()
        self._listen_thread_open = True
        self._listen_thread = threading.Thread(target=self._receive_thread)
        self._listen_thread.start()
        self._send_discovery()
        return True
    
    async def discover_devices(self, clear:bool=False) -> List[DeviceInfo]:
        if clear: self._discovered_devices = {}
        self._send_discovery()
        await asyncio.sleep(0.5) # Allow for all devices to reply
        return list(self._discovered_devices.values())

    async def connect(self, email:str, device_id:str, device_host:str|None=None, device_port:int|None=DEVICE_PORT, timeout:float = CONNECT_TIMEOUT) -> MicroNabtoModbusDeviceInfo|None:
        """
        Connect to a device. If device_ip and device_port is not set, the system will try to discover the device.
        
        Args:
            email (str): The email used to authenticate with the device through the official application/app
            device_id (str): The device id to connect to
            device_ip (str, optional): The ip of the device. Defaults to None.
            device_port (int, optional): The port of the device. Defaults to 5570.
            timeout (int, optional): How long to wait for discovery. Defaults to 3.
        
        Returns:
            ModbusDeviceInfo|None: The device info if connected, otherwise None
        """
        self._connection_error = None
        self._connected = None
        self._email = email
        
        if device_host is not None and device_port is not None:
            deviceinfo = DeviceInfo(device_id, device_host, device_port)
            self._discovered_devices[device_id] = deviceinfo
        else:
            deviceinfo = await self._discover_device(device_id, timeout)
            
        if deviceinfo is None: 
            self._connection_error = MicroNabtoConnectionErrorType.TIMEOUT 
            return None
        _LOGGER.debug(f"Connecting to device: {deviceinfo.device_id} with address: {deviceinfo.address}")
        request = self._enqueue_connect_request(deviceinfo)
        try:
            self._send_connect_request(request)
            await request.wait_for_response()
            return request.get_data()
        except Exception as e:
            _LOGGER.error(f"Error connecting: {e}")
        finally:
            self._dequeue_connect_request(request)
        return None
          
    def stop_listening(self) -> None:
        self._listen_thread_open = False

    def close(self) -> None:
        self.stop_listening()
        socket = self._socket
        self._connected = None
        if socket is None: return
        self._socket = None
        socket.close()
 
    async def request_datapoint_data(self, points: List[ModbusDatapoint]) -> Dict[ModbusDatapointKey, Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES]]:
        connected = self._connected
        if connected is None or self._socket is None or not self.is_connected(): return dict()
        Payload = MicroNabtoPayloadCrypt(MicroNabtoCommandBuilder.build_datapoint_read_command(self._map_points_to_read_args(points)))
        request = self._enqueue_request(points)
        try:
            self._socket.sendto(MicroNabtoPacketBuilder().build_packet(self._client_id, self._server_id, MicroNabtoPacketType.DATA, request.sequence_id, [Payload]), connected.device.address)
            await request.wait_for_response()
            values = request.get_values()
            return {point.key: (point, value) for point, value in zip(points, values)}
        except Exception as e:
            _LOGGER.error(f"Error requesting datapoint data: {e}")
            raise
        finally:
            self._dequeue_request(request)
    
    async def request_setpoint_data(self, points: List[ModbusSetpoint]) -> Dict[ModbusSetpointKey, Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]]:
        connected = self._connected
        if connected is None or self._socket is None or not self.is_connected(): return dict()
        Payload = MicroNabtoPayloadCrypt(MicroNabtoCommandBuilder.build_setpoint_read_command(self._map_points_to_read_args(points)))
        request = self._enqueue_request(points)
        try:
            self._socket.sendto(MicroNabtoPacketBuilder().build_packet(self._client_id, self._server_id, MicroNabtoPacketType.DATA, request.sequence_id, [Payload]), connected.device.address)
            await request.wait_for_response()
            values = request.get_values()
            return {point.key: (point, value) for point, value in zip(points, values)}
        except Exception as e:
            _LOGGER.error(f"Error requesting setpoint data: {e}")
            raise
        finally:
            self._dequeue_request(request)
           
    def _is_connected(self, device_id:str|None=None): return self._connected is not None and (device_id is None or self._connected.device.device_id == device_id)
    
    def _enqueue_connect_request(self, device: DeviceInfo) -> RequestConnection:
        self._sequence_id = sequence_id = 1 if self._sequence_id + 1 > SEQUENCE_ID_MAX else self._sequence_id + 1
        request = RequestConnection(sequence_id, device)
        self._connection_request = request
        return request

    def _dequeue_connect_request(self, request:RequestConnection) -> bool:
        if request == self._connection_request:
            self._connection_request = None
            return True
        return False
     
    async def _discover_device(self, device_id:str, timeout:float) -> DeviceInfo|None:
        existing = self._discovered_devices.get(device_id)
        if existing is not None: return existing
        self._send_discovery(device_id)
        return await self._wait_for_discovery(device_id, timeout)

    def _send_connect_request(self, request:RequestConnection) -> bool:
        if self._socket == None: 
            self._connection_error = MicroNabtoConnectionErrorType.SOCKET_CLOSED
            return False
        if self._listen_thread_open == False: 
            self._connection_error = MicroNabtoConnectionErrorType.LISTEN_THREAD_CLOSED
            return False
        ipx_payload = MicroNabtoPayloadIPX()
        cp_id_payload = MicroNabtoPayloadCP_ID(self._email)
        self._socket.sendto(MicroNabtoPacketBuilder.build_packet(self._client_id, self._server_id, MicroNabtoPacketType.U_CONNECT, request.sequence_id, [ipx_payload, cp_id_payload]), request.device.address)
        return True
        
    def _send_discovery(self, specificDevice:str|None = None) -> None:
        """Broadcasts a discovery packet. Any device listening should respond"""
        if self._socket == None: return
        self._socket.sendto(MicroNabtoPacketBuilder.build_discovery_packet(specificDevice), ("255.255.255.255", self._config_values.discovery_port))
        
    def _send_device_info_request(self, request: RequestConnection) -> None:
        if self._socket is None: return
        payload = MicroNabtoPayloadCrypt(MicroNabtoCommandBuilder.build_ping_command())
        self._socket.sendto(MicroNabtoPacketBuilder().build_packet(self._client_id, self._server_id, MicroNabtoPacketType.DATA, request.sequence_id, [payload]), request.device.address)

    def _send_reconnect_request(self) -> bool:
        if self._connected is None: 
            self._connection_error = MicroNabtoConnectionErrorType.INVALID_ACTION
            return False
        request = self._enqueue_connect_request(self._connected.device)
        return self._send_connect_request(request)

    def _enqueue_request(self, points:List[ModbusDatapoint]|List[ModbusSetpoint]) -> RequestData:
        self._sequence_id = sequence_id = 1 if self._sequence_id + 1 > SEQUENCE_ID_MAX else self._sequence_id + 1
        request = RequestData(sequence_id, points)
        self._requests[sequence_id] = request
        return request

    def _dequeue_request(self, request:RequestData) -> bool:
        return self._requests.pop(request.sequence_id, None) != None
     
    async def _wait_for_discovery(self, device_id: str, timeout:float) -> DeviceInfo|None:
        """Wait for discovery of ip to be done. Returns None if not found"""
        # discovery broadcast response assigns the device info to the _discovered_devices dict
        discovery_timeout = time.time() + timeout
        deviceinfo:DeviceInfo|None = None
        while deviceinfo is None:
            deviceinfo = self._discovered_devices.get(device_id)
            if deviceinfo is not None:
                return deviceinfo
            if time.time() > discovery_timeout:
                return None
            await asyncio.sleep(0.2)
        return deviceinfo
    
    def _open_socket(self) -> None:
        if self._socket is not None: return
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)  # UDP
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1) # Allows for sending broadcasts
        self._socket.settimeout(self._config_values.socket_timeout)
        self._socket.bind(("", 0))
        
    async def _wait_for_connection(self, deviceinfo:DeviceInfo) -> bool:
        """Wait for connection to be tried"""
        connection_timeout = time.time() + 3
        while self._connection_error is None and not self._is_connected(deviceinfo.device_id):
            if time.time() > connection_timeout:
                self._connection_error = MicroNabtoConnectionErrorType.TIMEOUT
            await asyncio.sleep(0.2)
        return self._is_connected(deviceinfo.device_id)
            
    def _receive_thread(self) -> None:
        while self._listen_thread_open:
            self._handle_receive()          
            if self.is_connected():
                if time.time() - self._last_response > self._config_values.keep_alive_timeout and time.time() - self._last_reconnect > 3:
                    # If we have not received any data for a while, try to reconnect. Limit to every 3 seconds.
                    self._send_reconnect_request()
                    self._last_reconnect = time.time()

    def _handle_receive(self) -> None:
        if self._socket is None: return
        try:
            message, address = self._socket.recvfrom(self._config_values.socket_buffer_size)
            _LOGGER.debug(f"Received message: {message} from address: {address}")
            if (len(message) < 16): # Not a valid packet
                return 
            self._process_received_message(message, address)
        except socket.timeout:  
            return
                    
    def _process_received_message(self, message:bytes, address:tuple[str,int]) -> None:
        if message[0:4] == b'\x00\x80\x00\x01': 
            # This might be a discovery packet response
            discovery_response = message[19:len(message)]
            device_id_length = 0
            for b in discovery_response: # Loop until first string terminator
                if b == 0x00:
                    break
                device_id_length += 1
            device_id = discovery_response[0: device_id_length].decode("ascii")
            if "remote.lscontrol.dk" in device_id:
                # This is a valid reponse from a MicroNabtoConnect device!
                # Add the device Id and IP to our list if not seen before.
                device = self._discovered_devices.get(device_id)
                _LOGGER.debug(f"Discovery response packet from: {device_id} with address: {address} ({"existing" if device is not None else "new"})")
                if device is not None:
                    device.set_address(address)
                else:
                    self._discovered_devices[device_id] = DeviceInfo(device_id, address[0], address[1])
            return
        if message[0:4] != self._client_id:
            # Not a packet intented for us
            return
        self._last_response = time.time()
        packet_type = message[8].to_bytes(1, 'big')
        if (packet_type == MicroNabtoPacketType.U_CONNECT.value):
            # response to our connect request
            _LOGGER.debug("U_CONNECT response packet")
            if (message[20:24] == b'\x00\x00\x00\x01'):
                self._server_id = message[24:28]
                _LOGGER.debug('Connected, pinging to get model number')
                connection_request = self._connection_request
                if connection_request is None or connection_request.device.address != address: return
                self._send_device_info_request(connection_request)
            else:
                _LOGGER.error("Received unsucessfull response")
                self._connection_error = MicroNabtoConnectionErrorType.AUTHENTICATION_ERROR
        elif (packet_type == MicroNabtoPacketType.DATA.value): # 0x16
            _LOGGER.debug(f"Data packet: {message[16]}")
            # We only care about data packets with crypt payload. 
            if message[16] == 54: # x36
                _LOGGER.debug("Packet with crypt payload!")
                length = int.from_bytes(message[18:20], 'big')
                payload = message[22:20+length]
                sequence_id = int.from_bytes(message[12:14], 'big')
                _LOGGER.debug(f'sequence_id: {sequence_id}, length: {length}, payload: {payload}')
                _LOGGER.debug(f''.join(r'\x'+hex(letter)[2:] for letter in payload))
                connection_request = self._connection_request
                if connection_request is not None and connection_request.device.address == address and connection_request.sequence_id == sequence_id:  # Not a response to our request
                    self._process_device_info_payload(payload, connection_request)
                else:
                    self._parse_data_response(sequence_id, payload)
            else:
                _LOGGER.debug(f"Not an interresting data packet. payload: {message}")
        else:
            _LOGGER.debug("Unknown packet type. Ignoring")
    
    def _process_device_info_payload(self, payload:bytes, request: RequestConnection) -> None:
        self._dequeue_connect_request(request)
        self._connected = request
        
        device_number = int.from_bytes(payload[4:8], 'big')
        device_model = int.from_bytes(payload[8:12], 'big')
        slave_device_number = int.from_bytes(payload[16:20], 'big')
        slave_device_model = int.from_bytes(payload[20:24], 'big')
        _LOGGER.debug(f"Got model: {device_model} with device number: {device_number}, slavedevice number: {slave_device_number} and slavedevice model: {slave_device_model}")
                    
        deviceinfo = MicroNabtoModbusDeviceInfo(
            device_id=request.device.device_id, 
            device_host=request.device.address[0], 
            device_port=request.device.address[1],
            version=VersionInfo(0, 0, 0),
            identification=None,
            device_number=device_number, 
            device_model=device_model, 
            slave_device_number=slave_device_number,
            slave_device_model=slave_device_model,
        )
        _LOGGER.debug(f"Successfully connected to device: {deviceinfo.device_id} with address: {deviceinfo.device_host}:{deviceinfo.device_port}. Device model: {device_model}, device number: {device_number}, slave device number: {slave_device_number}, slave device model: {slave_device_model}")
        request.set_data(deviceinfo)

    def _map_points_to_read_args(self, points: Sequence[ModbusDatapoint|ModbusSetpoint]) -> List[MicroNabtoCommandBuilderReadArgs]:
        read_args = list[MicroNabtoCommandBuilderReadArgs]()
        for point in points:
            read_obj = ModbusParser.get_point_read_obj(point)
            read_address = ModbusParser.get_point_read_address(point)
            if read_address is None: continue
            read_args.append(MicroNabtoCommandBuilderReadArgs(read_obj=read_obj, read_address=read_address))
        return read_args
    
    def _map_points_to_write_args(self, points: Sequence[ModbusSetpoint], value:int) -> List[MicroNabtoCommandBuilderWriteArgs]:
        write_args = list[MicroNabtoCommandBuilderWriteArgs]()
        for point in points:
            write_obj = ModbusParser.get_point_write_obj(point)
            write_address = ModbusParser.get_point_write_address(point)
            if write_address is None: continue
            write_args.append(MicroNabtoCommandBuilderWriteArgs(write_obj=write_obj, write_address=write_address, value=value))
        return write_args
        
    def _parse_data_response(self, sequence_id:int, response_payload:bytes) -> None:
        _LOGGER.debug(f"Got dataresponse with sequence id: {sequence_id}")
        requestdata = self._requests.get(sequence_id)
        if requestdata is None:
            _LOGGER.debug(f"Response ignored. Request for points not found for sequence id: {sequence_id}")
            return
        point = requestdata.points[0]
        if isinstance(point, ModbusDatapoint):
            _LOGGER.debug(f"Is a datapoint response")
            self._parse_datapoint_response(requestdata, response_payload)
        else: #if isinstance(point, ModbusSetpoint):
            _LOGGER.debug(f"Is a setpoint response")
            self._parse_setpoint_response(requestdata, response_payload)

    def _parse_datapoint_response(self, requestdata:RequestData, response_payload:bytes) -> None:
        response_length = int.from_bytes(response_payload[0:2])
        values = list[MODBUS_VALUE_TYPES]()
        for position in range(response_length):
            point = requestdata.points[position]
            payload_slice = response_payload[2+position*2:4+position*2]
            signed = ModbusParser.get_point_signed(point)
            new_value = ModbusParser.parse_from_modbus_value(point=point, value=int.from_bytes(payload_slice, 'big', signed=signed))
            values.append(new_value)
            # _LOGGER.debug(f"New Datapoint value set: {valueKey} = {self.values[valueKey]} (old={old_value}), rawVal={int.from_bytes(payload_slice, 'big', signed=signed)}, point={point}")
        requestdata.set_data(values)
     
    def _parse_setpoint_response(self, requestdata:RequestData, response_payload:bytes) -> None:
        response_length = int.from_bytes(response_payload[1:3])
        values = list[MODBUS_VALUE_TYPES]()
        for position in range(response_length):
            point = requestdata.points[position]
            payload_slice = response_payload[3+position*2:5+position*2]
            signed = ModbusParser.get_point_signed(point)
            new_value = ModbusParser.parse_from_modbus_value(point=point, value=int.from_bytes(payload_slice, 'big', signed=signed))
            values.append(new_value)
            # _LOGGER.debug(f"New Setpoint value set: {valueKey} = {self.values[valueKey]} (old={old_value}), rawVal={int.from_bytes(payload_slice, 'big', signed=signed)}, point={point}")
        requestdata.set_data(values)
