"""The micro_nabto wire format, against answers captured from a real device.

Captured from a Nilan CTS 402, whose replies pad the answer to an even length (01, or 02 02)
and end with the 16-bit sum of every byte before. Client id, server id and email are replaced."""
from collections.abc import Callable

import pytest

from src.modbus_event_connect.micro_nabto import _wire as wire

CLIENT = bytes.fromhex("01020304")
SERVER = bytes.fromhex("000001a7")
EMAIL = "user@example.invalid"

CONNECT_REPLY = bytes.fromhex(
    "01020304" "00000000" "83020001" "0001" "0029"
    "3400000c" "00000001" "000001a7" "3b00000d" "00000000" "3c0000033c")
RECEIPT = bytes.fromhex("01020304" "000001a7" "16020001" "0002" "0018" "3400000800000003")
DATAPOINTS_27 = bytes.fromhex(
    "001b0000010400f0009500e400e100a0023d00000000000000010001000000000000000100010001000100010271"
    "000000000ca600000000")
SETPOINTS_29 = bytes.fromhex(
    "00001d0000012c0001000200030320032000e6000a001e0046000f003c0078005a000000a000be00f001900212"
    "03200104019a021c032a000100010004")
REFUSED = bytes.fromhex("00000004")
PING = (b"pong" + bytes.fromhex("00011a58" "00000474" "0000008e" "00011a4e" "00000001" "0022")
        + f"us#1:{EMAIL}:".encode())


def _data_packet(answer: bytes, sequence: int = 3, client: bytes = CLIENT) -> bytes:
    padding = 2 - len(answer) % 2
    total = 22 + len(answer) + padding + 2
    packet = (client + SERVER + bytes.fromhex("16020001") + sequence.to_bytes(2, "big") + total.to_bytes(2, "big")
              + bytes.fromhex("3600") + (total - 16).to_bytes(2, "big") + bytes.fromhex("000a")
              + answer + bytes([padding]) * padding)
    return packet + (sum(packet) & 0xFFFF).to_bytes(2, "big")


def _resummed(packet: bytes) -> bytes:
    return packet + (sum(packet) & 0xFFFF).to_bytes(2, "big")


# ================================================================================ replies

def test_the_server_id_is_taken_after_the_status_not_from_the_header() -> None:
    """The header's server id is zero in a connect reply; the session's id follows the status."""
    assert wire.reply(CONNECT_REPLY, CLIENT) == wire.ConnectReply(1, True, SERVER)


def test_a_connect_reply_without_the_accepted_status_is_a_refusal() -> None:
    refused = CONNECT_REPLY[:20] + bytes(4) + CONNECT_REPLY[24:]
    answer = wire.reply(refused, CLIENT)
    assert isinstance(answer, wire.ConnectReply) and not answer.accepted


def test_the_receipt_sent_ahead_of_an_answer_is_not_taken_for_it() -> None:
    assert wire.reply(RECEIPT, CLIENT) is None


def test_a_reply_to_another_client_is_ignored() -> None:
    assert wire.reply(_data_packet(DATAPOINTS_27), bytes.fromhex("09090909")) is None


@pytest.mark.parametrize("datagram", [b"", CONNECT_REPLY[:15], CONNECT_REPLY[:27], RECEIPT[:22]])
def test_a_short_datagram_is_no_reply(datagram: bytes) -> None:
    assert wire.reply(datagram, CLIENT) is None


def test_a_data_reply_carries_its_sequence_and_the_answer_without_padding() -> None:
    assert wire.reply(_data_packet(DATAPOINTS_27, 7), CLIENT) == wire.DataReply(7, DATAPOINTS_27)


def test_an_odd_answer_loses_its_one_byte_of_padding() -> None:
    assert len(SETPOINTS_29) % 2 == 1
    assert wire.reply(_data_packet(SETPOINTS_29), CLIENT) == wire.DataReply(3, SETPOINTS_29)


def test_a_damaged_datagram_is_dropped() -> None:
    packet = bytearray(_data_packet(DATAPOINTS_27))
    packet[30] ^= 0x01
    assert wire.reply(bytes(packet), CLIENT) is None


def test_a_datagram_cut_short_on_the_way_is_dropped() -> None:
    assert wire.reply(_resummed(_data_packet(DATAPOINTS_27)[:-6]), CLIENT) is None


def test_padding_that_does_not_match_its_length_is_dropped() -> None:
    assert wire.reply(_resummed(_data_packet(DATAPOINTS_27)[:-4] + bytes.fromhex("0102")), CLIENT) is None


def test_a_datapoint_answer_is_a_count_then_the_registers() -> None:
    values = wire.datapoint_values(DATAPOINTS_27)
    assert values is not None and len(values) == 27
    assert values[:4] == [0x0000, 0x0104, 0x00F0, 0x0095]


def test_a_setpoint_answer_has_one_status_byte_before_its_count() -> None:
    values = wire.setpoint_values(SETPOINTS_29)
    assert values is not None and len(values) == 29
    assert values[:3] == [0x0000, 0x012C, 0x0001]


@pytest.mark.parametrize("parse", [wire.datapoint_values, wire.setpoint_values])
def test_a_refused_read_has_no_values(parse: Callable[[bytes], list[int] | None]) -> None:
    assert parse(REFUSED) == []


@pytest.mark.parametrize("payload", [b"", b"\x00", b"\x00\x02\x00\x01", b"\x00\x01\x00\x01\x02\x02"])
def test_an_answer_whose_count_and_values_disagree_is_malformed(payload: bytes) -> None:
    """Taking padding or a stray byte for a value would report a number the device never sent."""
    assert wire.datapoint_values(payload) is None


def test_the_identity_is_the_four_handshake_numbers() -> None:
    assert wire.identity(PING) == {"device_number": 72280, "device_model": 1140,
                                   "slave_device_number": 72270, "slave_device_model": 1}


def test_the_email_the_device_echoes_is_not_part_of_the_identity() -> None:
    identity = wire.identity(PING)
    assert identity is not None and EMAIL not in repr(identity)


@pytest.mark.parametrize("payload", [DATAPOINTS_27, PING[:23], b"ping" + PING[4:]])
def test_only_a_pong_is_an_identity(payload: bytes) -> None:
    assert wire.identity(payload) is None


def test_a_discovery_reply_names_the_device() -> None:
    assert wire.discovery_reply(wire.DISCOVERY_REPLY + bytes(15) + b"a.device.invalid\x00junk") == "a.device.invalid"


@pytest.mark.parametrize("datagram", [CONNECT_REPLY, wire.DISCOVERY_REPLY + bytes(15),
                                      wire.DISCOVERY_REPLY + bytes(15) + b"\xff\xfe\x00"])
def test_anything_else_is_no_discovery_reply(datagram: bytes) -> None:
    assert wire.discovery_reply(datagram) is None


# =============================================================================== requests

def test_a_datapoint_read_names_each_address_in_four_bytes() -> None:
    assert wire.datapoint_read([(0, 23), (1, 0x01020304)]) == bytes.fromhex(
        "0000002d" "0002" "0000000017" "0101020304" "01")


def test_a_setpoint_read_names_each_address_in_two_bytes() -> None:
    assert wire.setpoint_read([(0, 30), (2, 0x0102)]) == bytes.fromhex("0000002a" "0002" "00001e" "020102" "01")


def test_a_setpoint_write_names_address_in_four_bytes_then_the_register() -> None:
    assert wire.setpoint_write([(0, 30, 0xFFFF)]) == bytes.fromhex("0000002b" "0001" "000000001e" "ffff" "01")


def test_a_discovery_request_names_the_device_or_asks_everyone() -> None:
    assert wire.discovery_request("a.device.invalid") == bytes.fromhex("00000001" + "00" * 8) + b"a.device.invalid\x00"
    assert wire.discovery_request().endswith(b"*\x00")


def test_a_data_request_ends_with_the_sum_of_its_bytes() -> None:
    packet = wire.data_request(CLIENT, SERVER, 5, wire.ping())
    assert packet[:4] == CLIENT and packet[4:8] == SERVER and packet[12:14] == b"\x00\x05"
    assert int.from_bytes(packet[-2:], "big") == sum(packet[:-2])


def test_a_checksum_past_sixteen_bits_wraps_rather_than_raising() -> None:
    command = wire.setpoint_write([(0xFF, 0xFFFF_FFFF, 0xFFFF)] * 60)
    packet = wire.data_request(b"\xff" * 4, b"\xff" * 4, 0xFFFF, command)
    assert sum(packet[:-2]) > 0xFFFF
    assert int.from_bytes(packet[-2:], "big") == sum(packet[:-2]) & 0xFFFF


def test_the_handshake_carries_the_email() -> None:
    assert EMAIL.encode() in wire.connect_request(CLIENT, 1, EMAIL)
