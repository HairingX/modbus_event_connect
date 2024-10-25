from typing import List, Sequence, TypedDict

from .payload import MicroNabtoCommandType

class MicroNabtoCommandBuilderReadArgs(TypedDict):
    read_obj: int
    read_address: int

class MicroNabtoCommandBuilderWriteArgs(TypedDict):
    write_obj: int
    write_address: int
    value: int

class MicroNabtoCommandBuilder():
    @staticmethod
    def build_datapoint_read_command(points: Sequence[MicroNabtoCommandBuilderReadArgs]) -> bytes: 
        request = b""
        for point in points:
            request += point['read_obj'].to_bytes(1, 'big') + point['read_address'].to_bytes(4, 'big')
        return b"".join([
            b'\x00\x00\x00',
            MicroNabtoCommandType.DATAPOINT_READLIST,            
            (len(points)).to_bytes(2, 'big'),
            request,
            b'\x01' # Seems like terminator for list/command
        ])
        
    @staticmethod
    def build_setpoint_read_command(points:List[MicroNabtoCommandBuilderReadArgs]) -> bytes:
        request = b""
        for point in points:
            request += point['read_obj'].to_bytes(1, 'big') + point['read_address'].to_bytes(2, 'big')
        return b"".join([
            b'\x00\x00\x00',
            MicroNabtoCommandType.SETPOINT_READLIST,            
            (len(points)).to_bytes(2, 'big'),
            request,
            b'\x01' # Seems like terminator for list/command
        ])

    @staticmethod
    def build_setpoint_write_command(points:List[MicroNabtoCommandBuilderWriteArgs]) -> bytes:
        request = b""
        for point in points:
            request += point['write_obj'].to_bytes(1, 'big') + point['write_address'].to_bytes(4, 'big') + point['value'].to_bytes(2, 'big')
        return b"".join([
            b'\x00\x00\x00',
            MicroNabtoCommandType.SETPOINT_WRITELIST,            
            (len(points)).to_bytes(2, 'big'),
            request,
            b'\x01' # Seems like terminator for list/command
        ])
        
    @staticmethod
    def build_keep_alive_command() -> bytes: 
        return b"".join([
            b'\x00\x00\x00',
            MicroNabtoCommandType.KEEP_ALIVE,
        ])
   
    @staticmethod
    def build_ping_command(): 
        return b"".join([
            b'\x00\x00\x00',
            MicroNabtoCommandType.PING,
            b'\x70\x69\x6e\x67'
        ])