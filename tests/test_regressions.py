"""Regression tests for bugs found during the Wavin Sentio review.

Each test here failed before the corresponding fix. They exist so the bugs cannot come back
silently - every one of them produced wrong values rather than an error.
"""
import asyncio
import logging
from enum import auto

import pytest

from src.modbus_event_connect import (
    MODBUS_VALUE_TYPES,
    ModbusDatapoint,
    ModbusDatapointKey,
    ModbusDeviceAdapter,
    ModbusDeviceBase,
    ModbusDeviceInfo,
    ModbusEventConnect,
    ModbusPointKey,
    ModbusParser,
    ModbusSetpoint,
    ModbusSetpointKey,
    ModbusTCPEventConnect,
    ValueLimit,
    VersionInfo,
    VersionInfoKeys,
)
from src.modbus_event_connect import ModbusStatusKey
from src.modbus_event_connect.modbus_tcp.transport import (
    EXCEPTION_ILLEGAL_DATA_ADDRESS,
    EXCEPTION_SLAVE_DEVICE_FAILURE,
    EXCEPTION_NONE,
    EXCEPTION_SLAVE_DEVICE_BUSY,
    ModbusTransport,
)

_LOGGER = logging.getLogger(__name__)


class DK(ModbusDatapointKey):
    TEMPERATURE = auto()
    COUNTER = auto()
    WIDE = auto()
    NARROW = auto()


class SK(ModbusSetpointKey):
    TARGET = auto()
    FLAG = auto()


class _Device(ModbusDeviceBase):
    def __init__(self, device_info: ModbusDeviceInfo):
        super().__init__(device_info)
        self._attr_manufacturer = "TEST"
        self._attr_model_name = "TEST"
        self._attr_max_request_length = 32
        self._attr_version_keys = VersionInfoKeys(datapoint_major=DK.COUNTER)
        self._attr_datapoints = [
            # val_d2_fp100 equivalent: signed 16-bit scaled by 100.
            ModbusDatapoint(key=DK.TEMPERATURE, read_address=104, divider=100, signed=True,
                            max=ValueLimit.INT16_MAXERR),
            ModbusDatapoint(key=DK.COUNTER, read_address=200),
        ]
        self._attr_setpoints = [
            ModbusSetpoint(key=SK.TARGET, read_address=119, write_address=119, divider=100,
                           signed=True, max=ValueLimit.INT16_MAXERR),
            ModbusSetpoint(key=SK.FLAG, read_address=26, write_address=26, max=1),
        ]


class _Adapter(ModbusDeviceAdapter):
    def _translate_to_model(self, device_info: ModbusDeviceInfo):
        return _Device


class _Client(ModbusTCPEventConnect):
    def __init__(self, transport=None):
        super().__init__(transport=transport)
        self._attr_adapter = _Adapter()


def _info(device_id: str = "test") -> ModbusDeviceInfo:
    return ModbusDeviceInfo(device_id=device_id, device_host="h", device_port=502,
                            version=VersionInfo(), identification=None)


def _datapoint(client: _Client, key: ModbusDatapointKey) -> ModbusDatapoint:
    """Fetch a datapoint, failing the test if the model does not have it."""
    point = client._attr_adapter.get_datapoint(key)
    assert point is not None, f"test model has no datapoint {key}"
    return point


def _setpoint(client: _Client, key: ModbusSetpointKey) -> ModbusSetpoint:
    """Fetch a setpoint, failing the test if the model does not have it."""
    point = client._attr_adapter.get_setpoint(key)
    assert point is not None, f"test model has no setpoint {key}"
    return point


def _connected_client() -> _Client:
    client = _Client()
    client._attr_adapter.load_device_model(_info())
    return client


# --------------------------------------------------------------------- parsing

def test_signed_point_decodes_negative_values():
    """A signed point left with the default min must not reject negative readings."""
    point = ModbusDatapoint(key=DK.TEMPERATURE, read_address=1, divider=100, signed=True,
                            max=ValueLimit.INT16_MAXERR)
    assert ModbusParser.values_to_value([0xFE0C], point) == -5.0
    assert ModbusParser.values_to_value([0x0866], point) == 21.5


def test_signed_point_still_rejects_the_invalid_sentinel():
    point = ModbusDatapoint(key=DK.TEMPERATURE, read_address=1, divider=100, signed=True,
                            max=ValueLimit.INT16_MAXERR)
    assert ModbusParser.values_to_value([0x7FFF], point) is None


def test_signed_default_max_is_the_signed_maximum():
    """An unresolved max on a signed point must not inherit the unsigned range."""
    point = ModbusDatapoint(key=DK.TEMPERATURE, read_address=1, signed=True)
    assert ModbusParser.get_point_max(point) == 32767
    assert ModbusParser.get_point_min(point) == -32768


def test_unsigned_default_range_is_unchanged():
    point = ModbusDatapoint(key=DK.COUNTER, read_address=1)
    assert ModbusParser.get_point_max(point) == 65535
    assert ModbusParser.get_point_min(point) == 0


def test_explicit_min_is_respected():
    point = ModbusDatapoint(key=DK.TEMPERATURE, read_address=1, signed=True, min=0)
    assert ModbusParser.values_to_value([0xFE0C], point) is None


# -------------------------------------------------------------------- batching

def test_batch_groups_contiguous_points_including_multi_register():
    client = _connected_client()
    wide = ModbusDatapoint(key=DK.WIDE, read_address=28, read_length=2)
    following = ModbusDatapoint(key=DK.NARROW, read_address=30)
    batches = list(client.batch_reads([wide, following]))
    assert len(batches) == 1, "28-29 and 30 are contiguous and belong in one request"


def test_batch_does_not_overlap_a_multi_register_point():
    """Stepping by 1 would put 29 inside the 28-29 point and corrupt the decode."""
    client = _connected_client()
    wide = ModbusDatapoint(key=DK.WIDE, read_address=28, read_length=2)
    overlapping = ModbusDatapoint(key=DK.NARROW, read_address=29)
    batches = list(client.batch_reads([wide, overlapping]))
    assert len(batches) == 2


def test_batch_respects_the_device_request_limit():
    client = _connected_client()
    points = [ModbusDatapoint(key=DK.COUNTER, read_address=addr) for addr in range(1, 50)]
    for batch in client.batch_reads(points):
        span = batch[-1].read_address + batch[-1].read_length - batch[0].read_address
        assert span <= 32


# --------------------------------------------------------------- notifications

def test_notify_does_not_stop_at_an_unsubscribed_key():
    client = _connected_client()
    seen: list[ModbusPointKey] = []
    client.subscribe(DK.COUNTER, lambda k, o, n: seen.append(k))
    client._notify_subscribers({DK.TEMPERATURE: (None, 1), DK.COUNTER: (None, 2)})
    assert seen == [DK.COUNTER]


def test_notify_only_fires_when_the_value_changed():
    client = _connected_client()
    events: list[tuple] = []
    client.subscribe(DK.COUNTER, lambda k, o, n: events.append((o, n)))
    point = _datapoint(client, DK.COUNTER)
    for _ in range(3):
        client._set_values([(point, 7)])
    assert events == [(None, 7)]
    client._set_values([(point, 8)])
    assert events[-1] == (7, 8)


def test_setpoint_reports_its_previous_value():
    client = _connected_client()
    model = client._attr_adapter._get_loaded_model()
    model.set_values([(SK.FLAG, 0)])
    assert model.set_values([(SK.FLAG, 1)])[SK.FLAG] == (0, 1)


def test_a_raising_subscriber_does_not_block_the_others():
    client = _connected_client()
    seen = []
    def boom(key, old, new): raise RuntimeError("consumer bug")
    client.subscribe(DK.COUNTER, boom)
    client.subscribe(DK.COUNTER, lambda k, o, n: seen.append(n))
    client._notify_subscribers({DK.COUNTER: (None, 3)})
    assert seen == [3]


# -------------------------------------------------------------- lifecycle

def test_subscribe_before_connect_is_allowed():
    client = _Client()
    client.subscribe(DK.COUNTER, lambda k, o, n: None)
    assert client.is_connected is False
    client._attr_adapter.load_device_model(_info())
    client._apply_subscriptions()
    assert DK.COUNTER in client.get_read_keys()


def test_two_clients_do_not_share_state():
    a, b = _Client(), _Client()
    assert a._subscribers is not b._subscribers
    assert a._attr_adapter is not b._attr_adapter
    a._attr_adapter.load_device_model(_info("a"))
    b._attr_adapter.load_device_model(_info("b"))
    assert a.device_info.device_id == "a"
    assert b.device_info.device_id == "b"


# -------------------------------------------------------------------- transport

class _RecordingTransport:
    """Minimal ModbusTransport stand-in that records calls and can fail on demand."""

    def __init__(self, *, fail=True, exception=EXCEPTION_ILLEGAL_DATA_ADDRESS, delay=0.0):
        self.calls = []
        self.fail = fail
        self.delay = delay
        self._exception = exception if fail else EXCEPTION_NONE

    @property
    def is_open(self): return True
    @property
    def last_exception_code(self): return self._exception
    @property
    def last_error_text(self): return None if not self.fail else "recorded failure"

    async def open(self): return True
    async def close(self): return None

    async def _read(self, kind, address, count):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append((kind, address, count))
        return None if self.fail else [0] * count

    async def read_input_registers(self, address, count):
        return await self._read("input", address, count)
    async def read_holding_registers(self, address, count):
        return await self._read("holding", address, count)
    async def read_discrete_inputs(self, address, count):
        return await self._read("discrete", address, count)
    async def write_register(self, address, value):
        self.calls.append(("write_register", address, value)); return not self.fail
    async def write_registers(self, address, values):
        self.calls.append(("write_registers", address, len(values))); return not self.fail


def test_recording_transport_satisfies_the_protocol():
    assert isinstance(_RecordingTransport(), ModbusTransport)


def test_a_transport_can_be_injected():
    """inject-websession: the host must be able to supply its own connection."""
    transport = _RecordingTransport(fail=False)
    client = _Client(transport=transport)
    assert client.transport is transport


def test_setpoints_are_read_from_holding_registers_even_in_the_fallback():
    client = _connected_client()
    client._transport = _RecordingTransport()
    points = [_setpoint(client, SK.FLAG),
              _setpoint(client, SK.TARGET)]
    asyncio.run(client._request_setpoint_read(points))
    assert {name for name, _, _ in client._transport.calls} == {"holding"}


def test_a_failing_batch_only_retries_its_own_points():
    client = _connected_client()
    client._transport = _RecordingTransport()
    # FLAG is at 26 and TARGET at 119: two separate, both-failing batches.
    points = [_setpoint(client, SK.FLAG),
              _setpoint(client, SK.TARGET)]
    asyncio.run(client._request_setpoint_read(points))
    singles = [c for c in client._transport.calls if c[2] == 1]
    assert len(singles) == 2, "each batch holds one point, so exactly two single reads"


def test_busy_responses_are_retried():
    client = _connected_client()
    client._transport = _RecordingTransport(exception=EXCEPTION_SLAVE_DEVICE_BUSY)
    client.BUSY_RETRY_INITIAL_DELAY = 0.001
    point = _datapoint(client, DK.COUNTER)
    asyncio.run(client._request_datapoint_read([point]))
    reads = [c for c in client._transport.calls if c[0] == "input"]
    assert len(reads) >= client.BUSY_RETRY_MAX_ATTEMPTS


def test_write_to_address_zero_is_allowed():
    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    point = ModbusSetpoint(key=SK.FLAG, write_address=0, max=100)
    assert asyncio.run(client._request_setpoint_write(point, 1)) is True


def test_reads_without_a_client_return_empty_rather_than_raising():
    client = _connected_client()
    client._transport = None
    assert asyncio.run(client._request_setpoint_read(
        [_setpoint(client, SK.FLAG)])) == []
    assert asyncio.run(client._request_datapoint_read(
        [_datapoint(client, DK.COUNTER)])) == []


# ------------------------------------------------- legacy transport compatibility

class _LegacyTransport(ModbusEventConnect):
    """
    A transport shaped like micro_nabto: no super().__init__(), and a synchronous
    _request_setpoint_writes. The base class must keep working with it untouched.
    """
    def __init__(self) -> None:
        self._attr_adapter = _Adapter()
        self.written = []

    @property
    def is_connected(self) -> bool: return True
    def stop(self) -> None: pass
    async def _request_datapoint_read(self, points): return []
    async def _request_setpoint_read(self, points): return []
    def _request_setpoint_writes(self, point_values) -> bool:  # deliberately not async
        self.written.extend(point_values)
        return True


def test_legacy_transport_without_super_init_gets_its_own_subscribers():
    a, b = _LegacyTransport(), _LegacyTransport()
    a.subscribe(DK.COUNTER, lambda k, o, n: None)
    assert DK.COUNTER in a._subscribers
    assert DK.COUNTER not in b._subscribers, "registries must not be shared"


def test_legacy_transport_synchronous_write_is_accepted():
    client = _LegacyTransport()
    client._attr_adapter.load_device_model(_info())
    assert asyncio.run(client.request_setpoint_write(SK.FLAG, 1)) is True
    assert len(client.written) == 1


def test_subscriptions_are_applied_without_the_transport_calling_anything():
    """request_initial_data() is the single place that applies pending subscriptions."""
    client = _LegacyTransport()
    client.subscribe(DK.COUNTER, lambda k, o, n: None)
    client._attr_adapter.load_device_model(_info())
    asyncio.run(client.request_initial_data())
    assert DK.COUNTER in client.get_read_keys()


def test_micro_nabto_stays_compatible_without_being_modified():
    """
    micro_nabto must keep working untouched: it does not call super().__init__() and
    implements _request_setpoint_writes synchronously.

    Not instantiated here - MicroNabtoEventConnect.__init__ opens a MicroNabtoConnection,
    which starts a listening thread that would keep the test session alive.
    """
    import inspect
    from src.modbus_event_connect import MicroNabtoEventConnect

    init_source = inspect.getsource(MicroNabtoEventConnect.__init__)
    assert "super().__init__()" not in init_source, (
        "test premise changed: micro_nabto now calls super().__init__()")
    assert not inspect.iscoroutinefunction(MicroNabtoEventConnect._request_setpoint_writes), (
        "test premise changed: micro_nabto's write is now async")

    # The base class must cope with exactly that shape.
    assert isinstance(ModbusEventConnect.__dict__["_subscribers"], property), (
        "_subscribers must be a lazy per-instance property, not set in __init__")
    assert "isawaitable" in inspect.getsource(ModbusEventConnect.request_setpoint_writes), (
        "request_setpoint_writes must accept a synchronous transport implementation")


def test_reads_do_not_block_the_event_loop():
    """A slow transport must not stall the loop; the client must stay cooperative."""
    async def scenario():
        client = _connected_client()
        client._transport = _RecordingTransport(fail=False, delay=0.3)
        stop = asyncio.Event()

        async def heartbeat():
            ticks = 0
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.02)
            return ticks

        task = asyncio.create_task(heartbeat())
        await client._request_datapoint_read([_datapoint(client, DK.COUNTER)])
        stop.set()
        return await task

    assert asyncio.run(scenario()) >= 5


# ------------------------------------------------- observable busy state

class _BusyThenOk:
    """Reports SLAVE_DEVICE_BUSY for the first `busy_for` calls, then succeeds."""
    def __init__(self, busy_for=2):
        self.busy_for = busy_for
        self.calls = 0
        self._exc = EXCEPTION_SLAVE_DEVICE_BUSY
    @property
    def is_open(self): return True
    @property
    def last_exception_code(self): return self._exc
    @property
    def last_error_text(self): return None
    async def open(self): return True
    async def close(self): return None
    async def _read(self, address, count):
        self.calls += 1
        if self.calls <= self.busy_for:
            self._exc = EXCEPTION_SLAVE_DEVICE_BUSY
            return None
        self._exc = EXCEPTION_NONE
        return [0] * count
    async def read_input_registers(self, address, count): return await self._read(address, count)
    async def read_holding_registers(self, address, count): return await self._read(address, count)
    async def read_discrete_inputs(self, address, count): return await self._read(address, count)
    async def write_register(self, address, value): return await self._read(address, 1) is not None
    async def write_registers(self, address, values): return await self._read(address, 1) is not None


def test_busy_state_is_observable_from_outside():
    """A consumer must be able to see when the device is busy, not just have it retried."""
    client = _connected_client()
    client._transport = _BusyThenOk(busy_for=2)
    client.BUSY_RETRY_INITIAL_DELAY = 0.001

    events = []
    client.subscribe(ModbusStatusKey.DEVICE_BUSY, lambda k, o, n: events.append(n))
    assert client.device_busy is False, "should start idle"

    asyncio.run(client._request_datapoint_read(
        [_datapoint(client, DK.COUNTER)]))

    # subscribe() delivers the current value first, then the transitions
    assert events[0] == 0, "first callback is the value at subscribe time"
    assert 1 in events, "the consumer was never told the device went busy"
    assert events[-1] == 0, "the consumer was never told the device became ready again"
    assert client.device_busy is False


def test_busy_state_clears_when_the_device_recovers():
    client = _connected_client()
    client._transport = _BusyThenOk(busy_for=1)
    client.BUSY_RETRY_INITIAL_DELAY = 0.001
    asyncio.run(client._request_datapoint_read(
        [_datapoint(client, DK.COUNTER)]))
    assert client.device_busy is False
    assert client.get_value(ModbusStatusKey.LAST_EXCEPTION_CODE) == EXCEPTION_NONE


def test_status_keys_do_not_need_a_device_model():
    """Status must be subscribable before connect(), like any other key."""
    client = _Client()
    seen = []
    client.subscribe(ModbusStatusKey.CONNECTED, lambda k, o, n: seen.append(n))
    assert seen == [0], "subscriber should immediately receive the current status"
    assert client.get_value(ModbusStatusKey.CONNECTED) == 0


# ------------------------------------------------- waiting out a busy device

class _BusyForAWhile(_RecordingTransport):
    """Busy for the first `busy_calls` requests, then healthy."""
    def __init__(self, busy_calls=3):
        super().__init__(fail=False)
        self.busy_calls = busy_calls
        self.n = 0
    @property
    def last_exception_code(self):
        return EXCEPTION_SLAVE_DEVICE_BUSY if self.n <= self.busy_calls else EXCEPTION_NONE
    async def _read(self, kind, address, count):
        self.n += 1
        self.calls.append((kind, address, count))
        return None if self.n <= self.busy_calls else [0] * count
    async def write_register(self, address, value):
        self.n += 1
        self.calls.append(("write_register", address, value))
        return True


def test_write_waits_until_the_device_is_ready_again():
    """The library owns this wait: it caused the busy state and only it can see it clear."""
    client = _connected_client()
    client._transport = _BusyForAWhile(busy_calls=3)
    client.BUSY_POLL_INTERVAL = 0.001
    client.BUSY_RETRY_INITIAL_DELAY = 0.001

    seen = []
    client.subscribe(ModbusStatusKey.DEVICE_BUSY, lambda k, o, n: seen.append(n))

    async def scenario():
        return await client.request_setpoint_write(SK.FLAG, 1, wait_for_ready=True)

    assert asyncio.run(scenario()) is True
    assert client.device_busy is False, "write returned while the device was still busy"
    assert 1 in seen and seen[-1] == 0, f"busy transitions not published: {seen}"
    probes = [c for c in client._transport.calls if c[0] == "input"]
    assert probes, "no status probe was issued while waiting"


def test_write_can_return_without_waiting():
    client = _connected_client()
    client._transport = _BusyForAWhile(busy_calls=99)
    client.BUSY_POLL_INTERVAL = 0.001
    client.BUSY_RETRY_INITIAL_DELAY = 0.001

    async def scenario():
        return await client.request_setpoint_write(SK.FLAG, 1, wait_for_ready=False)

    asyncio.run(scenario())   # must not hang waiting for a device that never recovers


def test_await_device_ready_gives_up():
    client = _connected_client()
    client._transport = _BusyForAWhile(busy_calls=10_000)
    client.BUSY_POLL_INTERVAL = 0.001
    client._set_status(ModbusStatusKey.DEVICE_BUSY, 1)
    assert asyncio.run(client.await_device_ready(timeout=0.05)) is False


def test_the_probe_uses_a_register_the_model_guarantees():
    """Probing an address the unit lacks would look like a sick device."""
    client = _connected_client()
    point = client._probe_point()
    assert point is not None
    assert point.key in [p.key for p in client._attr_adapter.get_initial_datapoints_for_read()]


# ------------------------------------------- work-in-progress, visible to a UI

def test_write_pending_brackets_the_whole_write():
    """A UI must be able to disable its inputs for the whole operation."""
    client = _connected_client()
    client._transport = _BusyForAWhile(busy_calls=2)
    client.BUSY_POLL_INTERVAL = 0.001
    client.BUSY_RETRY_INITIAL_DELAY = 0.001

    seen = []
    client.subscribe(ModbusStatusKey.WRITE_PENDING, lambda k, o, n: seen.append(n))
    during = {}

    original = client._request_setpoint_writes
    async def spy(point_values):
        during["pending"] = client.write_pending
        during["accepts"] = client.accepts_writes
        return await original(point_values)
    client._request_setpoint_writes = spy

    asyncio.run(client.request_setpoint_write(SK.FLAG, 1))

    assert during["pending"] is True, "write_pending was not set before touching the wire"
    assert during["accepts"] is False, "accepts_writes stayed True during a write"
    assert seen == [0, 1, 0], f"expected subscribe-value, then on, then off; got {seen}"
    assert client.write_pending is False


def test_write_pending_is_set_even_when_the_device_never_reports_busy():
    """DEVICE_BUSY alone is not enough: a fast device would show a UI nothing at all."""
    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    seen = []
    client.subscribe(ModbusStatusKey.WRITE_PENDING, lambda k, o, n: seen.append(n))
    asyncio.run(client.request_setpoint_write(SK.FLAG, 1))
    assert 1 in seen, "a UI would never have learned that a write happened"
    assert client.device_busy is False, "this device never reported busy"


def test_write_pending_clears_even_if_the_write_raises():
    client = _connected_client()
    client._transport = _RecordingTransport(fail=False)
    async def boom(point_values): raise RuntimeError("transport exploded")
    client._request_setpoint_writes = boom
    try:
        asyncio.run(client.request_setpoint_write(SK.FLAG, 1))
    except RuntimeError:
        pass
    assert client.write_pending is False, "a failed write left the UI disabled forever"


def test_overlapping_writes_keep_write_pending_until_the_last_one_finishes():
    """Rapid +/- taps produce overlapping writes; the first to finish must not clear the flag."""
    class _Staggered(_RecordingTransport):
        def __init__(self):
            super().__init__(fail=False)
            self.writes = 0
        async def write_register(self, address, value):
            self.writes += 1
            await asyncio.sleep(0.01 if self.writes == 1 else 0.20)
            return True

    async def scenario():
        client = _connected_client()
        client._transport = _Staggered()

        async def slow():
            await asyncio.sleep(0.005)
            await client.request_setpoint_write(SK.FLAG, 1)

        task = asyncio.create_task(slow())
        await client.request_setpoint_write(SK.TARGET, 21.0)
        still_running, pending = not task.done(), client.write_pending
        await task
        return still_running, pending, client.write_pending

    still_running, pending_while_running, pending_after = asyncio.run(scenario())
    assert still_running, "test premise: the second write should still be in flight"
    assert pending_while_running, "a UI would have re-enabled inputs mid-write"
    assert pending_after is False, "the flag never cleared"


def test_status_keys_behave_like_any_other_key():
    """Whatever works for a register key must work for a status key."""
    client = _connected_client()
    register, status = DK.COUNTER, ModbusStatusKey.DEVICE_BUSY
    for key in (register, status):
        client.subscribe(key, lambda k, o, n: None)
    for name, call in (
        ("get_value", client.get_value),
        ("has_value", client.has_value),
        ("provides", client.provides),
        ("get_unit_of_measure", client.get_unit_of_measure),
    ):
        reg_result, status_result = call(register), call(status)
        if name in ("has_value", "provides"):
            assert reg_result and status_result, f"{name} disagrees between key kinds"
    values = client.get_values()
    assert register in values, "register key missing from get_values()"
    assert status in values, "status key missing from get_values()"


def test_repeated_writes_to_one_key_collapse_to_the_newest():
    """Tapping + five times must put the final value on the device, not walk it through five."""
    class _Device(_RecordingTransport):
        def __init__(self):
            super().__init__(fail=False)
            self.sent = []
            self.value = None
        async def write_register(self, address, value):
            await asyncio.sleep(0.001)
            self.sent.append(value)
            self.value = value
            return True

    async def scenario():
        client = _connected_client()
        client._transport = _Device()
        await asyncio.gather(*(
            client.request_setpoint_write(SK.TARGET, raw / 100)
            for raw in (2000, 2050, 2100, 2150, 2200)))
        return client._transport

    device = asyncio.run(scenario())
    assert device.value == 2200, "the last value asked for did not end up on the device"
    assert len(device.sent) < 5, f"no coalescing happened: sent {device.sent}"


def test_different_keys_are_never_coalesced_together():
    """A temperature and a humidity setpoint are unrelated; neither may swallow the other."""
    class _Device(_RecordingTransport):
        def __init__(self):
            super().__init__(fail=False)
            self.written = {}
        async def write_register(self, address, value):
            await asyncio.sleep(0.001)
            self.written[address] = value
            return True

    async def scenario():
        client = _connected_client()
        client._transport = _Device()
        await asyncio.gather(
            client.request_setpoint_write(SK.TARGET, 21.0),
            client.request_setpoint_write(SK.FLAG, 1),
        )
        return client._transport.written

    written = asyncio.run(scenario())
    target = client_target = 119   # SK.TARGET write address in the test model
    flag = 26                      # SK.FLAG write address
    assert target in written, "the temperature write was lost"
    assert flag in written, "the flag write was lost"


def test_concurrent_writes_land_in_order_even_when_retried():
    """
    Five rapid taps must leave the device holding the last value.

    Without serialisation a write rejected with SLAVE_DEVICE_BUSY backs off and lands after
    later writes, so the user ends up with an earlier value than the one they asked for.
    """
    class _Device(_RecordingTransport):
        def __init__(self, busy_attempts):
            super().__init__(fail=False)
            self.value = None
            self.attempt = 0
            self.busy_attempts = set(busy_attempts)
            self._exc = EXCEPTION_NONE
        @property
        def last_exception_code(self): return self._exc
        async def read_input_registers(self, address, count):
            self._exc = EXCEPTION_NONE
            return [0] * count
        async def write_register(self, address, value):
            self.attempt += 1
            await asyncio.sleep(0.001)
            if self.attempt in self.busy_attempts:
                self._exc = EXCEPTION_SLAVE_DEVICE_BUSY
                return False
            self._exc = EXCEPTION_NONE
            self.value = value
            return True

    async def scenario(busy_attempts):
        client = _connected_client()
        client._transport = _Device(busy_attempts)
        client.BUSY_RETRY_INITIAL_DELAY = 0.001
        wanted = [2000, 2050, 2100, 2150, 2200]
        await asyncio.gather(*(
            client.request_setpoint_write(SK.TARGET, raw / 100) for raw in wanted))
        return client._transport.value

    assert asyncio.run(scenario(())) == 2200
    assert asyncio.run(scenario((1, 2, 3))) == 2200, (
        "a retried early write overtook a later one")


def test_a_disconnected_peripheral_is_reported_unavailable_not_disabled():
    """
    Exception 0x04 means the register exists but its peripheral is offline - a disconnected
    Calefa, for example. The value must go to None so a UI can show it unavailable, and the
    point must stay enabled so it recovers when the peripheral comes back.
    """
    client = _connected_client()
    client._transport = _RecordingTransport(exception=EXCEPTION_SLAVE_DEVICE_FAILURE)
    point = _datapoint(client, DK.COUNTER)
    client.set_read(DK.COUNTER, True)

    # seed a stale value, so we can tell whether the failure clears it
    client._set_values([(point, 42)])
    assert client.get_value(DK.COUNTER) == 42

    asyncio.run(client.request_datapoint_read())

    assert client.get_value(DK.COUNTER) is None, "a stale value survived the peripheral going offline"
    still_read = [p.key for p in client._attr_adapter.get_datapoints_for_read()]
    assert DK.COUNTER in still_read, "the point was disabled; it would never recover"


# ------------------------------- set_read and subscribe are independent owners

def test_unsubscribing_does_not_cancel_an_explicit_set_read():
    """Two owners of the read flag; neither may switch the other off."""
    client = _connected_client()
    callback = lambda k, o, n: None

    client.set_read(DK.COUNTER)
    assert DK.COUNTER in client.get_read_keys()

    client.subscribe(DK.COUNTER, callback)
    client.unsubscribe(DK.COUNTER, callback)

    assert DK.COUNTER in client.get_read_keys(), (
        "unsubscribing switched off a point that set_read() still wanted")


def test_set_read_false_does_not_silence_a_subscriber():
    client = _connected_client()
    client.subscribe(DK.COUNTER, lambda k, o, n: None)
    client.set_read(DK.COUNTER, False)
    assert DK.COUNTER in client.get_read_keys(), (
        "set_read(False) stopped a subscriber from receiving events")


def test_a_point_stops_being_read_once_neither_owner_wants_it():
    client = _connected_client()
    callback = lambda k, o, n: None
    client.set_read(DK.COUNTER)
    client.subscribe(DK.COUNTER, callback)
    client.unsubscribe(DK.COUNTER, callback)
    client.set_read(DK.COUNTER, False)
    assert DK.COUNTER not in client.get_read_keys()


def test_set_read_before_connect_is_applied_on_connect():
    client = _Client()
    client.set_read(DK.COUNTER)
    client._attr_adapter.load_device_model(_info())
    asyncio.run(client.request_initial_data())
    assert DK.COUNTER in client.get_read_keys()
