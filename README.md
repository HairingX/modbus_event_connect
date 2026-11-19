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
Model    what the device is and how it is talked to: ModbusModel or        (Part 2)
         MicroNabtoModel - its points, how to decode them, its timing
Device   where it is: ModbusDevice or MicroNabtoDevice, given the models      (Part 1)
Client   joins them: connect, subscribe, run, write                           (Part 1)
```

A model is locked to one protocol and says everything about how its unit behaves on it, read top
to bottom in one file. A device library keeps each model in a file of its own and lists the
models once; its `create_client` writes out what is needed before the model is known. An
application only says where the device is.

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
from my_devices import TARGET, TEMPERATURE, create_client   # see Part 2

def on_change(key, old, new):
    print(f"{key}: {new.value} ({new.quality.name})")

async def main():
    client = create_client(host="<device-ip>", unit_id=1)
    await client.connect()                  # reads every point once
    client.subscribe(TEMPERATURE, on_change)
    await client.write(TARGET, 21.5)
    polling = asyncio.create_task(client.run())   # reads whatever is due, until cancelled
    try:
        await asyncio.sleep(3600)
    finally:
        polling.cancel()
        await client.disconnect()

asyncio.run(main())
```

**`run()` reads whatever is due, whenever it is due**, until it is cancelled - it never ends by
itself - and is woken when a subscription, a refresh or a write makes something due sooner. While
the client is not connected - before `connect()`, or after `disconnect()` - it waits until it is.
A poll that raises is logged, the first of a run of failures as an error with its traceback, and
polling goes on after a pause. It waits on the client's clock, as every pause the client makes
does: a test that gives the client a `FakeClock` moves it on with `clock.advance()`.

A program with its own loop calls `poll()` instead, which reads the points that are due. A call
when nothing is due returns at once without contacting the device; `seconds_until_next_poll()`
says how long until something is.

## Connecting

A device library's `create_client` does this for you. Building one by hand, every value is
written out; none has a default but a protocol's own facts, such as its port:

```python
from modbus_event_connect import CircuitBreaker, Exponential, Fixed, Retry
from modbus_event_connect.modbus import ModbusDevice, ModbusTcpConnection
from modbus_event_connect.micro_nabto import MicroNabtoDevice
import my_blinds, my_heating, my_devices, my_ventilation   # each lists its models

# Modbus TCP, one device; once 3 requests in a row went unanswered, asked again 2, 4 and 8 s
# later, then every 10 s. A request waits as long as the model says its unit answers within,
# and timeout_margin more, for the network.
device = ModbusDevice.tcp("<device-ip>", unit_id=1, models=my_devices.MODELS, timeout_margin=2.5,
                          connect_timeout=3.0, frame_gap=0.0,
                          reconnect=CircuitBreaker(after=3, retry=Retry(Exponential(2.0, factor=2.0, max=10.0), None)))

# Several Modbus devices behind one gateway, sharing its connection, each with its own breaker
gateway = ModbusTcpConnection("<gateway-ip>", connect_timeout=3.0, frame_gap=0.0)
slowly = CircuitBreaker(after=3, retry=Retry(Exponential(10.0, factor=2.0, max=120.0), None))
heating = ModbusDevice(gateway, 1, reconnect=slowly, models=my_heating.MODELS, timeout_margin=2.5)
blinds = ModbusDevice(gateway, 3, reconnect=slowly, models=my_blinds.MODELS, timeout_margin=2.5)

# micro_nabto; with device_id, the device is found again if its address changes
ventilation = MicroNabtoDevice.udp("<email-paired-with-the-device>", host="<device-ip>",
                                   device_id="<device-id>", models=my_ventilation.MODELS,
                                   timeout=1.0, retries=2, session_idle=12.0, rediscovery_timeout=3.0,
                                   rediscovery_resend=0.5, answer_wait=5.0,
                                   reconnect=CircuitBreaker(after=1, retry=Retry(Fixed(5.0), None)))
```

Behind a gateway, a device that stops answering does not slow down the others, and each request
waits as long as its own unit needs.

A device is given every model it may turn out to be, of its own protocol only: a type checker
refuses a `MicroNabtoModel` given to a `ModbusDevice`. Once the client has chosen the model, it
tells the device with `use(model)`, and the device talks to it as that model says. Until then it
asks as cautiously as any of its models needs: the fewest registers in one read, the longest
wait for an answer. A model the device was not given raises `ModelError`, and so does one given
twice.

A micro_nabto device says what it is when you connect. Give the client a function instead of a
model, and it picks one of the device's models from that:

```python
def pick_model(identity):
    return VENTILATION if identity["device_model"] == 1140 else None

client = Client(ventilation, pick_model, offline_after_intervals=1)
```

`offline_after_intervals` is how many reads of the chosen model's fastest poll rate the device
may leave unanswered before it counts as not answering; see
[Connection state](#connection-state).

micro_nabto devices on the local network are found with `discover`, which writes out how long
it searches and how often it asks again: `await discover(timeout=2.0, resend=0.5,
target=BROADCAST)`. Each answer is a `DiscoveredDevice` with its `device_id`, `host` and `port`.

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

`client.value(key)` gives the current value at any time, or `None` while the unit has none.
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

Every point is read once when connecting. After that, a point is read on its poll rate -
`FAST`, `MEDIUM`, `SLOW`, `RARE`, or `STATIC`, once when connecting - while someone subscribes
to it. How many seconds each rate is, the model says, for its own unit; nothing outside the
model changes it.

To read now, call `await client.refresh([TEMPERATURE])`. `refresh(PollRate.SLOW)` reads a
whole poll rate, and `refresh()` reads everything. `subscribe(key, callback, poll=False)` is
told about changes without asking for the point to be read on a timer. A key no model of the
client has raises `KeyError`, before `connect()` too.

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
| `Retry(Exponential(2.0, factor=2.0, max=10.0), None)` | after 2, 4 and 8 s, then every 10 s |
| `Retry(Fixed(60.0), None)` | every minute |
| `Retry(Exponential(2.0, factor=2.0, max=10.0), Attempts(8))` | as the first, and gives up at the eighth opening |
| `Retry(Steps(2.0, 5.0, then=Fixed(60.0)), Within(3600.0))` | after 2 and 5 s, then every minute, for an hour |
| `NO_RETRY` | only on `connect()` |

What the model states is about its unit itself, and has no default either:

| Model field | What it does |
|---|---|
| `ModbusModel(busy=Retry(Exponential(0.2, factor=2.0, max=None), Attempts(5)))` | how a read answered "busy" (0x06) is tried again: here 4 times more, after 0.2 s, doubling |
| `ModbusModel(answers_within=0.5)` | seconds the unit takes at most to answer; a request waits this and the device's `timeout_margin` |
| `write_retry=Retry(Fixed(0.5), Within(15.0))` | how a write the unit answered busy is sent again: here every half second for 15 s; `NO_RETRY` sends it once |
| `MicroNabtoModel(zeros=RereadZeros(within=7.5, wait=Steps(0.5, then=Fixed(1.0)), at_once=True, halves=True))` | how an answer of only zeros, where values were, is read again before the zeros are taken: here at once, after 0.5 s, then every second, and in two halves at 7.5 s |
| `MicroNabtoModel(zeros=TAKE_ZEROS)` | zeros are taken as they come |
| `MicroNabtoModel(answer_wait=2.0)` | how long an answer is waited for once the device has said it has the request; never shorter than the connection's timeout |
| `MicroNabtoModel(write_taken=frozenset({0}))` | the statuses the unit answers a write it took with; a write answered with another is `BUSY`, and sent again as `write_retry` says. A CTS 402 answered 0 to the writes it took, and a Genvex Optima 270 answered 0x01 to every write it took |

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
reconnect = CircuitBreaker(after=3, retry=WhenOnline(Exponential(2.0, factor=2.0, max=10.0), None,
                                                    online=lambda: network["up"]))
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
- A write the device answers busy is sent again as the model's `write_retry` says, a `Retry`
  that counts the sendings and the time since the first: `Retry(Fixed(0.5), Within(15.0))`
  sends it every half second for 15 s, and `NO_RETRY` sends it once. A newer setting takes over
  from one being sent again.
- A point is not read while it is being written. Once the device takes a setting, subscribers
  see the value written; then the point is read back, so they see what the device really did.
- `await client.write_sequence([Write(MODE, Mode.HEAT), Write(TARGET, 21.5)])` checks every
  value first, then writes them in order and stops at the first refusal.
- `client.status(Status.WRITE_PENDING).value` is `True` while writes are waiting or being sent.

`Client(device, model, offline_after_intervals=3, read_only=True)` refuses every write with
`ReadOnlyError` before it reaches the device. Use it while developing against a real
installation.

## Connection state

After `connect()`, the client follows whether the device answers. You do not need to connect
again after an outage.

- `client.status(Status.CONNECTED).value` is `False` once the device has stayed silent for
  `offline_after_intervals` of its model's fastest interval - `Client(...,
  offline_after_intervals=3)` with a fastest rate of 10 s gives 30 s - so one lost answer does
  not make it unavailable. Meanwhile its values are `STALE`, and `client.silent` and
  `client.silent_for` say whether the latest request went unanswered, and for how long the
  device has been silent.
- A read that fails waits for its next regular read; the protocol rides out a busy or silent
  device itself, as its model and its breaker say (see "Riding out failures"), and nothing is
  added on top of that.
- `client.subscribe_status(Status.CONNECTED, callback)` tells you when that changes. A status is
  not a point, so a model's keys never collide with it.
- When the device answers after a silence, whatever failed meanwhile is read at once; once it
  counted as not answering, every polled point is.
- `await client.disconnect()` lets go of the device: `Status.CONNECTED` turns `False`, and
  `poll()`, `refresh()` and `write()` raise `NotConnectedError` until `connect()` again. Nothing
  reopens the link by itself.

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

- Run `client.run()` as a task on the application's event loop. When the device is removed,
  cancel the task, then `await client.disconnect()`.
- The client is a `DeviceClient`, the surface every client of event_connect's devices shares, and
  its `model` a `ModelInfo`: an application written against those uses any of them alike.
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
| `NotConnectedError` | most methods | Used before `connect()` succeeded; `poll()`, `refresh()` and `write()` also after `disconnect()`. |
| `InvalidValueError` | `write()` | The point cannot take the value. |
| `ReadOnlyError` | `write()` | The client is read-only. |
| `ModelError` | creating a model, `connect()` | The model contradicts itself, or the device was not given it. |
| `KeyError` | `subscribe()`, `write()` | No model of the client has the key, or this unit does not. |
| `TypeError` | `value()`, `subscribe()`, `write()` | The key names another type than the model gives the point. |

---

# Part 2: Describing a device

A model is plain data: a list of points, each saying where its value lives and how to decode
it, and everything about how its unit behaves over its protocol - how often each poll rate is
read, how long a write takes to show, how its manual numbers registers, how many it takes in one
read, how long it takes to answer, how a busy answer is tried again. You write it once, from the
device's manual and from what you measure, and every app uses it. Every setting is written out;
none has a default to forget, and nothing outside the model changes it.

Write where each number comes from on its own line, so the next reader knows what may be changed:

- `# measured on <unit>: <result> (<owner/repo>#<n>)`, where a record of the measurement exists,
- `# manual: "<quote>"`,
- `# reported: <what, where>`,
- `# guess`, optionally with how it could be measured.

## One file per model

A device library keeps each model in a file of its own:

```python
"""my_devices/_thermostat.py: the Example Thermostat, as it is talked to over Modbus."""
from enum import IntEnum

from modbus_event_connect import (NO_RETRY, Attempts, DataType, Exponential, Key, Limits, Point,
                                  PollRate, Retry, Section, Unit)
from modbus_event_connect.modbus import (HoldingRegister, InputRegister, ModbusModel, SingleWrite,
                                         plain)


class Mode(IntEnum):                      # the states the manual documents
    OFF = 0
    HEAT = 1
    COOL = 2


TEMPERATURE = Key("temperature", float)
TARGET = Key("target", float)
MODE = Key("mode", Mode)
SERIAL_NUMBER = Key("serial_number", int)

THERMOSTAT = ModbusModel(
    "Thermostat", "Example",
    [Section([
        Point(TEMPERATURE,
              read=InputRegister(10),     # where the value is
              data_type=DataType.INT16,   # a signed 16-bit number
              scale=0.1,                  # the device sends 215 for 21.5
              unit=Unit.CELSIUS,
              valid_raw=range(-0x8000, 0x7FFF),   # all but 0x7FFF, what it sends with no reading
              poll_rate=PollRate.MEDIUM),
        Point(TARGET,
              read=HoldingRegister(20), write=HoldingRegister(20),
              data_type=DataType.INT16, scale=0.1, unit=Unit.CELSIUS,
              limits=Limits(min=5, max=30, step=0.5),   # checked before anything is written
              poll_rate=PollRate.SLOW),
        Point(MODE,
              read=HoldingRegister(21), write=HoldingRegister(21),   # 0, 1 or 2; nothing else
              poll_rate=PollRate.SLOW),
        Point(SERIAL_NUMBER,
              read=InputRegister(1), data_type=DataType.UINT32,
              poll_rate=PollRate.STATIC),   # read once, when connecting
    ])],
    poll_intervals={
        PollRate.MEDIUM: 30.0,            # guess
        PollRate.SLOW: 60.0,              # guess
    },
    read_back_after=2.0,                  # measured on the Example Thermostat: shown 2 s after (example/thermostat#1)
    write_retry=NO_RETRY,                 # manual: "a write is never answered busy"
    scan_check=None,                      # it has nothing to scan
    identity_points=(),
    scan_steps=(),
    numbering=plain(first_address=1),     # manual: "registers are numbered by their address"
    max_registers=125,                    # manual: "reads of up to 125 registers"
    max_bits=2000,                        # manual: "reads of up to 2000 coils"
    single_write=SingleWrite.FC06,        # manual: "function codes 3, 4, 6 and 16"
    bit_write=None,                       # it writes no single bit of a register
    busy=Retry(Exponential(0.2, factor=2.0, max=None), Attempts(5)),   # guess
    answers_within=0.5,                   # manual: "replies within 500 ms"
)
```

A model checks itself when it is made, and raises `ModelError` naming every problem: a poll rate
its readable points use with no interval, an interval of a rate nothing is read at, a
`scan_check` missing where it scans or given where it does not, a `bit_write` missing where a
point writes one bit of a holding register or given where none does, and a count or a time its
protocol cannot have.

The package lists every model once, and creates a client with only what depends on the
installation, writing out what is needed before the model is known:

```python
"""my_devices/__init__.py"""
from modbus_event_connect import Client, CircuitBreaker, Clock, Exponential, Retry
from modbus_event_connect.modbus import PORT, ModbusDevice

from ._thermostat import MODE, SERIAL_NUMBER, TARGET, TEMPERATURE, THERMOSTAT, Mode

MODELS = [THERMOSTAT]


def create_client(*, host: str, unit_id: int, port: int = PORT, read_only: bool = False,
                  clock: Clock | None = None) -> Client:
    """A client for the thermostat at `host`; call `connect()` on it."""
    device = ModbusDevice.tcp(
        host, port=port, unit_id=unit_id, models=MODELS,
        reconnect=CircuitBreaker(after=3, retry=Retry(Exponential(2.0, factor=2.0, max=10.0), None)),   # guess
        timeout_margin=2.5,               # guess: the network's share of a request's wait
        connect_timeout=3.0,              # guess
        frame_gap=0.0,                    # manual: "no pause is needed between requests"
        clock=clock)
    return Client(device, THERMOSTAT,
                  offline_after_intervals=3,   # guess: three missed reads of MEDIUM, 90 s
                  read_only=read_only, clock=clock)
```

A second model is a second file, added to `MODELS`. A model reached over another protocol is a
model of that protocol, in a list of its own: a device takes the models of its own protocol only.

A key's type is what the point's value is: `bool` for `BOOL` or a bit, `str` for text, `int` for
an integer that stays whole after scaling, `float` for any number, `date` and `datetime` for a
packed date, and an `IntEnum` for a number that names a state. Publish the keys with the model; they are how an application names the
points, with their types.

A mistake in a point, such as limits on a point that cannot be written, or a key type its
registers cannot hold, raises `ValueError` when the point is created, naming every problem at
once; so does a model setting its protocol cannot meet. A mistake between points for one unit,
such as two overlapping, or one the protocol cannot carry, raises `ModelError` from `connect()`;
test for it first, see [Testing a model](#testing-a-model).

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

**Every model states** every setting of its protocol, see [One file per model](#one-file-per-model);
it cannot be created without them.

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
from modbus_event_connect.modbus import InputRegister, ModbusModel, NumberRange, RegisterNumbering

NUMBERING = RegisterNumbering(
    input_registers=[NumberRange(first=30001, last=39999, address=0)],
    holding_registers=[NumberRange(first=40001, last=49999, address=0)],
)
ModbusModel(..., numbering=NUMBERING, ...)

Point(Key("temperature", float), read=InputRegister(30011), data_type=DataType.INT16, scale=0.1,
      poll_rate=PollRate.MEDIUM)   # sent as address 10
```

A number in none of its table's ranges is a mistake in the model. It is reported before
anything is read, never guessed at.

The model also says how many registers the device takes in one read. The Modbus
specification allows 125 registers or 2000 bits; `max_registers` and `max_bits` say how many of
them the device takes. Neighbouring points are read together, up to that limit.

**micro_nabto** points name an object and an address, `DatapointRegister(27, obj=0)`, as the
device's documentation gives them. A `MicroNabtoModel` states how many registers one read asks
for, at most as many as one UDP datagram can ask for, how long an acknowledged request is waited
for, how long a later answer to a read sent again is waited for, how an answer of only zeros
is read again, and which statuses answer a write it took:

```python
from modbus_event_connect import Fixed, PollRate, Retry, Steps, Within
from modbus_event_connect.micro_nabto import MicroNabtoModel, RereadZeros

VENTILATION = MicroNabtoModel(
    "Ventilation", "Example", VENTILATION_SECTIONS,
    poll_intervals={PollRate.FAST: 10.0, PollRate.RARE: 900.0},   # guess
    read_back_after=1.0,                  # guess
    write_retry=Retry(Fixed(0.5), Within(15.0)),   # reported: writes answered busy, taken when sent again
    scan_check=None,                      # it has nothing to scan
    identity_points=(),
    scan_steps=(),
    max_registers=64,                     # guess: one read of 108 was answered; this leaves room
    answer_wait=2.0,                      # guess
    later_answer_wait=5.0,                # guess
    zeros=RereadZeros(within=7.5, wait=Steps(0.5, then=Fixed(1.0)), at_once=True, halves=True),   # guess
    write_taken=frozenset({0}),           # guess
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
| `poll_rate` | `FAST`, `MEDIUM`, `SLOW`, `RARE` or `STATIC`; every point that can be read has one, and no other |
| `poll_always=True` | read even when no one subscribes, for a value the model itself relies on |
| `deadband=0.2` | changes smaller than this are not reported |
| `poll_intervals={PollRate.FAST: 10.0, ...}`, on the model | its own seconds per poll rate: for exactly the rates its readable points and its scan check use; `STATIC` has none |
| `scan_check=PollRate.RARE`, on the model | the rate at which what its scans read is read again; see [When the unit changes](#when-the-unit-changes) |

How long a device may stay silent before it counts as not answering is the client's
`offline_after_intervals`, counted in the model's fastest interval.

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
it for yours. So every model states its `read_back_after`, in seconds as measured.

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

measured = await measure_read_back(client, TARGET, (20.0, 21.0), delays=(1, 2, 3, 4, 5), repeats=3)
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
ModbusModel("Heat pump", "Example",
            [Section(common_points),
             Section(cooling_points, when=lambda identity: identity["firmware"] >= 20)],
            identity_points=[Point(Key("firmware", int), read=InputRegister(0), poll_rate=PollRate.STATIC)],
            ...)
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
                  data_type=DataType.INT16, scale=0.1, unit=Unit.CELSIUS, poll_rate=PollRate.MEDIUM)]

async def scan_room(scan: Scan, n: int):
    installed = await scan.read([f"room_{n}_installed"])
    if installed[f"room_{n}_installed"].value == 0:
        scan.set_available(Labels(room=n), False, reason="no room")

HEATING = ModbusModel("Heating", "Example", [RepeatedSection(room, range(1, 9), label="room", scan=scan_room)],
                      poll_intervals={PollRate.MEDIUM: 30.0, PollRate.RARE: 900.0},   # guess
                      scan_check=PollRate.RARE,   # guess: rooms are seldom set up
                      ...)
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

- What each scan read is read again at the model's `scan_check` rate - one of its own poll
  rates, whose interval it states - the scans spread evenly over it. A scan reads only what
  decides what the unit has, so that is all that is read. A model that scans states a
  `scan_check`; one that does not, states `None`. `PollRate.STATIC` checks only when asked.
- Where that has changed, only that scan runs again: one room's. A change to what the whole
  unit's `scan_steps` read scans everything again, choosing the model afresh.
- A register a read finds missing is no longer read, and its room is checked at once.
- An unanswered read never changes anything; the check is tried again later.
- `client.subscribe_points(callback)` is told the keys gained and lost. A lost key's
  subscribers are told `MISSING`.
- `await client.refresh(rate)`, given the model's `scan_check` rate, checks now, as well as
  reading that rate's points. To find out everything afresh, `disconnect()` and `connect()`.

## Testing a model

Test a model without the device. `assert_models_valid` resolves each model against every
identity you expect to meet, and fails listing every problem: a model given twice, overlapping
registers, a register the protocol cannot carry, a refresh naming a key that does not exist, a
transform that does not convert back, a section no identity includes.

```python
from modbus_event_connect import NO_RETRY, CircuitBreaker, Client
from modbus_event_connect.modbus import ModbusDevice
from modbus_event_connect.testing import (SimulatedModbusDevice, SimulatedModbusGateway,
                                          assert_models_valid)
from my_devices import MODELS, TEMPERATURE, THERMOSTAT

def test_models_are_valid():
    assert_models_valid(MODELS, identities=[{}])

async def test_temperature_is_read():
    unit = SimulatedModbusDevice(input_registers={1: 0, 2: 1234, 10: 215},
                                 holding_registers={20: 210, 21: 1})
    device = ModbusDevice(SimulatedModbusGateway({1: unit}), 1, models=MODELS, timeout_margin=0.0,
                          reconnect=CircuitBreaker(after=3, retry=NO_RETRY))
    client = Client(device, THERMOSTAT, offline_after_intervals=3)
    await client.connect()
    assert client.value(TEMPERATURE).value == 21.5
```

`SimulatedModbusDevice` refuses an unknown address with exception 0x02, Illegal Data Address,
as the Modbus specification says, and can be made busy or silent; `SimulatedModbusGateway` can
be made slow or cut off. `SimulatedMicroNabtoDevice` answers as a Nilan CTS 402 was measured
to, and can be made to answer late (`answer_delay`), to answer writes with other statuses
(`write_statuses`), and to take writes answered with statuses other than 0 (`write_taken`).
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
