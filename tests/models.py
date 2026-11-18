"""Models for the tests, each value written out once here; a test states what it changes."""
from collections.abc import Mapping, Sequence
from typing import Any

from event_connect import NO_RETRY, Attempts, CircuitBreaker, Exponential, Fixed, PollRate, Retry, Steps

from modbus_event_connect import Point, RepeatedSection, ScanStep, Section
from modbus_event_connect.micro_nabto import MicroNabtoModel, RereadZeros
from modbus_event_connect.modbus import BitWrite, ModbusModel, RegisterNumbering, SingleWrite, plain

INTERVALS: Mapping[PollRate, float] = {PollRate.FAST: 10.0, PollRate.MEDIUM: 30.0, PollRate.SLOW: 60.0,
                                       PollRate.RARE: 900.0}
"""Seconds between reads of each poll rate a test model reads at."""
READ_BACK_AFTER = 1.0
"""Seconds from a write until a read shows it."""
BUSY = Retry(Exponential(0.2, factor=2.0, max=None), Attempts(5))
"""A read answered busy tried 4 times more, after 0.2 s, doubling."""
ZEROS = RereadZeros(within=7.5, wait=Steps(0.5, then=Fixed(1.0)), at_once=True, halves=True)
"""Zeros read again at once, then after 0.5 s and every second, and in halves at 7.5 s."""
NEVER_AGAIN = CircuitBreaker(after=3, retry=NO_RETRY)
"""A device not asked again, once 3 requests in a row went unanswered, until it is connected."""
HANDSHAKE_NEVER_AGAIN = CircuitBreaker(after=1, retry=NO_RETRY)
"""A session not tried again, once a handshake failed, until it is opened."""
ANSWERS_WITHIN = 0.5
"""Seconds a test's Modbus unit answers within."""
TIMEOUT_MARGIN = 2.5
"""Seconds a Modbus request waits beyond the time its unit answers within."""
CONNECT_TIMEOUT = 3.0
"""Seconds a Modbus TCP link is waited for to open."""
FRAME_GAP = 0.0
"""Seconds kept between an answer and the next Modbus request."""
OFFLINE_AFTER_INTERVALS = 3
"""Reads of the fastest poll rate a device may leave unanswered before it counts as offline."""


def intervals(sections: Sequence[Section | RepeatedSection], identity_points: Sequence[Point[Any]] = (),
              scan_check: PollRate | None = None) -> dict[PollRate, float]:
    """`INTERVALS`, for exactly the rates the readable points and the scan check use."""
    points = list(identity_points)
    for section in sections:
        if isinstance(section, RepeatedSection):
            points.extend(point for number in section.numbers for point in _instance(section, number))
        else:
            points.extend(section.points)
    rates = {point.poll_rate for point in points if point.readable} | {scan_check}
    return {rate: seconds for rate, seconds in INTERVALS.items() if rate in rates}


def _instance(section: RepeatedSection, number: int) -> Sequence[Point[Any]]:
    try:
        return section.factory(number)
    except Exception:
        return ()


def modbus_model(sections: Sequence[Section | RepeatedSection] = (), *, name: str = "Bare",
                 manufacturer: str = "Test", identity_points: Sequence[Point[Any]] = (),
                 scan_steps: Sequence[ScanStep] = (), scan_check: PollRate | None = None,
                 poll_intervals: Mapping[PollRate, float] | None = None,
                 read_back_after: float = READ_BACK_AFTER, write_retry: Retry = NO_RETRY,
                 numbering: RegisterNumbering | None = None, max_registers: int = 125, max_bits: int = 2000,
                 single_write: SingleWrite = SingleWrite.FC06, bit_write: BitWrite | None = None,
                 busy: Retry = BUSY, answers_within: float = ANSWERS_WITHIN) -> ModbusModel:
    """A Modbus model of `sections`, numbered as its addresses and read at `intervals()` unless
    told otherwise."""
    return ModbusModel(
        name, manufacturer, sections,
        poll_intervals=intervals(sections, identity_points, scan_check) if poll_intervals is None else poll_intervals,
        read_back_after=read_back_after, write_retry=write_retry, scan_check=scan_check,
        identity_points=identity_points, scan_steps=scan_steps,
        numbering=numbering if numbering is not None else plain(first_address=1),
        max_registers=max_registers, max_bits=max_bits, single_write=single_write, bit_write=bit_write,
        busy=busy, answers_within=answers_within)


def micro_nabto_model(sections: Sequence[Section | RepeatedSection] = (), *, name: str = "Bare",
                      manufacturer: str = "Test", identity_points: Sequence[Point[Any]] = (),
                      scan_steps: Sequence[ScanStep] = (), scan_check: PollRate | None = None,
                      read_back_after: float = READ_BACK_AFTER, write_retry: Retry = NO_RETRY,
                      max_registers: int = 64, answer_wait: float = 5.0, later_answer_wait: float = 5.0,
                      zeros: RereadZeros = ZEROS) -> MicroNabtoModel:
    """A micro_nabto model of `sections`, read at `intervals()`."""
    return MicroNabtoModel(
        name, manufacturer, sections,
        poll_intervals=intervals(sections, identity_points, scan_check),
        read_back_after=read_back_after, write_retry=write_retry, scan_check=scan_check,
        identity_points=identity_points, scan_steps=scan_steps,
        max_registers=max_registers, answer_wait=answer_wait, later_answer_wait=later_answer_wait, zeros=zeros)
