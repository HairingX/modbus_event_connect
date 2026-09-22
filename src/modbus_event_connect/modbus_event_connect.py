import asyncio
import inspect
import logging
import time

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Dict, List, Sequence, Set, Tuple

from .modbus_models import MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES, ModbusDatapoint, ModbusParser, ModbusPointKey, ModbusSetpoint, ModbusSetpointKey, ModbusStatusKey
from .modbus_deviceadapter import ModbusDeviceAdapter

_LOGGER = logging.getLogger(__name__)

class ModbusEventConnect(ABC):
    """
    Turns polled register reads into value-change events.

    This class deliberately owns **no timer, thread or polling loop**. The consumer decides
    when to poll - normally from a scheduler it already runs, such as Home Assistant's
    DataUpdateCoordinator - by calling `request_datapoint_read()` / `request_setpoint_read()`.
    Subscribers are then notified only for the points whose value actually changed.

    Transport implementations must not block the event loop while doing I/O.
    """
    _attr_adapter: ModbusDeviceAdapter

    @property
    def _subscribers(self) -> Dict[ModbusPointKey, List[Callable[[ModbusPointKey, MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]]]:
        """
        Per-instance subscriber registry: Callable[key, old_value, new_value].

        Created lazily on first access rather than in __init__, so that a transport which does
        not call super().__init__() still gets its own registry. It used to be a class
        attribute, which silently shared every subscription between all clients in the process.
        """
        subscribers: Dict[ModbusPointKey, List[Callable[[ModbusPointKey, MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]]]|None = getattr(self, "_subscribers_store", None)
        if subscribers is None:
            subscribers = {}
            setattr(self, "_subscribers_store", subscribers)
        return subscribers

    @property
    @abstractmethod
    def is_connected(self) -> bool: 
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    def stop(self) -> Awaitable[None]|None:
        """
        Close the connection.

        May be implemented as `async def` (the TCP transport is) or as a plain `def`
        (micro_nabto is), so callers should `await` the result when it is awaitable.
        """
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    async def _request_datapoint_read(self, points: List[ModbusDatapoint]) -> List[Tuple[ModbusDatapoint, MODBUS_VALUE_TYPES|None]]:
        raise NotImplementedError("Method not implemented")
    @abstractmethod
    async def _request_setpoint_read(self, points: List[ModbusSetpoint]) -> List[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES|None]]:
        raise NotImplementedError("Method not implemented")
    async def _probe_device(self) -> None:
        """
        Make one cheap request whose only purpose is to refresh the status keys.

        Transports that can tell whether the device is busy should override this. The default
        does nothing, which makes await_device_ready() return immediately.
        """
        return None

    @abstractmethod
    def _request_setpoint_writes(self, point_values: Sequence[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]]) -> Awaitable[bool]|bool:
        """
        Write the given setpoints and report whether every write was accepted.

        Preferably implemented as `async def`, so that a blocking transport can keep the
        caller's event loop free. A plain `def` returning bool is also accepted - see
        `request_setpoint_writes()`.
        """
        raise NotImplementedError("Method not implemented")
        
    @property
    def device_info(self): return self._attr_adapter.device_info
    @property
    def manufacturer(self): return self._attr_adapter.manufacturer
    @property
    def model_name(self) -> str: return self._attr_adapter.model_name
    
    
    async def request_initial_data(self) -> None:
        """Request the current value of all points used in initialization, ex. version."""
        # Every transport calls this right after loading the device model, so it is the one
        # place that can apply subscriptions registered before connect() without each
        # transport having to remember to.
        self._apply_subscriptions()
        values:List[Tuple[ModbusDatapoint|ModbusSetpoint, MODBUS_VALUE_TYPES|None]] = []
        datapoints = self._attr_adapter.get_initial_datapoints_for_read()
        if len(datapoints) > 0: 
            values.extend(await self._request_datapoint_read(datapoints))
        setpoints = self._attr_adapter.get_initial_setpoints_for_read()
        if len(setpoints) > 0: 
            values.extend(await self._request_setpoint_read(setpoints))
        self._set_values(values)
    
    async def request_datapoint_read(self) -> None:
        """Request the current value of all subscribed datapoints. All subscribers will be notified of the new value if changed."""
        points = self._attr_adapter.get_datapoints_for_read()
        if len(points) == 0: return
        self._set_values(await self._request_datapoint_read(points))
            
    async def request_setpoint_read(self) -> None:
        """Request the current value of all subscribed setpoints. All subscribers will be notified of the new value if changed."""
        points = self._attr_adapter.get_setpoints_for_read()
        if len(points) == 0: return
        self._set_values(await self._request_setpoint_read(points))
    
    BUSY_POLL_INTERVAL = 0.2
    """Seconds between probes while waiting for the device to stop being busy."""
    BUSY_POLL_TIMEOUT = 10.0
    """Give up waiting for the device to become ready after this many seconds."""

    async def await_device_ready(self, timeout: float|None = None) -> bool:
        """
        Wait until the device stops reporting SLAVE_DEVICE_BUSY. Returns True if it is ready.

        This is a bounded wait for a condition, not a polling loop: it is caused by something
        this client just did, it ends on its own, and only this client can see when the device
        recovers. Deciding *how often* to read the device is still entirely the caller's.

        A probe costs a single register read, so waiting is far cheaper than guessing.
        """
        if timeout is None: timeout = self.BUSY_POLL_TIMEOUT
        deadline = time.monotonic() + timeout
        # Probe before trusting the flag. A write that the device accepted says nothing about
        # whether it is busy *now* - persisting that very change is what makes it busy.
        await self._probe_device()
        while self.device_busy:
            if time.monotonic() >= deadline:
                _LOGGER.warning(f"Device still busy after {timeout:.0f}s")
                return False
            await asyncio.sleep(self.BUSY_POLL_INTERVAL)
            await self._probe_device()
        return True

    async def request_setpoint_write(self, key: ModbusSetpointKey, value: MODBUS_VALUE_TYPES, *,
                                     wait_for_ready: bool = False) -> bool:
        """
        Write a new value to a setpoint. Returns True if the write was accepted.

        Returns as soon as the device accepts the write; it does **not** wait for the device
        to finish persisting the change. Waiting would stall every other request for as long
        as the device stays busy, and it buys nothing: the value has to be read back anyway,
        because devices may clamp or step-align it.

        The expected pattern is the one Home Assistant's own Modbus integration uses - write,
        show the new value optimistically, and refresh on your own schedule:

            await client.request_setpoint_write(key, 21.5)
            await coordinator.async_request_refresh()   # coalesced by the host

        Pass `wait_for_ready=True` only when the next step genuinely cannot start until the
        device has settled, such as a scripted sequence of dependent writes.

        Repeated writes to the **same** setpoint collapse: only the newest value is sent, so
        five rapid taps put the final value on the device rather than walking it through five.
        Different setpoints are independent and never collapse into each other.

        The written value is deliberately NOT cached, so a read-back reports what the device
        actually stored rather than what was asked for.
        """
        return await self.request_setpoint_writes([(key, value)], wait_for_ready=wait_for_ready)

    async def request_setpoint_writes(self, kv: Sequence[Tuple[ModbusSetpointKey, MODBUS_VALUE_TYPES]], *,
                                      wait_for_ready: bool = False) -> bool:
        """Write new values to multiple setpoints. Returns True only if every write was accepted."""
        point_values = list[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]]()
        all_resolved = True
        for key, value in kv:
            setpoint = self._attr_adapter.get_setpoint(key)
            if setpoint is None:
                _LOGGER.error(f"Failed to write data for '{key}', the setpoint is not available.")
                all_resolved = False
                continue
            point_values.append((setpoint, value))

        if len(point_values) == 0: return False
        # Announce before touching the wire, so a UI can disable its inputs for the whole
        # operation rather than only for the part the device happens to report as busy.
        # Counted, not a flag: rapid +/- taps produce overlapping writes, and the first to
        # finish must not clear the state while another is still in flight.
        setattr(self, "_writes_in_flight_count", self._writes_in_flight + 1)
        self._set_status(ModbusStatusKey.WRITE_PENDING, 1)
        try:
            # Record what each register should end up holding. A later request for the same
            # register replaces an earlier one that has not been sent yet: tapping + five
            # times should put the final value on the device, not walk it through five.
            for point, value in point_values:
                self._pending_writes[point.key] = value

            to_send: list[Tuple[ModbusSetpoint, MODBUS_VALUE_TYPES]] = []
            locks = [self._key_write_lock(point.key) for point, _ in point_values]
            for lock in locks:
                await lock.acquire()
            try:
                for point, _ in point_values:
                    # Whatever is pending now is the newest value asked for. If it is gone,
                    # a concurrent call already sent it and there is nothing left to do.
                    if point.key in self._pending_writes:
                        to_send.append((point, self._pending_writes.pop(point.key)))
                written = True
                if to_send:
                    # Transports may implement this synchronously (micro_nabto does) or async.
                    written = self._request_setpoint_writes(to_send)
                    if inspect.isawaitable(written):
                        written = await written
            finally:
                for lock in locks:
                    lock.release()
            if wait_for_ready:
                # Read the device straight away: persisting the change is what makes it busy,
                # so its state right after a write is the only one worth knowing.
                await self.await_device_ready()
            return bool(written) and all_resolved
        finally:
            setattr(self, "_writes_in_flight_count", max(0, self._writes_in_flight - 1))
            if self._writes_in_flight == 0:
                self._set_status(ModbusStatusKey.WRITE_PENDING, 0)
        
    def has_value(self, key: ModbusPointKey) -> bool:
        """Check if the device has a value for a datapoint, setpoint or status key."""
        if isinstance(key, ModbusStatusKey):
            return key in self._status
        return self._attr_adapter.has_value(key)
    def get_value(self, key: ModbusPointKey) -> MODBUS_VALUE_TYPES|None:
        """Get the value of a datapoint, setpoint or status key."""
        if isinstance(key, ModbusStatusKey):
            return self._status.get(key)
        return self._attr_adapter.get_value(key)
    def get_min_value(self, key: ModbusSetpointKey) -> float|int|None:
        """Get the minimum value of a setpoint."""
        return self._attr_adapter.get_min_value(key)
    def get_max_value(self, key: ModbusSetpointKey) -> float|int|None:
        """Get the maximum value of a setpoint."""
        return self._attr_adapter.get_max_value(key)
    def get_unit_of_measure(self, key: ModbusPointKey) -> str|None:
        """Get the unit of measure of a datapoint or setpoint."""
        return self._attr_adapter.get_unit_of_measure(key)
    def get_setpoint_step(self, key: ModbusSetpointKey) -> float|int:
        """Get the step size of a setpoint."""
        return self._attr_adapter.get_setpoint_step(key)
    @property
    def _explicit_reads(self) -> Set[ModbusPointKey]:
        """Points a caller asked to read directly, as opposed to by subscribing."""
        requested: Set[ModbusPointKey]|None = getattr(self, "_explicit_reads_store", None)
        if requested is None:
            requested = set()
            setattr(self, "_explicit_reads_store", requested)
        return requested

    def set_read(self, key: ModbusPointKey, read: bool = True) -> bool:
        """
        Include or exclude a point from the next read, without subscribing to it.

        For a consumer that polls values and reads them with `get_value()` rather than
        reacting to callbacks - a coordinator building a snapshot, for instance.

        This and `subscribe()` are independent: a point is read if either wants it. Turning
        this off does not stop a subscriber's events, and a subscriber leaving does not stop a
        point this was asked to read. Returns whether the point exists on this device.

        Status keys are produced by the client, not read from the device, so they are ignored.
        """
        if isinstance(key, ModbusStatusKey):
            return True
        if read:
            self._explicit_reads.add(key)
        else:
            self._explicit_reads.discard(key)
        if not self._attr_adapter.has_model:
            # Remembered anyway; _apply_subscriptions() applies it once a model is loaded.
            return False
        return self._sync_read_flag(key)

    def _sync_read_flag(self, key: ModbusPointKey) -> bool:
        """Read a point if either a subscriber or an explicit set_read() wants it."""
        wanted = key in self._explicit_reads or bool(self._subscribers.get(key))
        return self._attr_adapter.set_read(key, wanted)

    def get_read_keys(self) -> List[ModbusPointKey]:
        """Every point that the next read will fetch."""
        if not self._attr_adapter.has_model:
            return []
        keys: List[ModbusPointKey] = [p.key for p in self._attr_adapter.get_datapoints_for_read()]
        keys += [p.key for p in self._attr_adapter.get_setpoints_for_read()]
        return keys

    def provides(self, key: ModbusPointKey) -> bool:
        """Check if this client provides a datapoint, setpoint or status key."""
        if isinstance(key, ModbusStatusKey):
            # Status is produced by the client itself, so it is always available.
            return key in self._status
        return self._attr_adapter.provides(key)
    def get_values(self) -> Dict[ModbusPointKey, MODBUS_VALUE_TYPES|None]:
        """Get the values of all read datapoints and setpoints, plus the status keys."""
        values: Dict[ModbusPointKey, MODBUS_VALUE_TYPES|None] = dict(self._attr_adapter.get_values())
        # Explicit loop: Dict is invariant in its key type, so a Dict[ModbusStatusKey, ...]
        # is not assignable to a Dict[ModbusPointKey, ...] even though the keys are a subtype.
        for status_key, status_value in self._status.items():
            values[status_key] = status_value
        return values
    
    @property
    def _status(self) -> Dict[ModbusStatusKey, MODBUS_VALUE_TYPES|None]:
        """Connection status values, created lazily like _subscribers."""
        status: Dict[ModbusStatusKey, MODBUS_VALUE_TYPES|None]|None = getattr(self, "_status_store", None)
        if status is None:
            status = {
                ModbusStatusKey.CONNECTED: 0,
                ModbusStatusKey.DEVICE_BUSY: 0,
                ModbusStatusKey.WRITE_PENDING: 0,
                ModbusStatusKey.LAST_EXCEPTION_CODE: 0,
            }
            setattr(self, "_status_store", status)
        return status

    @property
    def _writes_in_flight(self) -> int:
        count: int = getattr(self, "_writes_in_flight_count", 0)
        return count

    def _key_write_lock(self, key: ModbusSetpointKey) -> asyncio.Lock:
        """
        One lock per setpoint, so writes to the same register cannot land out of order.

        Only same-key writes are serialised. A temperature and a humidity setpoint are
        unrelated and may go concurrently; what must not happen is a retried write to one
        register overtaking a later write to that same register.
        """
        locks: Dict[ModbusSetpointKey, asyncio.Lock]|None = getattr(self, "_write_locks", None)
        if locks is None:
            locks = {}
            setattr(self, "_write_locks", locks)
        lock = locks.get(key)
        if lock is None:
            lock = locks[key] = asyncio.Lock()
        return lock

    @property
    def _pending_writes(self) -> Dict[ModbusSetpointKey, MODBUS_VALUE_TYPES]:
        """The newest value requested per setpoint, until it is actually sent."""
        pending: Dict[ModbusSetpointKey, MODBUS_VALUE_TYPES]|None = getattr(self, "_pending_writes_store", None)
        if pending is None:
            pending = {}
            setattr(self, "_pending_writes_store", pending)
        return pending

    @property
    def write_pending(self) -> bool:
        """
        True while this client has at least one write in flight, including the wait after it.

        Counted rather than a flag, so that overlapping writes - which rapid +/- taps produce -
        only clear it once the last of them has finished.
        """
        return bool(self._status[ModbusStatusKey.WRITE_PENDING])

    @property
    def accepts_writes(self) -> bool:
        """
        True when it makes sense to write right now.

        What a UI should disable its inputs on: connected, no write of ours in flight, and the
        device not reporting busy.
        """
        return (bool(self._status[ModbusStatusKey.CONNECTED])
                and not self.write_pending and not self.device_busy)

    @property
    def device_busy(self) -> bool:
        """
        True while the device is answering SLAVE_DEVICE_BUSY (0x06).

        A consumer that queues writes should wait for this to clear rather than stacking
        requests up; subscribe to ModbusStatusKey.DEVICE_BUSY to be told when it does.
        """
        return bool(self._status[ModbusStatusKey.DEVICE_BUSY])

    def _set_status(self, key: ModbusStatusKey, value: MODBUS_VALUE_TYPES|None) -> None:
        """Publish a status change to subscribers, on change only."""
        old_value = self._status.get(key)
        if old_value == value: return
        self._status[key] = value
        self._notify_subscribers({key: (old_value, value)})

    def subscribe(self, key: ModbusPointKey, update_method: Callable[[ModbusPointKey, MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]):
        """
            Subscribe to a datapoint or setpoint value change.
            
            :param key: The key of the datapoint or setpoint to subscribe to.
            :param update_method: The method to call when the value changes. The Callable will receive the key, old_value and new_value as the inputs.
            """
        if key not in self._subscribers:
            self._subscribers[key] = []
        self._subscribers[key].append(update_method)
        if isinstance(key, ModbusStatusKey):
            # Status is pushed by the transport, not read from a register.
            update_method(key, None, self._status.get(key))
            return
        if not self._attr_adapter.has_model:
            # Subscribing before connect() is allowed; the read flags are applied to the
            # device model as soon as it is loaded. See _apply_subscriptions().
            return
        self._sync_read_flag(key)
        value = self._attr_adapter.get_value(key)
        if value is not None:
            update_method(key, None, value)

    def unsubscribe(self, key: ModbusPointKey, update_method: Callable[[ModbusPointKey, MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None], None]):
        """Remove a subscription to a datapoint or setpoint value change."""
        subscribers = self._subscribers.get(key)
        if subscribers is None: return
        if update_method in subscribers:
            if len(subscribers) == 1:
                del self._subscribers[key]
                if not isinstance(key, ModbusStatusKey) and self._attr_adapter.has_model:
                    # Not simply off: an explicit set_read() may still want this point.
                    self._sync_read_flag(key)
            else:
                subscribers.remove(update_method)

    def _apply_subscriptions(self) -> None:
        """
        Apply every existing subscription's read flag to the loaded device model.

        Called from `request_initial_data()`, so callbacks registered before connect() still
        cause their points to be read. Safe to call more than once.
        """
        if not self._attr_adapter.has_model: return
        for key in set(self._subscribers) | self._explicit_reads:
            if isinstance(key, ModbusStatusKey): continue
            if not self._sync_read_flag(key):
                _LOGGER.warning(f"Key '{key}' is not provided by this device model")
    
    def _parse_point_read_value(self, point: ModbusDatapoint|ModbusSetpoint, values: List[int]) -> MODBUS_VALUE_TYPES|None:
        """
        Parse the read value to the correct value type. 
        The values are the raw values from the register read, each item is a register address.
        A value can be a single value or a list of values, depending on the number of addresses the point value is stored in.
        """
        return ModbusParser.values_to_value(values, point)
    
    
    def _parse_point_write_value(self, point: ModbusSetpoint, values: MODBUS_VALUE_TYPES) -> List[int]|None:
        """
        Parse the write value to the correct value type. 
        The values are the raw values from the register read, each item is a register address.
        A value can be a single value or a list of values, depending on the number of addresses the point value is stored in.
        """
        return ModbusParser.value_to_values(values, point)

    def _set_values(self, kv: List[Tuple[MODBUS_POINT_TYPE, MODBUS_VALUE_TYPES|None]]) -> None:
        result = self._attr_adapter.set_values([(point.key, value) for point, value in kv])
        self._notify_subscribers(result)
    
    def _notify_subscribers(self, kv: Dict[ModbusPointKey, Tuple[MODBUS_VALUE_TYPES|None, MODBUS_VALUE_TYPES|None]]) -> None:
        """
        Notify subscribers of the points whose value changed.

        A point whose value is unchanged since the previous read raises no event - that is the
        difference between an event-driven client and a plain poll. A value going to None
        (the device returned its invalid-value sentinel, or the read failed) IS a change and
        is reported, so consumers can mark an entity unavailable.
        """
        for key, (old_value, new_value) in kv.items():
            if old_value == new_value:
                continue
            subscribers = self._subscribers.get(key)
            if subscribers is None:
                # No subscriber for this key; keep going, later keys may have one.
                continue
            for subscriber in subscribers:
                try:
                    subscriber(key, old_value, new_value)
                except Exception:
                    # One misbehaving consumer callback must not stop delivery to the others.
                    _LOGGER.exception(f"Subscriber for '{key}' raised an exception")

    def _handle_invalid_address(self, point: ModbusDatapoint|ModbusSetpoint) -> None:
        """
        Stop reading a point the device says does not exist.

        Logged at INFO, not ERROR: a register map covers everything a model *can* have, and a
        given unit will legitimately be missing most of it - unconfigured rooms, absent
        peripherals, features its hardware profile does not support. That is normal, and it
        would otherwise produce hundreds of error lines on the first read.
        """
        address = f"{point.read_address}"
        if point.read_length != 1 and point.read_address is not None:
            address += f"-{point.read_address + point.read_length - 1}"
        _LOGGER.info(f"'{point.key}' is not available on this device (address {address}); "
                     f"it will not be read again.")
        self._attr_adapter.set_read(point.key, False, force=True)