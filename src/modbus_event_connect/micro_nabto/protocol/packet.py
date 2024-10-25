from enum import Enum
from typing import List

from .command_builder import MicroNabtoCommandBuilder
from .payload_crypt import MicroNabtoPayloadCrypt
from .payload import MicroNabtoPayload

class MicroNabtoPacketType(Enum): 
    U_CONNECT = b'\x83'
    DATA = b'\x16'

class MicroNabtoPacketBuilder():
    @staticmethod
    def build_packet(CLIENT_ID:bytes, SERVER_ID:bytes, PACKET_TYPE: MicroNabtoPacketType, SEQUENCE_ID:int, PAYLOADS: List[MicroNabtoPayload]=[]) -> bytes:
        payload_bundle = b''
        checksum_required = False
        for payload in PAYLOADS:
            payload_bundle += payload.build_payload()
            if payload.requires_checksum:
                checksum_required = True
        
        packet_length = len(payload_bundle) + 16
        if checksum_required:
            packet_length += 2
        
        packet = b''.join([
            CLIENT_ID,
            SERVER_ID,
            PACKET_TYPE.value,
            b'\x02', # Version
            b'\x00', # Retransmision count
            b'\x00', # Flags
            SEQUENCE_ID.to_bytes(2, 'big'),
            packet_length.to_bytes(2, 'big'),
            payload_bundle
        ])
        if checksum_required:
            # Calculate the checksum. It is simply a sum of all bytes.
            sum = 0
            for b in packet:
                sum += b
            packet = b''.join([
                packet,
                sum.to_bytes(2, 'big')
            ])
        return packet
    
    @staticmethod
    def build_keep_alive_packet(CLIENT_ID:bytes, SERVER_ID:bytes, SEQUENCE_ID:int):

        CryptPayload = MicroNabtoPayloadCrypt(MicroNabtoCommandBuilder.build_keep_alive_command())
        payload = CryptPayload.build_payload()
        packet_length = len(payload) + 18 + 2 # Header with framecontrol tag and checksum at end
        
        packet = b''.join([
            CLIENT_ID,
            SERVER_ID,
            MicroNabtoPacketType.DATA.value,
            b'\x02', # Version
            b'\x00', # Retransmision count
            b'\x40', # Flags
            SEQUENCE_ID.to_bytes(2, 'big'),
            packet_length.to_bytes(2, 'big'),
            b'\x00\x03', #Frame control tag
            payload            
        ])
        
        sum = 0
        for b in packet:
            sum += b
        packet = b''.join([
            packet,
            sum.to_bytes(2, 'big')
        ])
        return packet

    @staticmethod
    def build_discovery_packet(device_id:str|None = None) -> bytes:
        if device_id == None:
            device_id = "*"

        return b"".join([
            b'\x00\x00\x00\x01', # So called "Legacy header"
            b'\x00\x00\x00\x00\x00\x00\x00\x00', # Seems like unused space in header?
            device_id.encode("ascii"),
            b'\x00' # Zero terminator for string
        ])