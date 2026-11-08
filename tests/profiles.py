"""Profiles for the tests' models, each setting written out once here."""
from event_connect import NO_RETRY, Attempts, CircuitBreaker, Exponential, Fixed, Retry, Steps, Timing

from modbus_event_connect import Model, Section
from modbus_event_connect.micro_nabto import MicroNabtoProfile, RereadZeros
from modbus_event_connect.modbus import BitWrite, ModbusProfile, RegisterNumbering, SingleWrite, plain

BUSY = Retry(Exponential(0.2), Attempts(5))
"""A read answered busy tried 4 times more, after 0.2 s, doubling."""
ZEROS = RereadZeros(within=7.5, wait=Steps(0.5, then=Fixed(1.0)), at_once=True, halves=True)
"""Zeros read again at once, then after 0.5 s and every second, and in halves at 7.5 s."""
NEVER_AGAIN = CircuitBreaker(after=3, retry=NO_RETRY)
"""A device not asked again, once 3 requests in a row went unanswered, until it is connected."""
HANDSHAKE_NEVER_AGAIN = CircuitBreaker(after=1, retry=NO_RETRY)
"""A session not tried again, once a handshake failed, until it is opened."""


def modbus(model: Model, *, numbering: RegisterNumbering | None = None, max_registers: int = 125,
           max_bits: int = 2000, single_write: SingleWrite = SingleWrite.FC06,
           bit_write: BitWrite = BitWrite.MASK, busy: Retry = BUSY) -> ModbusProfile:
    """`model` over Modbus, numbered as its addresses unless told otherwise."""
    return ModbusProfile(model=model, numbering=numbering if numbering is not None else plain(first_address=1),
                         max_registers=max_registers, max_bits=max_bits, single_write=single_write,
                         bit_write=bit_write, busy=busy)


def micro_nabto(model: Model, *, max_registers: int = 64, later_answer_wait: float = 5.0,
                zeros: RereadZeros = ZEROS) -> MicroNabtoProfile:
    """`model` over micro_nabto."""
    return MicroNabtoProfile(model=model, max_registers=max_registers, later_answer_wait=later_answer_wait,
                             zeros=zeros)


def bare_model(name: str = "Bare") -> Model:
    """A model with no point, for a device tested below the client."""
    return Model(name=name, manufacturer="Test", sections=[Section([])], timing=Timing())
