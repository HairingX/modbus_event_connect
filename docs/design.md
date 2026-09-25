# modbus_event_connect — design

**Status:** implemented.

How the library is built, and why — for whoever works on it. How to use it is in the
[README](../README.md).

The goal: a model **declares** its points — where they live, how they are encoded, their
limits, how fresh they must be, what a write disturbs — and the library does everything else
exactly once: encoding, decoding, validation, scheduling, events, availability and quality.
Wavin Sentio and Nilan become data plus a few scan steps.

---

## 1. Requirements

Each one has a real case behind it.

| # | Requirement | The case |
|---|---|---|
| R1 | Points are declared once: address, type, scaling, limits, unit | every device |
| R2 | A model can grow points by model, version or hardware | Nilan: features only some units have |
| R3 | A model can repeat a section of points without writing it out N times | Sentio: 24 rooms, peripherals |
| R4 | An installation can lack parts of its model, found by the scan | Sentio: unconfigured rooms |
| R5 | Points are read at different rates | temperatures every 10 s, fan data every minute, serial numbers once |
| R6 | The consumer can override the rates | an options screen in the application |
| R7 | A write can disturb other points, which must then be re-read soon | Nilan: ventilation step changes fan %, rpm and more |
| R8 | A cheap point can signal that expensive ones changed | an alarm summary bit in front of 77 alarm inputs |
| R9 | Every value says how trustworthy it is | "offline" and "0" are not the same thing |
| R10 | Several devices share one connection | a Modbus gateway with RS-485 devices behind it |
| R11 | A value can be read-only, write-only, or both, and the consumer can tell which | sensors, commands, setpoints |
| R12 | Commands are never merged, settings may be | "step up" pressed three times vs. a target temperature |
| R13 | Nothing is written unless writes are allowed | development against live installations |
| R14 | Everything above is testable without hardware and without waiting | CI |

---

## 2. Layers

```
  Model        Sentio, Nilan, a lamp …     declares points, poll rates, effects, scan steps
  ─────────────────────────────────────────────────────────────────────────────────────────
  Core         points, data types, polling, events, availability, quality, consumer API
  ─────────────────────────────────────────────────────────────────────────────────────────
  Protocol     Modbus, micro_nabto         address spaces, register numbering, batching,
                                           function codes, mapping device errors to quality
  ─────────────────────────────────────────────────────────────────────────────────────────
  Connection   TCP socket, serial port     the link: one request at a time, timeouts,
                                           reconnect, pause between frames
```

Only the protocol and connection layers know Modbus. Its four tables are a Modbus concept, not a
library concept: micro_nabto has its own address spaces (datapoint and setpoint objects), and
the core must not treat it as a special case.

A **device** is a model plus where to reach it: a connection and, for Modbus, a unit id. A
**client** is one device as the consumer sees it.

### Principles

- **The library never guesses what a device has.** It asks, and records the answer; what the
  answer means is the model's business, said in its scan steps.
- **Nothing derived is ever assigned.** Whether a point is polled is computed from its reasons
  — a subscriber, `set_polling()`, `poll_always`, being a trigger — and its availability.
  Nobody writes the result, so nobody can overwrite anybody.
- **One failing register never costs another.** A request refused because of one address is
  split, so only that address is reported.
- **When `connect()` returns, the picture is final.** Nothing appears or disappears later
  without an explicit call.
- **The library owns no schedule.** The host calls `poll()` and `rescan()` when it chooses. The
  only waiting the library does on its own finishes what a call started: a pulse's return to
  idle.

---

## 3. Points

### 3.1 One kind of point

Datapoint and setpoint become one point with an optional read side and an optional write side.
They differed only in that one could be written; the address space now travels with each side.

```python
Point(
    key=Key("room_3_temp_target", float),         # stable string and value type, see 3.8
    read=HoldingRegister(119),                    # optional
    write=HoldingRegister(119),                   # optional; may be a different space
    data_type=DataType.INT16,
    word_order=WordOrder.HIGH_FIRST, byte_order=ByteOrder.BIG,
    scale=0.01, offset=0,
    transform=None,                               # a pair, see 3.5
    no_data=(0x7FFF,),                            # sentinels, see 3.5
    limits=Limits(min=5, max=35, step=0.5),       # engineering units
    unit=Unit.CELSIUS,
    poll_rate=PollRate.MEDIUM,
    deadband=0.05,
    write_kind=WriteKind.STATE,
    labels={"room": 3},
)
```

Rules, checked when the point is constructed:

- at least one of `read` / `write`;
- `write` only into a writable space (holding, coil);
- a point in a bit space (coil, discrete) is one bit wide;
- `limits` only on a writable point, `no_data` only on a readable one.

The consumer asks `can_read(key)` and `can_write(key)`, and builds from that what it shows:

| can_read | can_write | a consumer shows |
|---|---|---|
| yes | no | a reading |
| no | yes | an action (a command) |
| yes | yes | a setting |

Read and write sides with different spaces is a real pattern — status in input register 100,
command in holding register 200 — so each side names its own space.

### 3.2 Access

The protocol defines what an address is.

| Protocol | Access | Spaces |
|---|---|---|
| Modbus | `InputRegister(a)`, `HoldingRegister(a)`, `DiscreteInput(a)`, `Coil(a)` | 4 tables |
| micro_nabto | `DatapointRegister(a, obj=0)`, `SetpointRegister(a, obj=0)` | datapoint / setpoint objects |

### 3.3 Register numbering

A Modbus request carries an address, 0 to 65535, in one of four tables. Manuals do not all
give that address:

- The Modbus Application Protocol specification (V1.1b3, 4.4) numbers each table's elements
  from 1, element X being address X - 1.
- Modicon's reference numbers (PI-MBUS-300) put the table in the leading digit: 0xxxx coils,
  1xxxx discrete inputs, 3xxxx input registers, 4xxxx holding registers, 40001 being address 0.
  Kepware's Modbus drivers also accept a six-digit form, 400001 to 465536.
- Kepware subtracts one by default, and has a setting, "Zero-Based Addressing", for devices
  where 40001 means address 1: reference numbers and the 0/1 offset vary independently.
- The Wavin Sentio manual gives addresses; verified live, InputRegister 51201 is address 51201.

Getting it wrong shifts every point by one register and still returns plausible numbers. So
the library assumes no convention: `ModbusOptions.numbering` is required, and so are a
model's options. It is a `RegisterNumbering`: per table, ranges of numbers, each saying which
address its first number is. That one form covers all of the above, and a manual that
continues a table in another range. `plain(first_address)` and `modicon(digits, first_address)`
are ready-made numberings. Both ask the same question, which address the first register is,
and neither has a default. The words zero-based and one-based are avoided: Kepware's
"Zero-Based Addressing" means the subtraction of one, the opposite of what the name suggests
for a manual that counts from 0.

It is declared **once per model**, because it is a property of how that vendor wrote the
manual. It is applied by the **protocol layer**, so the connection only ever sees addresses.
It cannot live on the connection: two devices behind one gateway can come from vendors with
different conventions. The model's overlap check compares addresses, not numbers, since two
ranges can map different numbers onto neighbouring addresses.

### 3.4 DataType

The names Modbus tools use (pymodbus among them), not a pair of `value_type` + `divider`
heuristics:

```
UINT16  INT16  UINT32  INT32  UINT64  INT64  FLOAT32  FLOAT64  BCD16  BCD32  BOOL
bit(n)  string(n, encoding)
```

- `FLOAT32` / `FLOAT64` are IEEE 754 — the format of most energy meters and drives. A scaled
  integer is an integer type with a `scale`, never a float.
- `bit(n)` is bit *n* of a 16-bit register; several points may share the address and are read
  with one request.
- Named states (fan modes, blind states) are not a data type: the key's type is an `IntEnum`,
  see 3.8, so every consumer uses the same states without re-implementing a mapping.
- Word and byte order apply to every multi-register type.

### 3.5 The value pipeline

Reading and writing are one pipeline run in opposite directions, so each step has an inverse:

```
read:   raw registers → decode (data type, orders) → no_data? → scale, offset → transform.read  → value
write:  value → limits check → transform.write → inverse scale, offset → encode → raw registers
```

- **Sentinels are explicit.** `no_data=(0x7FFF,)` produces quality `NO_DATA` (see 6.1), never a
  number and never a silent `None`.
- **Limits are in engineering units** — the numbers a user sees — and are checked before
  anything is encoded. They are also the entity's min / max / step.
- **A transform is a pair**, declared as one object with both directions, so a model cannot
  set one and forget the other:

  ```python
  Transform(read=lambda s: s / 60, write=lambda m: m * 60)   # seconds on the device, minutes shown
  Transforms.SECONDS_AS_MINUTES                              # the same, built in
  Transforms.INVERT_BOOL
  ```

  The model walker (4.4) samples the round trip `write(read(x)) == x`, which
  also catches lossy rounding such as `round(value / 60)`.

### 3.6 Write kind

- `WriteKind.STATE` — a setting. Repeated writes collapse to the newest value: 21.0, 21.5, 22.0
  ends at 22.0 without walking through the others.
- `WriteKind.COMMAND` — an action. Every write is sent, in order. Optionally a pulse: write, then
  write the idle value after a delay.

### 3.7 Units and other enums

Everything with a fixed vocabulary is an enum: `Unit`, `PollRate`, `WriteKind`, `Quality`,
`DataType`, the orders and the access spaces.

`Unit` follows world standards, not one consumer's:

- Its **value is the symbol** to show, written by the SI's rules (`"°C"`, `"kW·h"`, `"m³/h"`),
  and by UCUM's where the SI has none (`"wk"`, `"mo"`, `"a"`). Every symbol is in Latin-1, so
  even a legacy code page can show it.
- `Unit.X.code` is the **UCUM code**: unambiguous, case-sensitive ASCII (`"Cel"`, `"kW.h"`,
  `"m3/h"`), for exchange with systems that speak UCUM.
- The **member name** (`CELSIUS`) is the identifier, safe wherever a name must be plain.

A consumer with its own vocabulary for units maps to it.

Labels are free: `{"room": 3}`, `{"feature": "cooling"}`. Their meaning belongs to the model.

### 3.8 Keys

A key is a stable string. It ends up in the consumer's storage, typically in ids it builds from
it, so once a device is in use, changing a key string loses what the consumer built on it.

A key also carries the type of its point's value: `Key("room_3_temp_target", float)`. It is a
`str` subclass, so it is stored, compared and hashed as its text, and a type checker knows from
it what `value()` returns and what `write()` accepts: a wrong type is an error before the program
runs, not a surprise at a device. The same pattern types keys elsewhere: Home Assistant's
`HassKey[_T](str)` and aiohttp's `web.AppKey`.

| Key type | Registers | A read gives |
|---|---|---|
| `bool` | `BOOL`, a bit | `True` / `False` |
| `str` | `STRING` | the text |
| `int` | an integer that stays whole after scale, offset and precision | an `int` |
| `float` | any number | a `float` |
| an `IntEnum` | an integer, unscaled | its member; a number no member names is `NO_DATA` |

A point whose registers cannot hold its key's type is refused when it is created. The client
refuses a key whose type differs from the model's point (`TypeError`), and a subscription made
before `connect()` with such a key is logged and never told. The states stay integers: an
`IntEnum` member equals its number, and `DataValue.raw` keeps what the device sent, so a state
no enum names yet can still be told.

- A device library publishes its keys with its model; they are how a consumer names points.
- Repeated points get their key from the template, e.g. `Key(f"room_{n}_temp_air", float)`.
- The library is being built new and nobody runs it yet, so Sentio — the first consumer — is
  free to choose its keys now. After that they are frozen.
- **Nilan is different:** `nilan_proxy` runs in production behind the `nilan_connect`
  integration. When it moves onto this library, its key strings must come out identical, and a
  test compares the full key set before and after.

---

## 4. Models

### 4.1 Composition

A model is data: sections of points, identity points, and scan steps.

```python
class Room(StrEnum):                     # what one room has — typed, written once
    TYPE = "type"
    TEMP_AIR = "temp_air_current"
    TEMP_TARGET = "temp_air_target"

def room(n: int) -> list[Point]:         # one room; called for every room
    base = room_base(n)
    key = lambda name: f"datapoint_room_{n}_{name}"
    return [
        Point(key(Room.TYPE), read=InputRegister(base + 1), data_type=DataType.UINT16,
              poll_rate=PollRate.STATIC),
        Point(key(Room.TEMP_AIR), read=InputRegister(base + 4), data_type=DataType.INT16,
              scale=0.01, no_data=(0x7FFF,), unit=Unit.CELSIUS, deadband=0.05),
        Point(key(Room.TEMP_TARGET), read=HoldingRegister(base + 19),
              write=HoldingRegister(base + 19), data_type=DataType.INT16, scale=0.01,
              limits=Limits(5, 35, step=0.5), unit=Unit.CELSIUS),
    ]

SENTIO = Model(
    options=ModbusOptions(numbering=plain(first_address=1)),
    read_back_after=1.0,
    sections=[
        Section(BASE_POINTS),
        RepeatedSection(room, range(1, 25), label="room"),     # 24 rooms from one definition
        RepeatedSection(peripheral, range(1, 33), label="peripheral"),
    ],
    scan_steps=[probe_rooms, probe_peripherals],
)

NILAN = Model(
    options=MicroNabtoOptions(),
    read_back_after=2.0,
    # no identity points: the identity arrives with the handshake
    sections=[
        Section(BASE_POINTS),
        Section(COOLING_POINTS, when=lambda id: id.hardware_major >= 2),
        Section(HEAT_PUMP_POINTS, when=lambda id: id.device_model in HEAT_PUMP_MODELS),
    ],
)
```

(Addresses and names here are illustrative.)

`room(n)` is an ordinary function, so a model can loop, branch and compute. `RepeatedSection` calls it
for each number and labels every point with its instance, which is what lets the library:

- remove a whole instance in one call (4.3),
- hand the consumer one device per instance — a device per room,
- validate every instance (4.4).

Models are Python. A loader for a register map in a file (the "Modbus editor" case) can be
added on top later; it would produce the same objects.

### 4.2 The scan

The model is chosen and the installation examined before any value is published:

```
connect()
  1  open the connection
  2  identity             — from the handshake (Nilan) or by reading the identity points
  3  resolve sections     — keep the sections whose `when` matches the identity
  4  scan steps           — the model's own probes, in its order (Sentio: which rooms exist)
  5  first read           — everything the scan steps have not read; MISSING recorded as unavailable
  6  return               — the key list is final; the consumer builds its entities now
```

Steps 2–5 build the model, the availability record and the first values next to the current
ones, and commit them only if **every read was answered**. A read that goes unanswered never
reports a register missing, so a picture built from one would show absent parts of the model -
unconfigured rooms - as present. If a read goes unanswered, `connect()` raises
`CannotConnectError` and closes the connection it opened; the host retries later.

There is no address-level bootstrap: a model is chosen from the handshake or from its
identity points, and every step after that speaks in keys, so the register map is stated once.

**Scan steps** are declared by the model and run in its order. Each gets a `Scan`: the
identity, `read()` — which asks the device for what this scan has not read yet, and notifies
no one until the scan is committed, so a rescan cannot cause a storm of events — and
`set_available()`. What the steps read is kept for the scan: a later step gets it without a
request, and the first read (5) skips it. A step marks what this unit lacks; it never treats
an unanswered read as a missing register, since silence is not an answer.

`rescan()` runs 2–5 again on the open connection: availability is found afresh, the connection
and the subscriptions are left alone. If a read goes unanswered it raises, and the current
picture stays as it was. The host calls it — on reload, or on its own schedule.

### 4.3 Availability

Availability is the client's memory of what this unit does not have: a record keyed by point
key, with a reason that is for diagnostics only. Keeping it on the client, not on the points,
means a rescan replaces one record and a model is never rebuilt around it.

It is written at three moments: by scan steps, which catch structural facts cheaply (room 14
does not exist); by the first read, which records every `MISSING` the steps did not ask about;
and by every later read. `OFFLINE` is never recorded — the register exists, and what is behind
it will come back.

`has()`, `points` and `subscribe()` consult it, and none of them does I/O: a consumer calls
`subscribe()` once per entity, and it must stay a local lookup. An unavailable point is not
polled. A scan step's label selector marks a whole instance at once:

```python
scan.set_available(Labels(room=14), False, reason="room not configured")
```

Only the scan writes it: a consumer cannot hide a point the unit has.

### 4.4 Validation and the model walker

Point rules are checked at construction (3.1). Model rules are checked when a model is
resolved, at every `connect()`: duplicate keys (which would otherwise silently overwrite one
another), overlapping multi-register points, selectors that select nothing, and what the
protocol cannot carry. These are certain, cheap, and depend on the identity.

The **model walker**, `testing.assert_models_valid`, is what a device repository calls from
one test:

```python
def test_every_model_variant_is_valid():
    assert_models_valid(SENTIO, NILAN, identities=IDENTITIES)
```

Besides the rules above, it samples every transform: `write(read(x))` must give `x` back for
negative, fractional and large values. A sample is a test, not a proof, so it runs in the
device repository's tests and not at every connect. Test support lives in `testing`, never in
the modules that run in production.

Because sections declare their conditions, the walker can resolve every variant — every
combination of identity values the model distinguishes — and validate each one. Instantiating
a model with a single default identity would test one variant and let a mistake in the cooling
section through.

---

## 5. Freshness and scheduling

### 5.1 The idea

Poll rates, write effects, triggers, reconnect and "refresh now" are one mechanism: **every
point has a time at which it becomes due for reading.**

| Cause | Effect on the due time |
|---|---|
| the point's poll rate | due when its value is older than the poll rate's interval |
| a write that disturbs it | due at write + `after`, whatever its poll rate |
| a trigger | due now |
| reconnect | everything due now |
| `refresh(...)` from the consumer | the selected points due now |

This is how OPC UA works — the client states a sampling interval per monitored item and the
server schedules — and how SCADA drivers such as Kepware and Ignition treat scan rates per tag.

### 5.2 Poll rates

```python
class PollRate(Enum):
    FAST      # default 10 s
    MEDIUM    # default 30 s, and the default for a point
    SLOW      # default 60 s
    RARE      # default 15 min
    STATIC    # read at the scan and when triggered, never on a timer
```

A fixed vocabulary, so a consumer can offer one generic settings screen for every device.

- The **model** sets the default interval per poll rate — it knows the device.
- The **consumer** can override per poll rate, and per key:

  ```python
  client.set_poll_interval(PollRate.FAST, seconds=5)
  client.set_poll_interval("room_3_temp_air_current", seconds=2)
  ```

- The **device** may declare a floor. A consumer asking for faster is clamped, with a warning.
  Sentio answers 0x06 while it persists changes; hammering it makes that worse.

### 5.3 Who wants a point read

A point takes part in scheduling when something wants it: a subscriber, an explicit
`set_polling()`, or the model declaring it `poll_always`. A subscription with `poll=False`
delivers changes without causing reads on its own, which is what a "show everything"
editor needs.

Availability stays a veto over all of it.

### 5.4 The consumer's side

```python
await client.poll()                          # read whatever is due; costs nothing if nothing is
client.seconds_until_next_poll()             # how long until something is due, for sleeping
await client.refresh(PollRate.RARE)          # a "refresh now" button
await client.refresh(Labels(room=3))         # one room
await client.refresh(["a", "b"], after=2.0)  # later, e.g. from a plugin hook
```

The library still owns no timer. It owns a **plan**; the host owns the **clock** and calls
`poll()` from its own tick. One call reads everything that is due, whichever poll rate it
came from, so a fast point and a slow neighbour at the next address share one request.

`poll()` does not overlap itself: a call arriving while one is running returns once the
running one finishes, instead of queueing a second pass.

Order within a pass: points due because of a write or a trigger first, then the most overdue.

### 5.5 Write effects

```python
Point(Key("ventilation_step", int), read=HoldingRegister(1003), write=HoldingRegister(1003),
      limits=Limits(0, 4, step=1),
      on_write=Refresh(["fan_inlet_pct", "fan_outlet_pct", "fan_inlet_rpm", "fan_outlet_rpm"],
                       until_stable=30.0))
```

One re-read after the write is not enough. A fan ramps over tens of seconds, so a single read at
two seconds catches a value halfway, and nothing more happens until its next scheduled read.
Hence two times:

```
t = 0      write step 3
t = 2 s    read the step back (the device may clamp) and the affected points
t = 4 s    again — still changing, so keep following
t = 6 s …  every `after` while the values change
           stop when two reads in a row are equal, or when `until_stable` has passed
           → the points return to their own poll rate
```

- A written point is always re-read, whether or not it lists itself. The answer to a write
  says the request arrived, not what the device now holds: Modbus answers 0x06 with an echo
  of the request, and this library's micro_nabto write waits for no answer at all.
- `on_write` accepts keys or a label selector.
- Without `until_stable`, the affected points are read once after `after`.
- `after` defaults to the written point's read-back delay: its own `read_back_after`, else
  the model's, which is required. How long a device takes to show a write is known to no one
  but its model's author, so the library has no default; `testing.measure_read_back`
  measures it on a real device, writing to a point the author chooses.

### 5.6 Triggers

A cheap point that changes when expensive ones do:

```python
Point(Key("alarm_summary", bool), read=DiscreteInput(1), data_type=DataType.BOOL,
      poll_rate=PollRate.FAST, on_change=Refresh(Labels(kind="alarm")))
```

The 77 alarm inputs can then sit in `STATIC` and cost nothing until the summary moves. The same
fits a change counter, a "configuration changed" timestamp, or an alarm count.

`on_change` takes the same `Refresh` as a write effect, plus `when`: any change, rising or
falling. Its `after` defaults to 0 — a trigger reads at once.

Anything the declarative form cannot say, a plugin does in code: it subscribes to the point and
calls `refresh(...)` or `rescan()`. The declarative form compiles to those same calls, so there
is one mechanism, not two.

### 5.7 Time

Two clocks, for two jobs, both from one injectable `Clock`:

| | Python | Used for |
|---|---|---|
| **monotonic** | `time.monotonic()` — seconds as `float`, never moves backwards | due times, `after`, `until_stable`, intervals, backoff |
| **wall** | `datetime.now(timezone.utc)` | `DataValue.timestamp` — a moment a person or a database reads |

Scheduling must not run on the wall clock. It can jump: a Raspberry Pi without a real-time
clock boots at a stale time and jumps forward when NTP syncs, and an administrator can set it
by hand. On the wall clock, that jump makes every point overdue at once, or none due for hours.
The monotonic clock is immune, and it never leaves the library.

```python
class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now(self) -> datetime: ...

client = SentioClient(connection, clock=FakeClock())    # tests: advance time by hand
clock.advance(seconds=2)                                # moves both clocks together
```

Without a `clock` argument the client uses the system clocks.

---

## 6. Values and events

### 6.1 Every value carries its quality

```python
@dataclass(frozen=True)
class DataValue[T]:
    value: T | None                # of the key's type, see 3.8
    quality: Quality
    timestamp: datetime            # when the device answered; timezone-aware UTC
    raw: tuple[int, ...] = ()      # what the device answered, before decoding
```

`timestamp` is a `datetime` in UTC, Python's own type for a moment in time. Scheduling does not
use it: see 5.7. `raw` is kept for `NO_DATA` too, so a sentinel or a state no enum names yet is
still there to see and to report.

This is OPC UA's DataValue. It is what the earlier `PointRead` was reaching for, and it applies
to every read, not only to a scan step's question.

| Quality | Cause | A consumer shows |
|---|---|---|
| `GOOD` | the device answered with a valid value | the value |
| `NO_DATA` | the device answered with a `no_data` sentinel | unknown |
| `OFFLINE` | 0x04: the device answered, but what is behind the register does not respond | unavailable |
| `STALE` | the last attempt failed (timeout, other error); the previous value is kept | unavailable after its own tolerance, see 6.3 |
| `MISSING` | 0x02: this unit does not have the register | nothing: it builds nothing for it |

### 6.2 Change events

Subscribers are told when the value **or the quality** changes. A point going offline is an
event even if its last value was the same.

`deadband` suppresses changes smaller than itself, so a temperature flickering between 21.49 and
21.50 does not produce an event every tick. Quality changes always pass.

The callback receives the old and the new `DataValue`.

### 6.3 In a consumer

- One task per device calls `poll()` — at a fixed short interval, or sleeping for
  `seconds_until_next_poll()`.
- What the consumer shows subscribes, and updates from the callback, so it changes only when
  its own value does.
- What to build from `can_read` / `can_write`; unit, limits, step and device from the point.
- A tolerance before `STALE` is shown as unavailable, so one lost packet does not flap a
  value. The library exposes the consecutive failure count; the tolerance is the consumer's
  policy.
- **Whether the device is reachable** is `Status.CONNECTED`, and it is decided by answers, never
  by the socket: a TCP socket stays open for minutes after a cable is pulled. A pass or a write
  that nothing answered makes it False; the first answer makes it True again and makes every
  value due. Each change is logged once, not on every failed read.

---

## 7. Writes

```python
await client.write(ROOM_3_TEMP_TARGET, 21.5)          # Key("room_3_temp_target", float)
```

1. Refused at once in read-only mode (7.4).
2. Validated against `limits`, in engineering units.
3. Run backwards through the pipeline (3.5).
4. Sent with the function code for the space and the device's write policy.
5. Its effects scheduled (5.5).

**Write policy**, per device:

| Write | Default | Alternatives |
|---|---|---|
| one holding register | `0x06` | `0x10` — some devices only implement 0x10 |
| several holding registers | `0x10` | — |
| one coil | `0x05` | — |
| several coils | `0x0F` | — |
| one bit inside a holding register | `0x16` Mask Write: atomic on the device | read-modify-write, serialised per register, for devices without 0x16 |

**Coalescing** applies to `WriteKind.STATE` only (3.6). Writes to one device are sent in order;
a write rejected as busy is retried before later writes, not after them.

**Sequences:** some devices need unlock → write → save. `client.write_sequence([Write(key, value),
...])` sends an ordered list as one operation and stops at the first refusal; each `Write` is
typed by its key.

### 7.4 Read-only mode

```python
client = SentioClient(connection, read_only=True)
```

Every write raises `ReadOnlyError` before anything reaches the connection. It is the rule we
already follow for the live Sentio and Nilan installations, made into a feature: live tests use
it instead of monkeypatching write methods, and a user who only wants monitoring can turn it on.

---

## 8. Topology

The Modbus unit id is a field of every request, not a property of the connection. pymodbus
already takes it per call; only our transport pins it at construction.

```python
gateway = ModbusTcpConnection(host, port)        # one link; the host owns and closes it
lamps   = LampClient(gateway, unit_id=1)
fan     = FanClient(gateway, unit_id=2)
blinds  = BlindClient(gateway, unit_id=3)
```

- The **connection** sends one request at a time, keeps an optional pause between frames (RS-485
  gateways need it), and reconnects.
- Every request returns its **status with its response**, never through a "last call"
  property: on a shared connection that would describe whichever request finished last.
- Reachability, `OFFLINE` and failure counts belong to each device. A device behind a gateway
  can stop answering while the others go on.
- **Gateway exception codes:** 0x0B is the gateway reporting that the device behind it did not
  answer. For that device it is no answer at all, the same as a timeout on a direct link, and it
  counts towards backoff: the gateway waited out its own bus timeout before saying so. 0x0A
  (no path to the device) is a configuration error.
- **Automatic backoff:** a device that keeps timing out is polled less often for a while, so its
  timeouts do not starve every other device on the bus. On a shared RS-485 bus this is what
  keeps one dead fan from freezing the lights.

Every device behind a gateway shares its bus, and each request occupies it for all of them. On
a slow serial bus this is where poll rates (5.2) stop being an optimisation and become necessary.

**micro_nabto** is one device per UDP session, with no gateway to share. Where each fact below
comes from is stated: uNabto's reference implementation
([github.com/nabto/unabto](https://github.com/nabto/unabto)), or a measurement on one Nilan
CTS 402 — which other devices may not share.

- **The session ends silently when unused.** uNabto sets the timeout to 7/2 × the keep-alive
  interval the client states in its connect request, 5 s when none is stated, as here: 17.5 s.
  The CTS 402 was measured ending it between 15 and 20 s. A session unused for 12 s is
  established anew before the next request, so a slow poll rate never costs an unanswered
  read; `session_idle` changes that for a device that differs.
- UDP loses datagrams. An unanswered request is sent again; then the session is established
  anew, at an address found by the device id if it has changed, before the device counts as
  unreachable.
- **Every answer is checked** against uNabto's checksum (a 16-bit sum) and padding (to an
  even length); a damaged one counts as lost. The CTS 402 sends a packet without an answer
  ahead of every answer; any such packet is ignored.
- **One unknown address refuses a whole read** on the CTS 402, answered with a count of 0.
  A refused read is halved until the refusal is pinned down, as Modbus 0x02 is; any count
  that does not match is treated the same way, so a device that answers differently is not
  misread.
- A setpoint write is sent without waiting for a confirmation; the read-back (7) shows whether
  it took.

---

## 9. Diagnostics

Per device: requests, failures by kind, average latency, backoff state, the consecutive failure
count. Per point: last quality and when it was last good. This is what a bug report needs —
without a host address in it.

---

## 10. Testing

- **Simulators** are the standard doubles: `SimulatedModbusDevice`, a register image that refuses
  requests the way real units do, several of them behind a `SimulatedModbusGateway`; and
  `SimulatedMicroNabtoDevice` on a localhost UDP port, answering byte for byte as a real device.
- **An injectable clock** (5.7), so scheduling, `after`, `until_stable` and backoff are tested
  without waiting.
- **The model walker** (4.4) in every device repository.
- **The key migration test** (3.8) for Sentio and Nilan.
- **Live tests** stay read-only, opt-in, and use read-only mode.

---

## 11. Subtleties the protocols must survive

A library that gets these wrong is worse than none.

| Subtlety | How it is handled |
|---|---|
| `0x02`: the register is absent | `MISSING`, recorded as unavailable, not polled again until a rescan |
| `0x01`: the function is unsupported | `UNSUPPORTED`, recorded like a missing register |
| `0x04`: what is behind the register is not answering | `OFFLINE`; the point stays polled |
| `0x06`: busy | retried four times, the wait doubling from 0.2 s, then `BUSY` |
| `0x0B`: the gateway's device did not answer | `NO_ANSWER`, counted towards backoff |
| No answer at all | `NO_ANSWER`; the value goes `STALE`; reachability follows answers, never the socket |
| One bad address refuses a whole request | the request is halved until the refusal is pinned down: a few absent addresses among many cost few requests, though a request where every address is absent costs about twice as many as reading each alone |
| Devices accept different request sizes | `max_registers` / `max_bits` per model |
| Four address spaces | one access class each; never batched together |
| Word order and byte order | per point |
| Sixteen flags in one register | `DataType.bit(n)`; points sharing an address are read once |
| A value meaning "no reading" | `no_data` / `raw_range` give `NO_DATA`, distinct from `MISSING` |
| Read and write at different addresses | a read side and a write side |
| Manuals number registers in their own ways | `RegisterNumbering`, once per model |
| A reply arriving after its timeout | the link is dropped, so it never answers the next request |
| One silent device behind a gateway | backoff per device; the others are not slowed |
| A slot index is not an identity (peripherals reorder) | the model's: a serial number is the identity |

---

## 12. Out of scope

- **A device's vocabulary.** A room, a peripheral, a heating circuit belong to the model; the
  library offers labels to select by.
- **Capability tables.** Which peripheral has humidity, which unit has a heat pump — model
  knowledge, changing on a different clock than the library.
- **Timers.** No polling loop, no scheduled rescan, no background thread.
- **Entity shape.** Which value becomes a sensor, a climate entity or a diagnostic is the
  integration's call.

---

## 13. Decisions and open questions

**Decided**

1. **`until_stable` is opt-in.** A write effect without it reads its points once after
   `after`; re-reading until stable is declared per effect.
2. **Time** (5.7): `DataValue.timestamp` is a UTC `datetime`; scheduling uses the monotonic
   clock; both come from one injectable `Clock`.
3. **micro_nabto access covers Nilan.** Checked against every model in `nilan_proxy`, which runs
   in production: each point is one register at object 0 — `read_obj` / `write_obj` exist but no
   model sets them — with signed, divider and offset, and setpoints add a write address and
   min / max / step. No strings, no transforms. `DatapointRegister(a)` / `SetpointRegister(a)`, with the object
   defaulting to 0 and the length following from the data type, covers all of it.

   Two migration notes. `nilan_proxy` states min / max in **register** units, while limits
   here are in engineering units (3.5), so migration converts them. And `nilan_proxy` picks a
   model from a decision table over four handshake values — device model, device number, slave
   device number, slave device model — without reading a register; a CTS 402 reports 1140 /
   72270 / 1 and runs as its `CTS400` model. A `ModelSelector` over the handshake identity
   expresses that table.

**Open**

4. **Deadband default:** none (every change reported), or a default per unit?
   Proposed: none; a model sets it on the points whose device is noisy.
5. **Confirming a micro_nabto write.** The device sends a receipt ahead of every answer to a
   read. If it sends one for a write too, a write could be sent again until confirmed instead of
   relying on the read-back. Finding out takes one write to a live device.
6. **Telling a consumer that a rescan changed the key list.** Nothing signals it today; a
   consumer that adds and removes entities has to compare `keys` before and after.
