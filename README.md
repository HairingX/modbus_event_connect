# Modbus Event Connect

Read and write devices over **Modbus** and **micro_nabto** in Python, and be told when a value
changes.

A device is described once, as a *model*. The library then takes care of the rest:

- reading only what is needed, as often as it is needed, in as few requests as possible
- telling you when a value changes
- checking a value before it is written
- retrying and recovering when the device is busy or goes away
- saying how trustworthy every value is, so "offline", "no reading" and a real value never
  look alike

## Which part is for you

| You are... | Read |
|---|---|
| Building an application for a device that already has a model | [Part 1: Using a device](#part-1-using-a-device) |
| Describing a new device: its registers, units and settings | [Part 2: Describing a device](#part-2-describing-a-device) |
| Working on the library itself | [docs/design.md](docs/design.md) |

```bash
pip install modbus-event-connect
```

## How the pieces fit

```text
Model   what the device has: its points, where they live, how to decode them   (Part 2)
Device  how to reach it: ModbusDevice or MicroNabtoDevice                      (Part 1)
Client  joins the two: connect, subscribe, poll, write                          (Part 1)
```

A *point* is one value the device has, such as a temperature or a setting. Its *key*, like
`"temperature"`, is how you name it everywhere.

---

# Part 1: Using a device

## A complete program

```python
import asyncio
from modbus_event_connect import Client
from modbus_event_connect.modbus import ModbusDevice
from my_devices import THERMOSTAT          # a model, see Part 2

def on_change(key, old, new):
    print(f"{key}: {new.value} ({new.quality.name})")

async def main():
    client = Client(ModbusDevice.tcp("<device-ip>"), THERMOSTAT)
    await client.connect()                  # reads every point once
    client.subscribe("temperature", on_change)
    await client.write("target", 21.5)
    try:
        while True:
            await client.poll()             # reads whatever is due
            await asyncio.sleep(1)
    finally:
        await client.disconnect()

asyncio.run(main())
```

The client never reads on its own. **Your program calls `poll()`**, and each call reads the
points that are due. A call when nothing is due returns at once without contacting the device,
so calling it every second is cheap.

`client.seconds_until_next_poll()` says how long until something is due, if you would rather
sleep longer. A subscription made while you sleep can make a point due sooner, so wake up at
least as often as your fastest poll rate.

## Connecting

```python
from modbus_event_connect.modbus import ModbusDevice, ModbusTcpConnection
from modbus_event_connect.micro_nabto import MicroNabtoDevice

# Modbus TCP, one device
device = ModbusDevice.tcp("<device-ip>", unit_id=1)

# Several Modbus devices behind one gateway, sharing its connection
gateway = ModbusTcpConnection("<gateway-ip>")
heating = ModbusDevice(gateway, unit_id=1)
blinds = ModbusDevice(gateway, unit_id=3)

# micro_nabto; with device_id, the device is found again if its address changes
ventilation = MicroNabtoDevice.udp("<email-paired-with-the-device>", host="<device-ip>",
                                   device_id="<device-id>")
```

Behind a gateway, a device that stops answering does not slow down the others.

A micro_nabto device says what it is when you connect. Give the client a function instead of a
model, and it picks the model from that:

```python
def pick_model(identity):
    return VENTILATION if identity["device_model"] == 1140 else None

client = Client(ventilation, pick_model)
```

`connect()` raises when it cannot finish:

| Error | Meaning |
|---|---|
| `CannotConnectError` | The device could not be reached, or did not answer every read. Try again later. |
| `AuthenticationError` | A micro_nabto device refused the email. Ask the user to check it. |
| `UnsupportedDeviceError` | The device answered, but no model matches it. |

## Reading values

`client.subscribe(key, callback)` calls `callback(key, old, new)` at once with the current
value, then on every change. `old` is `None` the first time. It returns a function that
unsubscribes.

`client.value(key)` gives the current value at any time, or `None` before the first read.

Every value is a `DataValue` with `.value`, `.quality` and `.timestamp`:

| Quality | Meaning | Show it as |
|---|---|---|
| `GOOD` | A real value. | the value |
| `NO_DATA` | The device says it has no reading, such as a sensor not fitted. | unknown |
| `STALE` | The last read failed. `.value` and `.timestamp` are the last good read's. | the value, or unavailable |
| `OFFLINE` | Something behind the device is not answering. It will come back. | unavailable |
| `MISSING` | This device does not have the value. | leave it out |

## How often values are read

Every point is read once when connecting. After that, a point is read on its poll rate while
someone subscribes to it:

| Poll rate | Read every |
|---|---|
| `FAST` | 10 s |
| `MEDIUM` (the default) | 30 s |
| `SLOW` | 60 s |
| `RARE` | 15 min |
| `STATIC` | once, when connecting |

These are the library's defaults; a model may set its own. You can change them:

```python
client.set_poll_interval(PollRate.FAST, 5)       # every FAST point
client.set_poll_interval("temperature", 2)       # one point
client.set_poll_interval("temperature", None)    # back to the model's interval
```

A model can set a floor, the shortest interval its device copes with. `set_poll_interval`
returns the interval actually used.

To read now, call `await client.refresh(["temperature"])`. `refresh(PollRate.SLOW)` reads a
whole poll rate, and `refresh()` reads everything. `subscribe(key, callback, poll=False)` is
told about changes without asking for the point to be read on a timer.

## Writing

```python
accepted = await client.write("target", 21.5)
```

`write` returns whether the device accepted the value. Before anything is sent, the value is
checked against the point's limits, and `InvalidValueError` says why it was refused.

- Writes are sent one at a time, in order.
- If a setting is written several times while earlier writes are still waiting, only the newest
  value is sent. Commands are always all sent.
- After a write, the point is read back, so subscribers see what the device really did.
- `await client.write_sequence([("mode", 2), ("target", 21.5)])` checks every value first,
  then writes them in order and stops at the first refusal.
- `client.write_pending` is `True` while writes are waiting or being sent.

`Client(device, model, read_only=True)` refuses every write with `ReadOnlyError` before it
reaches the device. Use it while developing against a real installation.

## Connection state

After `connect()`, the client follows whether the device answers. You do not need to connect
again after an outage.

- `client.connected` is `False` while the device does not answer.
- `client.subscribe(Status.CONNECTED, callback)` tells you when that changes.
- When the device answers again, every polled point is read at once.
- `await client.disconnect()` lets go of the device.

## What this unit has

A model can describe several variants, and parts that may or may not be installed, such as
rooms. After `connect()`:

| | |
|---|---|
| `client.keys` | the keys this unit has |
| `client.has(key)`, `client.can_write(key)` | whether it has the key, and whether it can be written |
| `client.instances("room")` | which rooms, zones or channels are installed, such as `(1, 3)` |
| `client.unavailable_reasons` | keys the unit does not have, with the reason |
| `await client.rescan()` | find out again, for example after a room was added |

## In a larger application

- Run the poll loop as a task on the application's event loop. When the device is removed,
  cancel the task, then `await client.disconnect()`.
- Subscription callbacks run on that event loop, from within the client's own calls, never on
  another thread, so they may update the application's state directly.
- Turn the errors `connect()` raises into the application's own: try again later after
  `CannotConnectError`, ask the user for new credentials after `AuthenticationError`.
- How a value's quality is shown is the application's choice. `client.consecutive_failures(key)`
  lets it wait a few failed reads before calling a `STALE` value unavailable.

## Errors

| Error | Raised by | When |
|---|---|---|
| `CannotConnectError` | `connect()`, `rescan()` | The device could not be reached, or left a read unanswered. |
| `AuthenticationError` | `connect()` | A micro_nabto device refused the email. |
| `UnsupportedDeviceError` | `connect()` | No model matches the device. |
| `NotConnectedError` | most methods | Used before `connect()` succeeded. |
| `InvalidValueError` | `write()` | The point cannot take the value. |
| `ReadOnlyError` | `write()` | The client is read-only. |
| `KeyError` | `subscribe()`, `write()` | This unit has no such key. |

---

# Part 2: Describing a device

A model is plain data: a list of points, each saying where its value lives and how to decode
it. You write it once, from the device's manual, and every app uses it.

## A first model

```python
from modbus_event_connect import DataType, Limits, Model, Point, PollRate, Section, Unit
from modbus_event_connect.modbus import HoldingRegister, InputRegister, ModbusOptions, plain

THERMOSTAT = Model(name="Thermostat", manufacturer="Example",
                   options=ModbusOptions(numbering=plain(first_address=1)),  # the manual gives addresses
                   read_back_after=2.0,   # seconds before a write shows in a read
                   sections=[Section([
    Point("temperature",
          read=InputRegister(10),         # where the value is
          data_type=DataType.INT16,       # a signed 16-bit number
          scale=0.1,                      # the device sends 215 for 21.5
          unit=Unit.CELSIUS,
          no_data=(0x7FFF,)),             # what the device sends when it has no reading
    Point("target",
          read=HoldingRegister(20), write=HoldingRegister(20),
          data_type=DataType.INT16, scale=0.1, unit=Unit.CELSIUS,
          limits=Limits(min=5, max=30, step=0.5)),   # checked before anything is written
    Point("serial_number",
          read=InputRegister(1), data_type=DataType.UINT32,
          poll_rate=PollRate.STATIC),     # read once, when connecting
                   ])])
```

A mistake in a point, such as limits on a point that cannot be written, raises `ValueError`
when the point is created, naming every problem at once. A mistake between points, such as two
overlapping, raises `ModelError` from `connect()`; test for it first, see
[Testing a model](#testing-a-model).

**Keys are forever.** Applications store them, for example in the ids of what they build. Choose them
carefully, and never rename one.

## Where a value lives

| Protocol | Read side | Write side |
|---|---|---|
| Modbus | `InputRegister`, `HoldingRegister`, `DiscreteInput`, `Coil` | `HoldingRegister`, `Coil` |
| micro_nabto | `DatapointRegister`, `SetpointRegister` | `SetpointRegister` |

A point has a read side, a write side, or both. They may differ, for a device that reports a
setting in one place and takes it in another.

**Every model states** its protocol's options, `ModbusOptions(...)` or `MicroNabtoOptions()`,
and `read_back_after`, see [Writing](#writing). A model without them cannot be created.

**Modbus addresses.** Manuals number registers in different ways, and a wrong guess shifts
every value by one register while still looking plausible. So there is no default: the model
says once how its manual numbers registers: which address its first register is.

| The manual | `numbering=` |
|---|---|
| gives the addresses themselves: register 1 is address 1 | `plain(first_address=1)` |
| counts from 1, register 1 being address 0 | `plain(first_address=0)` |
| uses 0xxxx to 4xxxx, 40001 being address 0 (Modicon) | `modicon(digits=5, first_address=0)` |
| uses 0xxxx to 4xxxx, 40001 being address 1 | `modicon(digits=5, first_address=1)` |
| uses 000001 to 465536, 400001 being address 0 | `modicon(digits=6, first_address=0)` |
| uses 000001 to 465535, 400001 being address 1 | `modicon(digits=6, first_address=1)` |
| numbers its own way | a `RegisterNumbering` of your own, as below |

A `RegisterNumbering` lists, for each table, ranges of numbers and the address each range
starts at. A manual that continues a table in another range gets one `NumberRange` for each.

```python
from modbus_event_connect.modbus import ModbusOptions, NumberRange, RegisterNumbering

NUMBERING = RegisterNumbering(
    input_registers=[NumberRange(first=30001, last=39999, address=0)],
    holding_registers=[NumberRange(first=40001, last=49999, address=0)],
)
Model(..., options=ModbusOptions(numbering=NUMBERING))

Point("temperature", read=InputRegister(30011))     # sent as address 10
```

A number in none of its table's ranges is a mistake in the model. It is reported before
anything is read, never guessed at.

`ModbusOptions` also says how many registers the device takes in one read. The Modbus
specification allows 125 registers or 2000 bits; set `max_registers` lower for a device that
takes fewer. Neighbouring points are read together, up to that limit.

**micro_nabto** points name an object and an address, `DatapointRegister(27, obj=0)`, as the
device's documentation gives them.

## How a value is decoded

| Field | What it does |
|---|---|
| `data_type` | `UINT16` (the default), `INT16`, `UINT32`, `INT32`, `UINT64`, `INT64`, `FLOAT32`, `FLOAT64`, `BCD16`, `BCD32`, `BOOL` |
| `DataType.bit(3)` | one bit of a register |
| `DataType.enum({0: "off", 1: "heat"})` | a number that means a word |
| `DataType.string(8)` | text over 8 registers |
| `word_order`, `byte_order` | for values over several registers; high word and big-endian by default |
| `scale`, `offset` | value = raw × scale + offset |
| `precision` | decimals to round to; by default, enough for `scale` and `offset` |
| `transform` | a conversion after scaling, such as `Transforms.SECONDS_AS_MINUTES` |
| `no_data`, `raw_range` | raw values that mean "no reading"; they read as `NO_DATA` |

A `FLOAT32` or `FLOAT64` that reads NaN or infinity is `NO_DATA` by itself.

## Units

`unit=Unit.CELSIUS` and so on. Units are written with their international symbols, following
the SI's rules (°C, kW·h, m³/h). For a unit the SI does not define, the symbol is UCUM's:
`wk`, `mo`, `a`.

`str(Unit.KILOWATT_HOUR)` is its symbol, `kW·h`. `Unit.KILOWATT_HOUR.code` is its
[UCUM](https://ucum.org) code, `kW.h`, plain ASCII for machines.

Convert to the unit a person expects in the model, not in the app: a device that counts minutes
for a setting people think of in hours gets `transform=Transforms.MINUTES_AS_HOURS` and
`unit=Unit.HOURS`.

## How often

| Field | What it does |
|---|---|
| `poll_rate` | `FAST`, `MEDIUM` (the default), `SLOW`, `RARE` or `STATIC`; see the table in Part 1 |
| `poll_always=True` | read even when no one subscribes, for a value the model itself relies on |
| `deadband=0.2` | changes smaller than this are not reported |
| `Model(poll_intervals=...)` | this device's own seconds per poll rate |
| `Model(min_poll_interval=5)` | the shortest interval this device copes with; apps cannot go below it |

Pick the rate from how fast the value changes, not from how often it is looked at:
temperatures `MEDIUM`, on/off states `FAST`, settings `SLOW`, firmware versions `STATIC`.

## Writing

| Field | What it does |
|---|---|
| `limits=Limits(min=5, max=30, step=0.5)` | refuses anything else, in the units a user sees |
| `write_kind=WriteKind.STATE` | a setting (the default): writes that queue up collapse to the newest |
| `write_kind=WriteKind.COMMAND` | an action: every write is sent |
| `pulse=Pulse(idle=0, after=1.0)` | a `COMMAND` that is written back to `idle` after 1 s |
| `read_back_after=5.0` | this point takes longer than its device to show a write |
| `on_write=Refresh(["mode", "target"])` | also read these after writing, since the write changes them |
| `on_change=Refresh(Labels(room=3))` | read these when this point's value changes |

After a write, the written point is read back, so apps see what the device holds. How long a
device takes before a read shows a write differs from device to device, and only you can know
it for yours. So every model states it: `Model(read_back_after=...)`, in seconds. A point that
is slower than the rest of its device gets its own `read_back_after`.

A point with `on_write` is read back together with its targets, after the same delay, or after
`Refresh(targets, after=...)` seconds. With `until_stable=30`, the targets are read again while
they keep changing, for up to 30 s, for a device that moves slowly to a new value.

**Measure it** on your device, with a point you may change and two values it may take:

```python
from modbus_event_connect.testing import measure_read_back

measured = await measure_read_back(client, "target", (20.0, 21.0), delays=(1, 2, 3, 4, 5))
print(measured)
```

```text
target: read back after a write
       1 s   0 of 3
       2 s   0 of 3
       3 s   3 of 3
       4 s   3 of 3
       5 s   3 of 3
  read_back_after=3
```

It writes to the device, so use a client that is not read-only, on a device you may change. It
writes the point's old value back at the end. Before each measured write it writes the other
value and waits the longest delay, so a late earlier write cannot pass for a quick one.

## Variants of a device

A model can cover several variants. The *identity* says which one this unit is: what the
device reported when connecting, plus any `identity_points` the model reads first.

```python
Model(...,
      identity_points=[Point("firmware", read=InputRegister(0), poll_rate=PollRate.STATIC)],
      sections=[Section(common_points),
                Section(cooling_points, when=lambda identity: identity["firmware"] >= 20)])
```

For devices so different that they deserve their own model, give the client a function that
picks one from the identity, as shown in [Connecting](#connecting).

## Repeated parts, and what is installed

A device with rooms, zones or channels describes one of them once. A scan step, run when
connecting, finds out which ones this installation has:

```python
from modbus_event_connect import Instances, Labels, Scan

def room(n):
    return [Point(f"room_{n}_installed", read=InputRegister(100 + 10 * n),
                  poll_rate=PollRate.STATIC),
            Point(f"room_{n}_temperature", read=InputRegister(101 + 10 * n),
                  data_type=DataType.INT16, scale=0.1, unit=Unit.CELSIUS)]

async def skip_empty_rooms(scan: Scan):
    for n in range(1, 9):
        installed = await scan.read([f"room_{n}_installed"])
        if installed[f"room_{n}_installed"].value == 0:
            scan.set_available(Labels(room=n), False, reason="no room")

HEATING = Model(name="Heating", manufacturer="Example",
                options=ModbusOptions(numbering=plain(first_address=1)), read_back_after=2.0,
                sections=[Instances(room, range(1, 9), label="room")],
                scan_steps=[skip_empty_rooms])
```

Every point `room(n)` returns is labelled `room=n`, so `Labels(room=3)` selects room 3's points.
After `connect()`, `client.instances("room")` lists the installed rooms, and `client.keys` has
only their points.

Scan steps run in order. If one of their reads goes unanswered, `connect()` fails rather than
guess what is installed.

## Testing a model

Test a model without the device. `assert_models_valid` resolves each model against every
identity you expect to meet, and fails listing every problem: overlapping registers, a refresh
naming a key that does not exist, a transform that does not convert back, a section no identity
includes.

```python
from modbus_event_connect.testing import (SimulatedModbusDevice, SimulatedModbusGateway,
                                          assert_models_valid)

def test_models_are_valid():
    assert_models_valid(THERMOSTAT, identities=[{}])

async def test_temperature_is_read():
    device = SimulatedModbusDevice(input_registers={1: 0, 2: 1234, 10: 215},
                                   holding_registers={20: 210})
    client = Client(ModbusDevice(SimulatedModbusGateway({1: device})), THERMOSTAT)
    await client.connect()
    assert client.value("temperature").value == 21.5
```

`SimulatedModbusDevice` refuses an unknown address with exception 0x02, Illegal Data Address,
as the Modbus specification says, and can be made busy or silent; `SimulatedModbusGateway` can
be made slow or cut off. `SimulatedMicroNabtoDevice` answers as a Nilan CTS 402 was measured
to. `FakeClock` lets a test move time forward without waiting.

---

## Disclaimer

Provided "as is", without warranty of any kind. You are responsible for the safe operation of
your devices.

## License

MIT. See [LICENSE](LICENSE).
