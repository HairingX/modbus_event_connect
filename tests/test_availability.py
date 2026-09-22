"""
Tests for the availability record and the derived read flag.

The old design let four parties assign `pointdata.read`: subscribe(), set_read(), the model's
Read.ALWAYS flag, and the 0x02 handler via set_read(force=True). Last writer won, so a
register the device had said it does not have came back the moment anything else touched the
key. Home Assistant creates one entity per value, so that was the normal case, not an edge.

Now each party keeps its own reason and the answer is computed:

    read = (subscriber or set_read() or Read.ALWAYS) and is_available(key)

Availability is a veto rather than a fourth reason, which is what makes the resurrections
below impossible by construction. Each of the first five reproduces one line of the table in
docs/design-bringup.md section 1.
"""
import asyncio
from enum import auto
from typing import Callable, List

from doubles import RecordingTransport, device_info
from src.modbus_event_connect import (
    MODBUS_VALUE_TYPES,
    ModbusDatapoint,
    ModbusDatapointKey,
    ModbusDevice,
    ModbusDeviceAdapter,
    ModbusDeviceBase,
    ModbusDeviceInfo,
    ModbusPointKey,
    ModbusSetpoint,
    ModbusSetpointKey,
    ModbusTCPEventConnect,
    Read,
    VersionInfoKeys,
)
from src.modbus_event_connect.modbus_tcp.transport import (
    EXCEPTION_ILLEGAL_DATA_ADDRESS,
    ModbusTransport,
)

Value = MODBUS_VALUE_TYPES | None


class DK(ModbusDatapointKey):
    PRESENT = auto()
    ABSENT = auto()
    ALWAYS = auto()


class SK(ModbusSetpointKey):
    TARGET = auto()


class _Device(ModbusDeviceBase):
    """Three datapoints, one of which the model insists on reading every time."""

    def __init__(self, info: ModbusDeviceInfo) -> None:
        super().__init__(info)
        self._attr_manufacturer = "TEST"
        self._attr_model_name = "TEST"
        self._attr_version_keys = VersionInfoKeys()
        self._attr_datapoints = [
            ModbusDatapoint(key=DK.PRESENT, read_address=10),
            ModbusDatapoint(key=DK.ABSENT, read_address=11),
            ModbusDatapoint(key=DK.ALWAYS, read_address=12, extra={"read": Read.ALWAYS}),
        ]
        self._attr_setpoints = [
            ModbusSetpoint(key=SK.TARGET, read_address=20, write_address=20, max=100),
        ]


class _Adapter(ModbusDeviceAdapter):
    def _translate_to_model(self, device_info: ModbusDeviceInfo) -> Callable[[ModbusDeviceInfo], ModbusDevice] | None:
        return _Device


class _Client(ModbusTCPEventConnect):
    def __init__(self, transport: ModbusTransport | None = None) -> None:
        super().__init__(transport=transport)
        self._attr_adapter = _Adapter()


def _connected_client(transport: RecordingTransport | None = None) -> _Client:
    """A client brought up the way a transport does it: load the model, settle the flags."""
    client = _Client()
    client._attr_adapter.load_device_model(device_info())
    client._sync_read_flags()
    if transport is not None:
        client._transport = transport
    return client


def _nothing(key: ModbusPointKey, old: Value, new: Value) -> None:
    """A subscriber that only needs to exist."""


# ------------------------------------------------- the five resurrections, one test each

def test_a_point_the_device_rejects_stops_being_read() -> None:
    client = _connected_client()
    client.set_read(DK.ABSENT)
    assert DK.ABSENT in client.get_read_keys()

    client.set_available(DK.ABSENT, False, reason="0x02")
    assert DK.ABSENT not in client.get_read_keys()


def test_subscribing_does_not_resurrect_an_unavailable_point() -> None:
    """The one that mattered: HA subscribes once per entity, after the first read."""
    client = _connected_client()
    client.set_available(DK.ABSENT, False, reason="0x02")

    client.subscribe(DK.ABSENT, _nothing)

    assert DK.ABSENT not in client.get_read_keys()


def test_set_read_does_not_resurrect_an_unavailable_point() -> None:
    client = _connected_client()
    client.set_available(DK.ABSENT, False, reason="0x02")

    client.set_read(DK.ABSENT)

    assert DK.ABSENT not in client.get_read_keys()


def test_read_always_does_not_resurrect_an_unavailable_point() -> None:
    """A model asking loudly still cannot conjure a register the unit does not have."""
    client = _connected_client()
    assert DK.ALWAYS in client.get_read_keys(), "test premise: ALWAYS is read without asking"

    client.set_available(DK.ALWAYS, False, reason="0x02")

    assert DK.ALWAYS not in client.get_read_keys()


def test_reloading_the_model_does_not_resurrect_an_unavailable_point() -> None:
    """
    instantiate() rebuilds the point tables from scratch.

    That is why the record lives on the client. Anything stored on a point object is wiped by
    exactly the operation a version change performs.
    """
    client = _connected_client()
    client.set_read(DK.ABSENT)
    client.set_available(DK.ABSENT, False, reason="0x02")

    client._attr_adapter.load_device_model(device_info())
    client._sync_read_flags()

    assert DK.ABSENT not in client.get_read_keys()
    assert client.is_available(DK.ABSENT) is False


# ------------------------------------------------------------- the reasons stay independent

def test_unsubscribing_leaves_an_explicit_set_read_standing() -> None:
    client = _connected_client()
    client.set_read(DK.PRESENT)
    client.subscribe(DK.PRESENT, _nothing)
    client.unsubscribe(DK.PRESENT, _nothing)
    assert DK.PRESENT in client.get_read_keys()


def test_set_read_false_leaves_a_subscriber_standing() -> None:
    client = _connected_client()
    client.subscribe(DK.PRESENT, _nothing)
    client.set_read(DK.PRESENT, False)
    assert DK.PRESENT in client.get_read_keys()


def test_a_point_stops_being_read_once_no_reason_remains() -> None:
    client = _connected_client()
    client.set_read(DK.PRESENT)
    client.subscribe(DK.PRESENT, _nothing)
    client.unsubscribe(DK.PRESENT, _nothing)
    client.set_read(DK.PRESENT, False)
    assert DK.PRESENT not in client.get_read_keys()


def test_becoming_available_again_restores_the_earlier_reasons() -> None:
    """Marking a point unavailable suspends it; it does not forget who wanted it."""
    client = _connected_client()
    client.set_read(DK.PRESENT)
    client.subscribe(DK.PRESENT, _nothing)

    client.set_available(DK.PRESENT, False, reason="peripheral pulled")
    assert DK.PRESENT not in client.get_read_keys()

    client.set_available(DK.PRESENT, True)
    assert DK.PRESENT in client.get_read_keys()


# ------------------------------------------------------------------------ the record itself

def test_everything_is_available_until_something_says_otherwise() -> None:
    client = _connected_client()
    assert client.is_available(DK.PRESENT)
    assert client.available_keys == {DK.PRESENT, DK.ABSENT, DK.ALWAYS, SK.TARGET}


def test_a_whole_group_is_marked_in_one_call() -> None:
    """What a structural decision looks like: one probe settles a room, a slot, a feature."""
    client = _connected_client()
    client.set_available([DK.ABSENT, DK.ALWAYS], False, reason="room not configured")
    assert client.available_keys == {DK.PRESENT, SK.TARGET}


def test_clearing_availability_re_enables_everything() -> None:
    """What reinit() needs: forget the decisions so the next read re-tests them."""
    client = _connected_client()
    client.set_read(DK.ABSENT)
    client.set_available([DK.ABSENT, DK.ALWAYS], False, reason="0x02")
    assert DK.ALWAYS not in client.get_read_keys()

    client.clear_availability()

    assert client.is_available(DK.ABSENT)
    assert DK.ABSENT in client.get_read_keys(), "the explicit set_read() should still apply"
    assert DK.ALWAYS in client.get_read_keys(), "Read.ALWAYS should apply again"


def test_availability_can_be_recorded_before_a_model_is_loaded() -> None:
    """A plugin may know something before connect(); it must not have to wait to say so."""
    client = _Client()
    client.set_available(DK.ABSENT, False, reason="known absent on this hardware")
    assert client.is_available(DK.ABSENT) is False
    assert client.available_keys == set(), "no model, so nothing is declared yet"

    client._attr_adapter.load_device_model(device_info())
    client._sync_read_flags()
    client.set_read(DK.ABSENT)
    assert DK.ABSENT not in client.get_read_keys()


def test_the_reason_is_kept_for_diagnostics() -> None:
    client = _connected_client()
    client.set_available(DK.ABSENT, False, reason="0x02 at address 11")
    assert client._unavailable[DK.ABSENT] == "0x02 at address 11"


def test_two_clients_do_not_share_the_record() -> None:
    a, b = _connected_client(), _connected_client()
    a.set_available(DK.ABSENT, False, reason="0x02")
    assert a.is_available(DK.ABSENT) is False
    assert b.is_available(DK.ABSENT) is True


# ----------------------------------------------------------- the read path writes the record

def test_an_illegal_address_during_a_read_marks_the_point_unavailable() -> None:
    """End to end: the device rejects the address, and the point is never polled again."""
    transport = RecordingTransport(fail=True, exception=EXCEPTION_ILLEGAL_DATA_ADDRESS)
    client = _connected_client(transport)
    client.set_read(DK.ABSENT)
    point = client._attr_adapter.get_datapoint(DK.ABSENT)
    assert point is not None

    asyncio.run(client._request_datapoint_read([point]))

    assert client.is_available(DK.ABSENT) is False
    assert DK.ABSENT not in client.get_read_keys()

    # and it stays gone, however loudly anything else asks
    client.subscribe(DK.ABSENT, _nothing)
    client.set_read(DK.ABSENT)
    assert DK.ABSENT not in client.get_read_keys()


def test_an_offline_peripheral_is_not_recorded_as_unavailable() -> None:
    """
    0x04 means the register exists and something behind it is not answering.

    That clears when the peripheral comes back, so it must never silence the point - only
    0x02, a permanent fact about this installation, may do that.
    """
    from src.modbus_event_connect.modbus_tcp.transport import EXCEPTION_SLAVE_DEVICE_FAILURE

    transport = RecordingTransport(fail=True, exception=EXCEPTION_SLAVE_DEVICE_FAILURE)
    client = _connected_client(transport)
    client.set_read(DK.PRESENT)
    point = client._attr_adapter.get_datapoint(DK.PRESENT)
    assert point is not None

    asyncio.run(client._request_datapoint_read([point]))

    assert client.is_available(DK.PRESENT) is True
    assert DK.PRESENT in client.get_read_keys(), "the point would never have recovered"


def test_status_keys_are_unaffected_by_availability() -> None:
    """Status is produced by the client, not read from a register."""
    from src.modbus_event_connect import ModbusStatusKey

    client = _connected_client()
    seen: List[Value] = []
    client.subscribe(ModbusStatusKey.CONNECTED, lambda k, o, n: seen.append(n))
    assert seen == [0]
    assert client.is_available(ModbusStatusKey.CONNECTED)
