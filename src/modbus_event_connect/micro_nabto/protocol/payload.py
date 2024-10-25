class MicroNabtoPayloadType:
    U_IPX = b'\x35'
    U_CRYPT = b'\x36'
    U_CP_ID = b'\x3F'
    # There are more of no interrest to us currently.

class MicroNabtoCommandType:    
    DATAPOINT_READLIST = b'\x2d'
    SETPOINT_READLIST = b'\x2a'
    SETPOINT_WRITELIST = b'\x2b'
    KEEP_ALIVE = b'\x02'
    PING = b'\x11'

class MicroNabtoPayload():
    
    requires_checksum = False
    payload_type = None
    payload_flags = b'\x00'

    def __init__(self) -> None:
        pass

    def build_payload(self) -> bytes: 
        return b''