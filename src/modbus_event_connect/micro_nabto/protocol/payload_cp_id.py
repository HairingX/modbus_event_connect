from .payload import MicroNabtoPayload, MicroNabtoPayloadType

class MicroNabtoPayloadCP_ID(MicroNabtoPayload):
    
    requires_checksum = False
    payload_type = MicroNabtoPayloadType.U_CP_ID
    email:str

    def __init__(self, email:str) -> None:
        self.email = email

    def build_payload(self) -> bytes: 
        length = 5 + len(self.email)
        return b"".join([
            self.payload_type,
            self.payload_flags,
            length.to_bytes(2, 'big'),
            b'\x01', # ID type email
            self.email.encode("ascii")
        ])