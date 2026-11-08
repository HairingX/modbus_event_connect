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
| Working on the library itself | [docs/design.md](https://github.com/HairingX/modbus_event_connect/blob/main/docs/design.md) |

```bash
pip install modbus-event-connect
```

## How the pieces fit

```text
Model    what the device is: its points, how to decode them, its timing        (Part 2)
Profile  how that model is talked to over one protocol                          (Part 2)
Device   where it is: ModbusDevice or MicroNabtoDevice, given the profiles      (Part 1)
Client   joins them: connect, subscribe, poll, write                            (Part 1)
```

A device library keeps each model in a file of its own, its profile next to it, and lists the
profiles once. An application only says where the device is, and how often to ask it again
after it stopped answering.

A *point* is one value the device has, such as a temperature or a setting. Its *key*,
`Key("temperature", float)`, names it everywhere and says what its value is. A key is its text
wherever it is stored, and a type checker knows from it what reading the point gives and what
writing it takes.

Keys, values, points by their behaviour, the scheduling of reads and what a write or a change
sets off are those of [event_connect](https://github.com/HairingX/event_connect), the
protocol-free core this library is built on; they are imported from here under the same names.

Everything is imported from four places. The modules inside them are internal, and may change in
any release.

| Import from | For |
|---|---|
| `modbus_event_connect` | the client, values, errors, and what a model is made of |
| `modbus_event_connect.modbus` | Modbus: the connection, the device, register kinds and numbering |
| `modbus_event_connect.micro_nabto` | micro_nabto: discovery, the session, the device, register kinds |
| `modbus_event_connect.testing` | testing a model: simulated devices, a fake clock, the model walker |

---

# Part 1: Using a device

## A complete program

```python
import asyncio
from modbus_event_connect import NO_RETRY, CircuitBreaker, Client, Exponential, Retry
from modbus_event_connect.modbus import ModbusDevice
from my_devices import PROFILES, TARGET, TEMPERATURE, THERMOSTAT   # see Part 2

def on_change(key, old, new):
    print(f"{key}: {new.value} ({new.quality.name})")

async def main():
    reconnect = CircuitBreaker(after=3, retry=Retry(Exponential(2.0, max=10.0)))
    device = ModbusDevice.tcp("<device-ip>", 502, 1, reconnect=reconnect, profiles=PROFILES)
    client = Client(device, THERMOSTAT, write_retry=NO_RETRY)
    await client.connect()                  # reads every point once
    client.subscribe(TEMPERATURE, on_change)
    await client.write(TARGET, 21.5)
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
from modbus_event_connect import CircuitBreaker, Exponential, Fixed, Retry
from modbus_event_connect.modbus import ModbusDevice, ModbusTcpConnection
from modbus_event_connect.micro_nabto import MicroNabtoDevice
import my_blinds, my_heating, my_devices, my_ventilation   # each lists the profiles of its models

# Modbus TCP, one device; once 3 requests in a row went unanswered, asked again 2, 4 and 8 s
# later, then every 10 s
device = ModbusDevice.tcp("<device-ip>", 502, 1, profiles=my_devices.PROFILES,
                          reconnect=CircuitBreaker(after=3, retry=Retry(Exponential(2.0, max=10.0))))

# Several Modbus devices behind one gateway, sharing its connection, each with its own breaker
gateway = ModbusTcpConnection("<gateway-ip>")
slowly = CircuitBreaker(after=3, retry=Retry(Exponential(10.0, max=120.0)))
heating = ModbusDevice(gateway, 1, reconnect=slowly, profiles=my_heating.PROFILES)
blinds = ModbusDevice(gateway, 3, reconnect=slowly, profiles=my_blinds.PROFILES)

# micro_nabto; with device_id, the device is found again if its address changes
ventilation = MicroNabtoDevice.udp("<email-paired-with-the-device>", host="<device-ip>",
                                   device_id="<device-id>", profiles=my_ventilation.PROFILES,
                                   reconnect=CircuitBreaker(after=1, retry=Retry(Fixed(5.0))))
```

Behind a gateway, a device that stops answering does not slow down the others.

A device is given the profile of every model it may turn out to be. Once the client has chosen
the model, it tells the device with `use(model)`, and the device talks to it as that model's
profile says. Until then it asks as little as any profile allows: the fewest registers in one
read, the longest wait for a later answer. A model the device has no profile of raises
`ModelError`, and so do two profiles of one model.

A micro_nabto device says what it is when you connect. Give the client a function instead of a
model, and it picks the model from that:

```python
def pick_model(identity):
    return VENTILATION if identity["device_model"] == 1140 else None

client = Client(ventilation, pick_model)
```

Once connected, `client.model` is the model chosen and `client.identity` what the device reported
about itself, with what the model's identity points read.

`connect()` raises when it cannot finish:

| Error | Meaning |
|---|---|
| `CannotConnectError` | The device could not be reached, or did not answer every read. Try again later. |
| `AuthenticationError` | A micro_nabto device refused the email. Ask the user to check it. |
| `UnsupportedDeviceError` | The device answered, but no model matches it. Its `identity` is what the device reported. |

## Reading values

`client.subscribe(key, callback)` calls `callback(key, old, new)` at once with the current
value, then on every change. `old` is `None` the first time. It returns a function that
unsubscribes.

`client.value(key)` gives the current value at any time, or `None` before the first read.
Its type follows the key: `client.value(TEMPERATURE)` is a `DataValue[float]`, and a key of
another type than the model gives the point raises `TypeError`.

Every value is a `DataValue` with `.value`, `.quality`, `.timestamp`, and `.raw`: what the
device answered, before it was decoded.

| Quality | Meaning | Show it as |
|---|---|---|
| `GOOD` | A real value. | the value |
| `NO_DATA` | The device says it has no reading, such as a sensor not fitted, or sent a number no state names. | unknown |
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
| `SCAN` | what each scan read, read again to find changes (see [When the unit changes](#when-the-unit-changes)) |

These are the library's defaults; a model may set its own. You can change them:

```python
client.set_poll_interval(PollRate.FAST, 5)       # every FAST point
client.set_poll_interval(TEMPERATURE, 2)         # one point
client.set_poll_interval(TEMPERATURE, None)      # back to the model's interval
```

A model can set a floor, the shortest interval its device copes with. `set_poll_interval`
returns the interval actually used.

To read now, call `await client.refresh([TEMPERATURE])`. `refresh(PollRate.SLOW)` reads a
whole poll rate, and `refresh()` reads everything. `subscribe(key, callback, poll=False)` is
told about changes without asking for the point to be read on a timer.

`client.set_scheduled_polling(False)` stops reading on the schedule - every poll rate, and the
checks for whether the unit has changed. `poll()` then reads only what `refresh()` and writes ask
for, and a read that finds a point gone still has its part of the unit checked.
`set_scheduled_polling(True)` reads at once what has become due meanwhile.

## Riding out failures

How a device is asked again is assembled from standard parts that every protocol shares: waits
(`Fixed`, `Exponential`, `Steps`, `Jitter`), stops (`Attempts`, `Within`, combined with `|`),
`Retry`, which puts a wait and a stop together, and `CircuitBreaker`, which stops asking a device
that has left requests unanswered.

**Reconnecting** is stated by whoever creates the device, as `reconnect`, a `CircuitBreaker`, and
nowhere else: how soon a silent device should be asked again depends on how it is reached, not on
what it is. It has no default. The breaker opens once `after` requests in a row went unanswered -
for micro_nabto, handshakes - and the device is not asked until its `retry` lets a probe through;
asking again reopens a closed link, and an answer closes the breaker. `connect()` closes it too.
Before each probe, `await retry.ready()` is asked: a subclass of `Retry` can check something
first, and a probe it refuses keeps the breaker open as long again.

| `reconnect=CircuitBreaker(after=3, retry=...)` | Once 3 requests in a row went unanswered, asks again |
|---|---|
| `Retry(Exponential(2.0, max=10.0))` | after 2, 4 and 8 s, then every 10 s |
| `Retry(Fixed(60.0))` | every minute |
| `Retry(Exponential(2.0, max=10.0), Attempts(8))` | as the first, and gives up at the eighth opening |
| `Retry(Steps(2.0, 5.0, then=Fixed(60.0)), Within(3600.0))` | after 2 and 5 s, then every minute, for an hour |
| `NO_RETRY` | only on `connect()` |

What the profile states is about the model's device itself, and has no default either:

| Profile field | What it does |
|---|---|
| `ModbusProfile(busy=Retry(Exponential(0.2), Attempts(5)))` | how a read answered "busy" (0x06) is tried again: here 4 times more, after 0.2 s, doubling |
| `MicroNabtoProfile(zeros=RereadZeros(within=7.5, wait=Steps(0.5, then=Fixed(1.0)), at_once=True, halves=True))` | how an answer of only zeros, where values were, is read again before the zeros are taken: here at once, after 0.5 s, then every second, and in two halves at 7.5 s |
| `MicroNabtoProfile(zeros=TAKE_ZEROS)` | zeros are taken as they come |

A retry that checks something before each probe is a subclass with its own `ready`. It is
frozen, like every `Retry`, so it asks something outside itself:

```python
from collections.abc import Callable
from dataclasses import dataclass

from modbus_event_connect import CircuitBreaker, Exponential, Retry


@dataclass(frozen=True, kw_only=True)
class WhenOnline(Retry):
    """Asks again only while the application says the network is up."""
    online: Callable[[], bool]

    async def ready(self) -> bool:
        return self.online()


network = {"up": True}
reconnect = CircuitBreaker(after=3, retry=WhenOnline(Exponential(2.0, max=10.0), online=lambda: network["up"]))
```

## Writing

```python
accepted = await client.write(TARGET, 21.5)
```

`write` returns whether the device accepted the value. A value of another type than the key's
is a type error before the program runs. Before anything is sent, the value is also checked
against the point's limits, or its states, and `InvalidValueError` says why it was refused.

- Writes are sent one at a time, in order.
- If a setting is written several times while earlier writes are still waiting, only the newest
  value is sent. Commands are always all sent.
- A write the device answers busy is sent again as `Client(..., write_retry=...)` says, a
  `Retry` that counts the sendings and the time since the first: `Retry(Fixed(0.5), Within(15.0))`
  sends it every half second for 15 s, and `NO_RETRY` sends it once. There is no default. A newer
  setting takes over from one being sent again.
- A point is not read while it is being written. Once the device takes a setting, subscribers
  see the value written; then the point is read back, so they see what the device really did.
- `await client.write_sequence([Write(MODE, Mode.HEAT), Write(TARGET, 21.5)])` checks every
  value first, then writes them in order and stops at the first refusal.
- `client.status(Status.WRITE_PENDING).value` is `True` while writes are waiting or being sent.

`Client(device, model, write_retry=NO_RETRY, read_only=True)` refuses every write with `ReadOnlyError` before it
reaches the device. Use it while developing against a real installation.

## Connection state

After `connect()`, the client follows whether the device answers. You do not need to connect
again after an outage.

- `client.status(Status.CONNECTED).value` is `False` once the device has not answered for the
  `offline_after` of its model's timing, so one lost answer does not make it unavailable. Meanwhile its values are `STALE`, and `client.silent` and `client.silent_for` say
  whether the latest request went unanswered, and for how long the device has been silent.
- A read that fails waits for its next regular read; the protocol rides out a busy or silent
  device itself, as its model's profile and its breaker say (see "Riding out failures"), and
  nothing is added on top of that.
- `client.subscribe_status(Status.CONNECTED, callback)` tells you when that changes. A status is
  not a point, so a model's keys never collide with it.
- When the device answers after a silence, whatever failed meanwhile is read at once; once it
  counted as not answering, every polled point is.
- `await client.disconnect()` lets go of the device.

## What this unit has

A model can describe several variants, and parts that may or may not be installed, such as
rooms. After `connect()`:

| | |
|---|---|
| `client.points` | the points this unit has, by their keys, each with its type |
| `client.has(key)`, `client.can_write(key)` | whether it has the key, and whether it can be written |
| `client.instances("room")` | which rooms, zones or channels are installed, such as `(1, 3)` |
| `client.unavailable_reasons` | keys the unit does not have, with the reason |
| `client.subscribe_points(callback)` | told when the unit gains or loses points, such as a room set up later |

## In a larger application

- Run the poll loop as a task on the application's event loop. When the device is removed,
  cancel the task, then `await client.disconnect()`.
- Subscription callbacks run on that event loop, from within the client's own calls, never on
  another thread, so they may update the application's state directly.
- Turn the errors `connect()` raises into the application's own: try again later after
  `CannotConnectError`, ask the user for new credentials after `AuthenticationError`.
- How a value's quality is shown is the application's choice. `client.consecutive_failures(key)`,
  the schedule's count, lets it wait a few failed reads before calling a `STALE` value
  unavailable.

## Errors

| Error | Raised by | When |
|---|---|---|
| `CannotConnectError` | `connect()` | The device could not be reached, or left a read unanswered. |
| `AuthenticationError` | `connect()` | A micro_nabto device refused the email. |
| `UnsupportedDeviceError` | `connect()`, `poll()` | No model matches the device, or no longer does after it changed. |
| `NotConnectedError` | most methods | Used before `connect()` succeeded. |
| `InvalidValueError` | `write()` | The point cannot take the value. |
| `ReadOnlyError` | `write()` | The client is read-only. |
| `ModelError` | `connect()` | The model chosen contradicts itself, or the device has no profile of it. |
| `KeyError` | `subscribe()`, `write()` | This unit has no such key. |
| `TypeError` | `value()`, `subscribe()`, `write()` | The key names another type than the model gives the point. |

---

# Part 2: Describing a device

A model is plain data: a list of points, each saying where its value lives and how to decode
it, and the device's timing. You write it once, from the device's manual, and every app uses it.

Next to the model, its *profile* says how that model is talked to over its protocol: how its
manual numbers registers, how many it takes in one read, how it is written and how a busy answer
is tried again. Every setting of both is written out; none has a default to forget.

## One file per model

A device library keeps each model in a file of its own, with its profile beside it:

```python
"""my_devices/_thermostat.py: the Example Thermostat, and how it is talked to over Modbus."""
from enum import IntEnum

from modbus_event_connect import (Attempts, DataType, Exponential, Key, Limits, Model, Point,
                                  PollRate, Retry, Section, Timing, Unit)
from modbus_event_connect.modbus import (BitWrite, HoldingRegister, InputRegister, ModbusProfile,
                                         SingleWrite, plain)


class Mode(IntEnum):                      # the states the manual documents
    OFF = 0
    HEAT = 1
    COOL = 2


TEMPERATURE = Key("temperature", float)
TARGET = Key("target", float)
MODE = Key("mode", Mode)
SERIAL_NUMBER = Key("serial_number", int)

# What the unit is.
THERMOSTAT = Model(
    name="Thermostat", manufacturer="Example",
    timing=Timing(read_back_after=2.0),   # seconds before a write shows in a read, as measured
    sections=[Section([
        Point(TEMPERATURE,
              read=InputRegister(10),     # where the value is
              data_type=DataType.INT16,   # a signed 16-bit number
              scale=0.1,                  # the device sends 215 for 21.5
              unit=Unit.CELSIUS,
              valid_raw=range(-0x8000, 0x7FFF)),   # all but 0x7FFF, what it sends with no reading
        Point(TARGET,
              read=HoldingRegister(20), write=HoldingRegister(20),
              data_type=DataType.INT16, scale=0.1, unit=Unit.CELSIUS,
              limits=Limits(min=5, max=30, step=0.5)),   # checked before anything is written
        Point(MODE,
              read=HoldingRegister(21), write=HoldingRegister(21)),   # 0, 1 or 2; nothing else
        Point(SERIAL_NUMBER,
              read=InputRegister(1), data_type=DataType.UINT32,
              poll_rate=PollRate.STATIC),   # read once, when connecting
    ])],
)

# How it is talked to over Modbus.
THERMOSTAT_MODBUS = ModbusProfile(
    model=THERMOSTAT,
    numbering=plain(first_address=1),     # the manual gives the addresses themselves
    max_registers=125,                    # registers it takes in one read
    max_bits=2000,                        # coils or discrete inputs in one read
    single_write=SingleWrite.FC06,        # one register with Write Single Register
    bit_write=BitWrite.MASK,              # one bit with Mask Write Register
    busy=Retry(Exponential(0.2), Attempts(5)),   # a busy read tried 4 times more, from 0.2 s
)
```

The package lists every profile once, and creates a client with only what depends on the
installation:

```python
"""my_devices/__init__.py"""
from modbus_event_connect import NO_RETRY, CircuitBreaker, Client, Exponential, Retry
from modbus_event_connect.modbus import ModbusDevice

from ._thermostat import MODE, SERIAL_NUMBER, TARGET, TEMPERATURE, THERMOSTAT, THERMOSTAT_MODBUS, Mode

PROFILES = [THERMOSTAT_MODBUS]


def create_client(host: str, *, port: int = 502, unit_id: int = 1) -> Client:
    """A client for the thermostat at `host`; call `connect()` on it."""
    reconnect = CircuitBreaker(after=3, retry=Retry(Exponential(2.0, max=10.0)))
    device = ModbusDevice.tcp(host, port, unit_id, reconnect=reconnect, profiles=PROFILES)
    return Client(device, THERMOSTAT, write_retry=NO_RETRY)
```

A second model is a second file with its own profile, added to `PROFILES`; a model reached over
another protocol too gets a profile of that protocol beside the first, in a list of its own.

A key's type is what the point's value is: `bool` for `BOOL` or a bit, `str` for text, `int` for
an integer that stays whole after scaling, `float` for any number, `date` and `datetime` for a
packed date, and an `IntEnum` for a number that names a state. Publish the keys with the model; they are how an application names the
points, with their types.

A mistake in a point, such as limits on a point that cannot be written, or a key type its
registers cannot hold, raises `ValueError` when the point is created, naming every problem at
once; so does a profile setting its protocol cannot meet. A mistake between points, such as two
overlapping, or one the profile cannot carry, raises `ModelError` from `connect()`; test for it
first, see [Testing a model](#testing-a-model).

**Keys are forever.** Applications store them, for example in the ids of what they build. Choose them
carefully, and never rename one. The names of a state's members are forever too: an
application may show or store them.

## Where a value lives

| Protocol | Read side | Write side |
|---|---|---|
| Modbus | `InputRegister`, `HoldingRegister`, `DiscreteInput`, `Coil` | `HoldingRegister`, `Coil` |
| micro_nabto | `DatapointRegister`, `SetpointRegister` | `SetpointRegister` |

A point has a read side, a write side, or both. They may differ, for a device that reports a
setting in one place and takes it in another.

**Every model states** its `Timing`, and its profile every setting of its protocol, see
[Writing](#writing) and [One file per model](#one-file-per-model). Neither can be created
without them.

**Modbus addresses.** Manuals number registers in different ways, and a wrong guess shifts
every value by one register while still looking plausible. So there is no default: the profile
says once how the model's manual numbers registers: which address its first register is.

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
from modbus_event_connect.modbus import InputRegister, ModbusProfile, NumberRange, RegisterNumbering

NUMBERING = RegisterNumbering(
    input_registers=[NumberRange(first=30001, last=39999, address=0)],
    holding_registers=[NumberRange(first=40001, last=49999, address=0)],
)
ModbusProfile(model=..., numbering=NUMBERING, ...)

Point(Key("temperature", float), read=InputRegister(30011), data_type=DataType.INT16, scale=0.1)   # sent as address 10
```

A number in none of its table's ranges is a mistake in the model. It is reported before
anything is read, never guessed at.

The profile also says how many registers the device takes in one read. The Modbus
specification allows 125 registers or 2000 bits; `max_registers` and `max_bits` say how many of
them the device takes. Neighbouring points are read together, up to that limit.

**micro_nabto** points name an object and an address, `DatapointRegister(27, obj=0)`, as the
device's documentation gives them. A `MicroNabtoProfile` states how many registers one read asks
for, at most as many as one UDP datagram can ask for, how long a later answer to a read sent
again is waited for, and how an answer of only zeros is read again:

```python
from modbus_event_connect import Fixed, Steps
from modbus_event_connect.micro_nabto import MicroNabtoProfile, RereadZeros

VENTILATION_MICRO_NABTO = MicroNabtoProfile(
    model=VENTILATION,
    max_registers=64,                     # a CTS 402 answered 108; this leaves room
    later_answer_wait=5.0,                # seconds a read sent again is kept open for a later answer
    zeros=RereadZeros(within=7.5, wait=Steps(0.5, then=Fixed(1.0)), at_once=True, halves=True),
)
```

## How a value is decoded

| Field | What it does |
|---|---|
| `data_type` | `UINT16` (the default), `INT16`, `UINT32`, `INT32`, `UINT64`, `INT64`, `FLOAT32`, `FLOAT64`, `BCD16`, `BCD32`, `BOOL`, `DOS_DATE`, `DOS_DATETIME` |
| `DataType.bit(3)` | one bit of a register |
| `DataType.string(8)` | text over 8 registers |
| `word_order`, `byte_order` | for values over several registers; high word and big-endian by default |
| `scale`, `offset` | value = raw × scale + offset |
| `precision` | decimals to round to; by default, enough for `scale` and `offset` |
| `transform` | a conversion after scaling, such as `Transforms.SECONDS_AS_MINUTES` |
| `valid_raw` | the raw numbers that are values, of an integer or a `BOOL` register; any other reads as `NO_DATA` and cannot be written |
| `codes` | for a key naming states: which state each raw code means |

`valid_raw` takes a `range` or a set, and a `range` costs nothing however wide:

```python
valid_raw=range(0, 0xFFFFFFFF)     # a UINT32's 0 to max-1; 0xFFFFFFFF means no reading
valid_raw=range(0, 2)              # a switch's 0 and 1; 255, or 11, is no value
valid_raw={*range(0, 230), 255}    # everything but 230 to 254
```

It names the device's own numbers, before `scale` and `offset`, and a number the data type
cannot hold is refused when the point is created: `range(0, 0xFFFF)` on an `INT16` would never
match, as the device's 0xFFFF is -1 to it.

A `FLOAT32` or `FLOAT64` that reads NaN or infinity is `NO_DATA` by itself. A point whose key
is an `IntEnum` reads its number as that state; a number no state names is `NO_DATA`, and
`.raw` still holds it.

Devices that number the same states differently share one `IntEnum`, each through its own
`codes`:

```python
class OperationState(IntEnum):     # every state any of the devices has
    OFF = 0
    HEATING = 1
    COOLING = 2
    DEFROST = 3

Point(STATE, read=InputRegister(20), codes={0: OperationState.OFF, 7: OperationState.HEATING,
                                            8: OperationState.COOLING})
```

A code the table lacks is `NO_DATA`, and a state it lacks cannot be written; `point.states`
names the states a point can have, in the enum's order. On a point that is only read, several
codes may name one state.

`DOS_DATE` is one register packed the MS-DOS way - years since 1980, month, day - and reads as
a `date`. `DOS_DATETIME` is such a date register followed by a time register - hours, minutes,
seconds halved - and reads as a `datetime` in the device's own clock, without a time zone.
Words that name no date or time, such as a date of 0, are `NO_DATA`.

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
| `Timing(poll_intervals=...)` | this device's own seconds per poll rate |
| `Timing(min_poll_interval=5)` | the shortest interval this device copes with; apps cannot go below it |
| `Timing(offline_after=30.0)` | seconds without an answer before the device counts as not answering |

A model's `timing=Timing(...)` holds all of them, with `read_back_after`, and a model cannot be
made without one; what it leaves out is event_connect's default: the poll rates above, no floor,
a second for a write to show, and 30 s of silence.

Pick the rate from how fast the value changes, not from how often it is looked at:
temperatures `MEDIUM`, on/off states `FAST`, settings `SLOW`, firmware versions `STATIC`.

## Writing

| Field | What it does |
|---|---|
| `limits=Limits(min=5, max=30, step=0.5)` | refuses anything else, in the units a user sees |
| `write_kind=WriteKind.STATE` | a setting (the default): writes that queue up collapse to the newest |
| `write_kind=WriteKind.COMMAND` | an action: every write is sent |
| `pulse=Pulse(idle=0, after=1.0)` | a `COMMAND` that is written back to `idle` after 1 s |
| `on_write=Refresh(["mode", "target"])` | also read these after writing, since the write changes them |
| `on_change=Refresh(Labels(room=3))` | read these when this point's value changes |

After a write, the written point is read back, so apps see what the device holds. How long a
device takes before a read shows a write differs from device to device, and only you can know
it for yours. So every model is given its timing, with `Timing(read_back_after=...)` in seconds as
measured; left out, it is event_connect's one second.

The targets of a point's `on_write` are read after the same delay, or after
`Refresh(targets, after=...)` seconds; the written point itself is read back after
`read_back_after` either way. With `until_stable=30`, the targets are read again while they keep
changing, for up to 30 s, for a device that moves slowly to a new value.

`point.write_range()` gives the lowest and highest value a point can be written with, and the
step between them: its limits, and where they leave a bound out, what its registers carry
through `scale`, `offset` and `transform`. A float data type carries any number, so it has a
range only where its limits give both bounds.

**Measure it** on your device, with a point you may change and two values it may take:

```python
from modbus_event_connect.testing import measure_read_back

measured = await measure_read_back(client, TARGET, (20.0, 21.0), delays=(1, 2, 3, 4, 5))
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
      identity_points=[Point(Key("firmware", int), read=InputRegister(0), poll_rate=PollRate.STATIC)],
      sections=[Section(common_points),
                Section(cooling_points, when=lambda identity: identity["firmware"] >= 20)])
```

For devices so different that they deserve their own model, give the client a function that
picks one from the identity, as shown in [Connecting](#connecting).

## Repeated parts, and what is installed

A device with rooms, zones or channels describes one of them once, and a scan finds out what
each one has:

```python
from modbus_event_connect import RepeatedSection, Labels, Scan

def room(n):
    return [Point(Key(f"room_{n}_installed", int), read=InputRegister(100 + 10 * n),
                  poll_rate=PollRate.STATIC),
            Point(Key(f"room_{n}_temperature", float), read=InputRegister(101 + 10 * n),
                  data_type=DataType.INT16, scale=0.1, unit=Unit.CELSIUS)]

async def scan_room(scan: Scan, n: int):
    installed = await scan.read([f"room_{n}_installed"])
    if installed[f"room_{n}_installed"].value == 0:
        scan.set_available(Labels(room=n), False, reason="no room")

HEATING = Model(name="Heating", manufacturer="Example", timing=Timing(read_back_after=2.0),
                sections=[RepeatedSection(room, range(1, 9), label="room", scan=scan_room)])
```

Every point `room(n)` returns is labelled `room=n`, so `Labels(room=3)` selects room 3's points.
After `connect()`, `client.instances("room")` lists the installed rooms, and `client.points`
has only their points.

A repeated section's `scan(scan, n)` decides what instance `n` has, from any points it reads -
several, or one whose value rules out others - and may mark only instance `n`'s points. What
decides for the whole unit, such as a firmware version, goes in the model's `scan_steps`, which
run first. If a scan's read goes unanswered, `connect()` fails rather than guess what is
installed.

### When the unit changes

A room set up later, or one taken away, is found while polling; nobody needs to connect again.

- What each scan read is read again at `PollRate.SCAN`, the scans spread evenly over it. A
  scan reads only what decides what the unit has, so that is all that is read.
- Where that has changed, only that scan runs again: one room's. A change to what the whole
  unit's `scan_steps` read scans everything again, choosing the model afresh.
- A register a read finds missing is no longer read, and its room is checked at once.
- An unanswered read never changes anything; the check is tried again later.
- `client.subscribe_points(callback)` is told the keys gained and lost. A lost key's
  subscribers are told `MISSING`.
- `await client.refresh(PollRate.SCAN)` checks now; `set_poll_interval(PollRate.SCAN, ...)`
  sets how often. To find out everything afresh, `disconnect()` and `connect()`.

## Testing a model

Test a model without the device. `assert_models_valid` resolves each model's profile against
every identity you expect to meet, and fails listing every problem: a model without a profile, a
profile of a model not listed, two profiles of one model, overlapping registers, a register the
profile cannot carry, a refresh naming a key that does not exist, a transform that does not
convert back, a section no identity includes.

```python
from modbus_event_connect import NO_RETRY, CircuitBreaker, Client
from modbus_event_connect.modbus import ModbusDevice
from modbus_event_connect.testing import (SimulatedModbusDevice, SimulatedModbusGateway,
                                          assert_models_valid)
from my_devices import PROFILES, TEMPERATURE, THERMOSTAT

def test_models_are_valid():
    assert_models_valid([THERMOSTAT], PROFILES, identities=[{}])

async def test_temperature_is_read():
    unit = SimulatedModbusDevice(input_registers={1: 0, 2: 1234, 10: 215},
                                 holding_registers={20: 210, 21: 1})
    device = ModbusDevice(SimulatedModbusGateway({1: unit}), 1, profiles=PROFILES,
                          reconnect=CircuitBreaker(after=3, retry=NO_RETRY))
    client = Client(device, THERMOSTAT, write_retry=NO_RETRY)
    await client.connect()
    assert client.value(TEMPERATURE).value == 21.5
```

`SimulatedModbusDevice` refuses an unknown address with exception 0x02, Illegal Data Address,
as the Modbus specification says, and can be made busy or silent; `SimulatedModbusGateway` can
be made slow or cut off. `SimulatedMicroNabtoDevice` answers as a Nilan CTS 402 was measured
to, and can be made to answer late (`answer_delay`) or to refuse writes (`write_statuses`).
`FakeClock` lets a test move time forward without waiting.

---

## Acknowledgements

How micro_nabto works - the local session with a Nilan or Genvex gateway, its handshake and
its commands - was first worked out by superrob. The implementation here is written anew, but
it was his work that made a local connection possible at all.

## Disclaimer

Provided "as is", without warranty of any kind. You are responsible for the safe operation of
your devices.

## License

MIT. See [LICENSE](https://github.com/HairingX/modbus_event_connect/blob/main/LICENSE).
