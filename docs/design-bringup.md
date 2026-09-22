# Device bring-up and availability

**Status:** proposed. Nothing here is implemented yet.

This document describes how a device is brought up — how the library learns what it is talking
to, what that particular unit actually has, and how that is presented to the consumer. It is
the contract a plugin author writes against, so it is meant to outlive any one device family.

Claims about the *current* behaviour were read out of the code, not remembered. Where a claim
was measured against hardware it says so.

---

## 1. Why this exists

Two device families use this library today, and they are different enough to be a fair test:

| | Wavin Sentio | Nilan |
|---|---|---|
| Transport | Modbus TCP | Nabto (`micro_nabto`) |
| Models | exactly one | many, chosen from a handshake |
| Identity | must be read from registers | arrives with the connection |
| What varies | which rooms and peripherals are installed | which features the model has |

Everything below has to serve both without either one being a special case.

### What is wrong with the current bring-up

All five were verified in the code.

**The register table is implied by the point's class.** `ModbusDatapoint` is read with function
code `0x04` (input registers) and `ModbusSetpoint` with `0x03` (holding registers), hardcoded in
`_request_datapoint_read` / `_request_setpoint_read`. That conflates *readable vs writable* with
*which address space*. A read-only holding register has to be modelled as a setpoint without a
write address, and discrete inputs (`0x02`) and coils (`0x01`/`0x05`) cannot be modelled at all.
Sentio's 77 alarm registers are discrete inputs.

**A read result cannot say what went wrong.** `_request_points_read` returns `(point, value)`.
A register the unit does not have (`0x02`), a register whose peripheral is offline (`0x04`) and
a register holding its invalid-value sentinel all arrive as `None`. Three different facts, one
answer. A plugin cannot act on the difference because it never sees it.

**One boolean is assigned by four parties.** `pointdata.read` is written by `subscribe()`, by
`set_read()`, by the model's `Read.ALWAYS` flag, and by the `0x02` handler via
`set_read(..., force=True)`. Last writer wins, so ordering decides the outcome. Demonstrated
against the real code with a transport that answers `0x02`:

```
1. after subscribe          : read = True
2. after 0x02 from device   : read = False   <- correct
3. after another subscribe  : read = True    <- resurrected
4. after set_read           : read = True    <- resurrected
5. after reconnect          : read = True    <- resurrected
```

Home Assistant creates one entity per value, so step 3 is the normal case, not an edge case.
The log line *"it will not be read again"* is therefore untrue.

**`instantiate()` wipes what discovery found.** It rebuilds `_datapoints` and `_setpoints` from
scratch. Any read flag or availability decision made before it is lost. The documented way for
a model to gain features — `_version_changed()` then `instantiate()` — is exactly the thing
that would erase Wavin's discovery.

**`_version_changed` has one user and it writes a log line.** Defined in `modbus_models.py`,
called from one place, overridden in one place: `WavinSentio`, where it warns if the address
space is older than 3.2. It does not change the model. Its docstring advertises adding and
removing features; nobody does that. Building Nilan's feature selection on it would mean
building on a path that has never run.

**The device layer reaches past the library.** Wavin's discovery calls the transport directly
with raw addresses (`wavin_sentio_connect.py` lines 208, 223, 245) and re-implements UTF-8
decoding the model already knows. The register map is therefore stated twice, and those reads
skip batching and busy-retry.

---

## 2. Principles

**The library never guesses what a device has.** It provides a way to ask, a place to record
the answer, and a point in time to do the asking. What the answer *means* belongs to the
plugin.

**Nothing derived is ever assigned.** Where several parties have an opinion, each records its
own, and the result is computed. This is the fix for the four-writers problem and it is applied
everywhere, not just there.

**One failing register must never cost another.** A device that rejects a batch because one
address in it is bad is normal. Isolation is the library's job, not the plugin's.

**Order is the plugin's to choose.** The library runs the steps that are declared, in the order
they are declared. It has no opinion about what comes first.

**When `connect()` returns, the picture is final.** The consumer can build its entities straight
from the key list. Nothing appears or disappears later without an explicit signal.

---

## 3. Points

A point gains three things. All three default to today's behaviour, so existing models keep
working unchanged.

### 3.1 Which address space

```python
class RegisterTable(Enum):
    INPUT     = auto()   # FC 0x04, read-only 16-bit
    HOLDING   = auto()   # FC 0x03 / 0x06 / 0x10, read-write 16-bit
    DISCRETE  = auto()   # FC 0x02, read-only 1-bit
    COIL      = auto()   # FC 0x01 / 0x05, read-write 1-bit
```

`ModbusDatapoint` defaults to `INPUT` and `ModbusSetpoint` to `HOLDING`, so nothing changes
until a model says otherwise. The four tables are separate address spaces: address 1 in
`INPUT` and address 1 in `HOLDING` are unrelated registers, and batching must never merge
across tables.

This is what makes Sentio's 77 alarms expressible, and what lets a device that exposes
everything through holding registers — which is common — be modelled honestly.

Writability stops being implied by the class. A point is writable if it has a write address.
A `ModbusSetpoint` in `INPUT` is a contradiction and is rejected at `instantiate()`.

### What `ModbusDatapoint` and `ModbusSetpoint` mean now

With the address space moved out into `register_table`, the two classes differ in exactly one
thing: **a datapoint is read-only, a setpoint is writable.** Nothing else. A read-only serial
number living in a holding register is a datapoint; the name says nothing about where it lives.

`ModbusDatapoint` has no `write_address` field at all, so this is enforced by construction
rather than by convention. `ModbusSetpoint` may in turn omit `read_address`, which is how a
write-only register is expressed — Sentio's Modbus password (`HR 00006`, access `W`) is one.

They are deliberately **not** merged into one class, for two reasons. `micro_nabto` uses both
throughout its read, write and command-building paths, and it must stay byte-identical. And the
split is the contract consumers see: in Home Assistant a datapoint becomes a sensor and a
setpoint becomes a `number`, `select` or `climate` entity.

A device model should set the table per point, or per device where most points share one —
putting everything in holding registers is common, and annotating hundreds of points by hand is
a mistake waiting to happen. A device-level default (`_attr_default_register_table`, in the
style of the existing `_attr_default_extras`) covers that case; a point that declares its own
table wins over it.

### 3.2 Word order for multi-register values

`ModbusParser.combine_values` currently folds registers high-word-first, unconditionally.
Plenty of devices — energy meters and inverters especially — put the low word first, and some
swap bytes within each word as well.

```python
word_order: WordOrder = WordOrder.HIGH_FIRST   # or LOW_FIRST
byte_order: ByteOrder = ByteOrder.BIG          # or LITTLE, within each register
```

Defaults match today's behaviour. This applies symmetrically to writes.

### 3.3 Bit fields

A single register holding sixteen independent flags is ordinary Modbus. Today it can only be
read as one number and picked apart by the consumer, which means sixteen meanings hide behind
one key.

```python
read_bit: int | None = None   # 0-15 within the register at read_address
```

Points that share an address are read **once** and decoded separately. That is a change to
`batch_reads`, which today requires strictly ascending, contiguous addresses
(`previous.read_address + previous.read_length == point.read_address`) and would treat two
points at the same address as non-contiguous.

For `DISCRETE` and `COIL` the register *is* a bit, so `read_bit` does not apply there.

---

## 4. Reading, and what a read says

### 4.1 The outcome

Every read of every point produces one of four outcomes. This is the heart of the design: the
three facts that collapse into `None` today stay separate.

```python
class ReadOutcome(Enum):
    OK           # the device answered; value is decoded
    MISSING      # 0x02 ILLEGAL_DATA_ADDRESS - this unit does not have this register
    OFFLINE      # 0x04 SLAVE_DEVICE_FAILURE - it exists, what is behind it is not responding
    ERROR        # anything else: other exception, timeout, no response, decode failure
```

```python
@dataclass(frozen=True)
class PointRead:
    key: ModbusPointKey
    outcome: ReadOutcome
    value: MODBUS_VALUE_TYPES | None = None
    exception_code: int = 0     # the raw code, for logging and for unusual devices
```

`OK` with `value is None` is meaningful and must be preserved: the device answered, and the
register held its invalid-value sentinel — `0x7FFF` for a Sentio `val_d2_fp100`, for instance.
That is a sensor reporting "no reading", not a register that is absent.

The distinction between `MISSING` and `OFFLINE` is the difference between a permanent fact
about this installation and a condition that clears when someone plugs the thing back in. Only
the first should ever silence a register.

### 4.2 The query

```python
async def read_keys(
    self, keys: Sequence[ModbusPointKey]
) -> Dict[ModbusPointKey, PointRead]: ...
```

Contract:

- **Every key gets an entry.** A device-level failure is reported as an outcome, never raised.
- **It batches**, respecting the device's maximum request length and never merging across
  register tables.
- **It isolates.** A batch rejected with `0x02` is re-read one point at a time, so only the
  genuinely absent registers are reported `MISSING`. This already works
  (`_handle_batch_failure`); what is new is that the outcome survives into the result.
- **It retries `0x06`.** Busy is not a failure; the Sentio manual states the request "shall be
  repeated again".
- **It ignores read flags and availability.** You asked for these keys explicitly, so the
  answer is not filtered by what the client would otherwise poll — including keys already
  marked unavailable, which is how you re-test them.
- **It stores nothing and notifies nobody.** It is a question, not a poll. Values reach
  subscribers through the ordinary read path only.

That last point matters for `reinit()`, which runs while consumers are subscribed and must not
produce a storm of events as a side effect of asking questions.

---

## 5. Availability

### 5.1 The record

Availability is the library's memory of what this unit does not have.

```python
def set_available(self, keys: ModbusPointKey | Iterable[ModbusPointKey],
                  available: bool, *, reason: str = "") -> None: ...
def is_available(self, key: ModbusPointKey) -> bool: ...
def clear_availability(self) -> None: ...

@property
def available_keys(self) -> Set[ModbusPointKey]: ...
```

It is **stored on the client**, keyed by point key — not on the point objects. Three
consequences, all of them deliberate:

- `instantiate()` cannot wipe it, so a step that rebuilds the model is no longer a trap.
- `reinit()` is one dict to clear.
- A plugin can mark four hundred keys in one call, which is what a group decision looks like.

`reason` is for diagnostics only. The library never branches on it.

### 5.2 Who writes it, and when

Three moments. All three write to the same record.

**Init steps** — cheap and targeted. One read per group catches the structural facts: room 14
does not exist, room 3 is a DUMMY, slot 12 is a display and not a thermostat. Measured on a
real CCU-208: 80 probes in 69 ms.

**The first full read** — catches everything the steps did not know to ask about. It runs
inside `connect()` and it happens anyway, so it costs nothing extra. Measured: 643 points,
101 requests, 310 ms. Any `MISSING` it finds is recorded.

**Every later read** — catches whatever changes afterwards.

`OFFLINE` is never recorded as unavailable. The register exists; something behind it is not
answering right now, and it will come back.

### 5.3 Who reads it

`provides(key)` and `subscribe(key, …)` consult it. **`subscribe()` never discovers anything.**
Home Assistant calls it once per entity — dozens of times — and it must stay a cheap local
lookup. Putting network I/O behind it would be a trap laid for every consumer.

Reading is also gated on it: an unavailable point is not polled. That replaces
`set_read(..., force=True)` entirely, and the `force` parameter goes away.

### 5.4 The read flag, without the four writers

`pointdata.read` stops being assigned and becomes derived:

```
read  =  (has a subscriber  or  set_read() asked  or  model says ALWAYS)
         and is_available(key)
```

Nobody writes the result, so nobody can overwrite anybody. `unsubscribe()` removes one reason
and leaves the others standing. `Read.ALWAYS` becomes a third reason rather than a special case
buried inside `set_read`. All five resurrections in §1 become impossible by construction, not
by convention.

---

## 6. Bring-up

### 6.1 The sequence

```
connect()
  1  open the transport
  2  build the model              — from whatever the protocol gave for free
  3  run the plugin's init steps  — in the plugin's order
  4  read everything              — MISSING outcomes are recorded
  5  return
```

There is deliberately **no address-level bootstrap phase**. Both device families here choose
their model without reading anything: Sentio has exactly one model
(`_translate_to_model` returns `WavinSentio` unconditionally), and Nilan's arrives in the
handshake. Every step after step 2 speaks in keys, so the register map is stated once, in the
model.

If a future device genuinely cannot choose a model without reading, the answer is a small model
containing only its identity registers, read by a step, followed by a step that swaps the model.
That is the mechanism below, not a new one.

### 6.2 Steps

```python
class InitStep(ABC):
    name: str
    """Used in logs and in the error when this step fails."""

    def reads(self) -> Sequence[ModbusPointKey]:
        """Keys to read before apply() is called. May be empty."""

    async def apply(self, client: ModbusEventConnect,
                    results: Mapping[ModbusPointKey, PointRead]) -> None:
        """
        Act on the result.

        May mark keys unavailable, may change the model, may read more via
        client.read_keys() when the second read depends on the first.
        Raise InitAborted to fail connect() with a reason.
        """
```

The plugin declares them:

```python
def init_steps(self) -> Sequence[InitStep]: ...
```

They run in list order, and each sees the effect of the one before. A step that needs to run
before the version check simply goes first; the library has no opinion.

Steps are declared by the **client**, not the model. The model is a register map. The client is
how a device is brought up and presented, and it is what a plugin author writes anyway. A step
that changes the model does so explicitly, and the library re-instantiates afterwards —
availability survives, because it lives on the client.

`_version_changed` is removed. Wavin's address-space warning becomes its first step.

### 6.3 Failure

A step that raises `InitAborted` fails `connect()` with that reason, and the consumer sees it
the way it sees any other connection failure. A step that simply finds nothing — every read
came back `ERROR` because the device went away mid-bring-up — should abort rather than record
four hundred keys as missing. Recording absence requires a clear answer, not silence.

This is the one rule a plugin author can get wrong in a way that is expensive: `ERROR` must
never be treated as `MISSING`.

---

## 7. reinit

```python
async def reinit(self) -> None:
    """Re-run bring-up against the open connection."""
```

Clears availability, runs the steps again, does **not** reconnect and does **not** touch
subscriptions. A room added on the controller appears; a room deleted disappears.

Why it is worth having, given that a Home Assistant reload already tears the integration down
and calls `connect()` again: reload rebuilds every entity and drops the connection. `reinit()`
refreshes the picture without either. It is the difference between restarting the car and
checking the mirror.

**The library owns no timer.** `reinit()` is called by the host — on reload, or on whatever
schedule its coordinator runs. This is the same rule that already governs polling, and for the
same reason: a library that schedules its own work fights the host's event loop and takes the
interval out of the user's hands.

### Telling the consumer something changed

```python
ModbusStatusKey.AVAILABILITY_REVISION   # integer, increments when available_keys changes
```

It behaves like every other status key — subscribable, change-filtered, pushed by the client
rather than read from a register. A consumer that wants to add and remove entities watches it;
one that does not, ignores it.

---

## 8. How the two families look

### Wavin Sentio

```
model:  WavinSentio, unconditionally. One model, no identity read needed.

step 1  "address space"
        reads   DATAPOINT_MAJOR, DATAPOINT_MINOR
        applies warn if older than 3.2

step 2  "installed rooms and peripherals"
        reads   ROOM_n_TYPE for n in 1..16          (IR base+27)
                PERIPHERAL_n_TYPE for n in 1..64    (IR base+1)
        applies MISSING            -> the whole group is unavailable
                type == DUMMY      -> the group's measurement keys are unavailable
                peripheral type    -> capabilities of that model decide the subset
                then reads names, serials and owners for what survived,
                and publishes SentioDiscovery

step 3  (implicit) the full read records anything else that answers 0x02
```

`_absent_keys` and the `subscribe()` override in `WavinSentioTCPConnect` are deleted — they are
a local reimplementation of §5.

One thing is unresolved and must not be guessed: the manual says a DUMMY room has *"no
thermostat or sensor installed"* but does not say what its temperature registers return. The
user's unit has no DUMMY rooms, so it cannot be measured. Marking those keys unavailable is
safe in one direction only — if a DUMMY room does report a usable value, we would be hiding it.
See §11.

### Nilan

```
model:  chosen from the handshake (model, device_number, slave_device_number,
        slave_device_model), exactly as nilan_proxy does today.
        Feature selection stays in the model's __init__, driven by its quirk table:

            if self.device_has_quirk("hotwaterTempSensor", slave_device_model):
                self.datapoints[TEMP_HOTWATER_TOP] = ...

        Nine named quirks, each a list of ~25 model numbers. It works because the
        identity is exact; there is nothing to infer.

steps:  none required. An empty list is a valid list.
```

That the Nabto handshake hands over the identity for free is Nilan's business. The library runs
whatever steps are declared, and zero is a number. No special case.

Once ported, Nilan also gains something it does not have today: it can *probe* for a feature
instead of looking it up. A quirk table has to grow with every new model number; asking the
device whether a register exists does not. The table remains useful as a fallback for registers
that exist but changed meaning — which probing cannot detect.

---

## 9. Modbus subtleties this design has to survive

Collected because a library that gets these wrong is worse than no library. Each row says
whether it is handled today.

| Subtlety | Today | Under this design |
|---|---|---|
| `0x02` — register absent | handled, outcome discarded | `MISSING`, recorded, never re-read |
| `0x04` — peripheral offline | handled, outcome discarded | `OFFLINE`, value cleared, point stays enabled |
| `0x06` — busy | retried with backoff | unchanged |
| `0x01` — function unsupported | logged as an error | `ERROR`, and the table is now explicit so it is diagnosable |
| No response at all | indistinguishable from an exception | `ERROR` with `exception_code == 0` |
| Whole batch rejected for one bad address | falls back per point | unchanged, but the per-point outcome is reported |
| Max registers per request varies | `_attr_max_request_length`, Sentio declares 32 | unchanged |
| Four separate address spaces | only two reachable from a model; the transport can also read discrete inputs but no point can ask for them, and coils are missing entirely | `RegisterTable`, never merged when batching |
| 32-bit word order | hardcoded high-word-first | `word_order`, default unchanged |
| Byte order within a register | hardcoded big-endian | `byte_order`, default unchanged |
| Bit fields in one register | not expressible | `read_bit`, shared addresses read once |
| Invalid-value sentinels | `max` / `max=-1` | unchanged; distinct from `MISSING` |
| Signed values | `signed`, min derived from it | unchanged |
| Strings | `ModbusValueType.UTF8`, no length prefix | unchanged |
| Read address ≠ write address | `read_address` / `write_address` | unchanged |
| Registers that change meaning between firmware versions | not expressible | a step reads the version and swaps the model |
| Identity in the protocol vs in registers | two code paths | one: step 2 builds the model from whatever is known |
| Index is not identity (peripheral slots reorder) | documented, left to the consumer | unchanged; the serial is the identity |
| Late response after a timeout desynchronises the stream | real, observed | see §11 |

That last one is not hypothetical. Running the test suite against the live controller produced
`request ask for transaction_id=5 but got id=2, Skipping`, and every request after it failed.
The trigger was a pytest event-loop scope mistake rather than the library, but the failure mode
— one timeout poisoning the connection — is real and unguarded.

---

## 10. What is deliberately not in the library

**Groups.** A room, a peripheral, a heating circuit — those are Wavin's vocabulary. A plugin
computes which keys a group covers and marks them in one call. If three device libraries later
turn out to want the same thing, it can move down then, having earned it.

**Capability tables.** Which peripheral model has humidity, which Nilan model has a sacrificial
anode — plugin knowledge, and it changes on a different clock than the library does.

**Timers.** No polling, no scheduled re-discovery, no background thread. The host decides when.

**Entity shape.** The library reports keys, values and availability. What becomes a sensor, a
climate entity or a diagnostic is the integration's call.

---

## 11. Open questions

**What does a DUMMY room return?** The manual says no thermostat or sensor is installed. It
does not say whether the temperature registers answer with the invalid sentinel, with zero, or
with `0x02`. No DUMMY room is available to measure. Options: implement from the manual's
meaning and mark it unverified, or leave DUMMY handling out until a unit with one can be read.
Not a design question — a fact we do not have.

**Should a timeout invalidate the connection?** One late response can desynchronise a pymodbus
transaction stream, and every subsequent request fails until it is reset. Reconnecting after a
timeout is the safe answer and costs a socket; tolerating it risks a dead client that still
reports connected. Needs a decision, and it belongs in the transport rather than here.

**Should version points be polled?** They are read at startup, and afterwards only if a
consumer subscribes to them. So whether a firmware change is noticed mid-session is currently
an accident of what the integration chose to expose. Polling them costs one extra register in a
batch that is being sent anyway.

---

## 12. What changes, concretely

| File | Change |
|---|---|
| `modbus_models.py` | `RegisterTable`, `word_order`, `byte_order`, `read_bit`; `read` becomes derived; `set_read` loses `force` |
| `modbus_event_connect.py` | availability record; `read_keys()`; `subscribe`/`set_read` become reasons; `_version_changed` removed |
| `modbus_tcp_event_connect.py` | read path returns `PointRead`; register table chosen per point; init steps run in `connect()`; `reinit()` |
| `modbus_deviceadapter.py` | passthrough for the above |
| `transport.py` | add `read_coils` / `write_coil`. It has `read_input_registers`, `read_holding_registers`, `read_discrete_inputs`, `write_register` and `write_registers` today — coils are the one gap |
| `micro_nabto/` | **unchanged.** Its one call, `set_read(key, False, force=True)`, keeps working: `force=True, read=False` maps onto "mark unavailable". Verified byte-identical to HEAD and must stay so. |
| `wavin_sentio_connect.py` | discovery becomes steps; `_absent_keys` and the `subscribe()` override deleted; raw-address reads replaced by `read_keys()` |

`micro_nabto` stays byte-identical, and it inherits the availability fix for free — it has the
same resurrection bug today, through the same line.
