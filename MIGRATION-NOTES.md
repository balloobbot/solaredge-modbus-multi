# Migrating solaredge-modbus-multi to modbus-connection

This branch replaces the integration's hand-rolled pymodbus layer with
[modbus-connection](https://github.com/home-assistant-libs/modbus-connection)
4.4.0 on the tmodbus backend. The register map now lives in a Home
Assistant-free device library at
`custom_components/solaredge_modbus_multi/solaredge/`, built on the
`Component` / `ComponentGroup` model framework, and the entity platforms read
typed attributes instead of a dict of decoded values.

Net: **-4,470 / +2,960 lines** across the component, of which 1,503 lines are
the new device library. `hub.py` went from 2,831 lines to 955 and `sensor.py`
from 2,597 to 1,938. There were no tests before; there are 38 now, all against
modbus-connection's in-memory mock backend, with no hardware and no Home
Assistant instance.

---

## 1. What weird things does this library do?

**Several real inverters as distinct slave IDs on one socket.** This is the
thing that made it interesting: a SolarEdge gateway hosts up to 32 inverters as
separate Modbus unit IDs, each an independent SunSpec device with its own
meters and batteries behind it. Almost every other Home Assistant Modbus
integration only ever talks to unit 1. This one is the shared-connection case
`ModbusConnection` / `for_unit()` was designed for, and it fits: one connection,
one `for_unit(id)` per inverter, requests serialized internally.

**Meter addresses shift by the length of a model that may not exist.** Meter
_n_ lives at a fixed address (40121, 40295, 40469) — unless the inverter
publishes SunSpec model 160 (multiple MPPT), in which case every meter slot
moves along by that model's whole length. The old code carried this as a
hardcoded `+50` for two MPPT modules and `+70` for three. Those constants are
the model's length plus its two header registers, so `scan()` now reports them
and the arithmetic comes from the device. That is the single clearest win of
the SunSpec discovery support.

**Two word orders in one device — and in one block.** The SunSpec map at 40000
is big-endian, as the spec says. Every proprietary block (site limit at 0xE000,
storage control at 0xE004, batteries at 0xE100/0xE200/0xE300, power control at
0xF000/0xF100/0xF156) is **CDAB**: 32- and 64-bit values arrive with their
words swapped. Worse, SolarEdge repurposes two of the SunSpec inverter model's
vendor event registers as its own scalars, and gives them _different_ word
orders from each other:

| Offset | SunSpec point | SolarEdge uses it as                       | Word order |
| ------ | ------------- | ------------------------------------------ | ---------- |
| 44     | `Evt_Vnd1`    | Grid on/off status                         | CDAB       |
| 50     | `Evt_Vnd4`    | Extended error code (firmware ≥ 3.20 only) | ABCD       |

So inside one 52-register model, the standard points are big-endian, one vendor
field is little-endian, and another vendor field is big-endian again.

There is an escape hatch, which I found in the datasheets while checking
something else and did not take: _"If the controller does not support the
Little-Endian word order, there is another linked map using the Big-Endian word
order at an offset of 0x800."_ So the whole CDAB proprietary map is mirrored
big-endian 2048 registers higher — storage control at 0xE804 rather than
0xE004. This branch keeps reading the CDAB map, because that is the one the
integration has always used and the one every user's inverter is known to
answer, and `word_order="little"` costs one keyword per field. Worth knowing it
exists if the mirrored map ever turns out to be better supported.

**Firmware that refuses registers inside a model it advertises.** Some
inverters answer a read of those same vendor event registers with an exception
even though model 103 declares a length that covers them. The old code
sidestepped this by reading them in their own small requests; the migration had
to keep doing that, and had to stop the read planner from quietly folding them
into a neighbouring block (see §3).

**Scale factors as display precision.** SunSpec's `sunssf` exponent tells you
how precise a reading actually is, and this integration feeds `abs(sf)` into
Home Assistant's `suggested_display_precision`. It is a genuinely good idea that
most integrations replace with a hardcoded constant, and it is the reason the
model here declares every scale factor as its own `sunssf` field _as well as_
referencing it from the points it scales — the library's `scale_register` hides
the exponent, and this integration needs it visible.

**Batteries have no presence bit.** An empty battery slot answers reads
normally; you know it is empty because the nameplate energy comes back as zero
or the unimplemented float. Battery identity strings carry the serial number
appended to both the manufacturer and the model, padded with arbitrary ASCII
control characters, and are not clean ASCII.

**A device scanner written against a raw socket.** `scanner.py` used to
hand-assemble Modbus/TCP frames, write them to a bare `asyncio` connection, and
compare the reply byte for byte — because setup has to distinguish three cases
(a SolarEdge inverter, some other Modbus device, nothing at that unit ID) and
the client library of the day collapsed the last two into one exception. With
typed exceptions this is now an ordinary read of nine registers: a timeout is
nobody home, any other answer is _something_, and the "SunS" marker plus a
SolarEdge manufacturer string is an inverter.

**A write mutex the coordinator busy-waits on.** SolarEdge's control registers
are flash-backed. A poll that lands immediately after a write reads the old
value back and the entity flickers, so a write sets `hub.has_write` and the
coordinator sleeps in one-second steps until it clears. There is also a
user-configurable `sleep_after_write`. The same flash-backing is why storage and
site-limit control are behind an opt-in with a warning that writing them
repeatedly may damage the inverter.

**Read-modify-write on a packed mode word.** `E_Lim_Ctl_Mode` packs three
mutually exclusive limit modes (bits 0–2) plus two independent flags (bits 10
and 11) into one register, exposed as one select and two switches. Changing any
one of them means writing all five back, and SolarEdge serves no atomic mask
write, so the read-modify-write is unavoidable — see §3.9 for where it ended up
living.

### Three latent bugs the declarative model surfaced

Rewriting hand-sliced register windows as declared addresses turned up three
real defects in the code being replaced. None are introduced by this branch;
all are fixed by it.

1. **`M_VAh_Imported_C` was decoded from the wrong registers.** The meter
   decoder concatenated `registers[55:70]`, `registers[71:104]` and friends and
   then walked the result in 32-bit steps. Register 70 is never in any slice, so
   the last apparent-energy phase was assembled from registers 69 and **71** —
   garbage on every three-phase meter.
   `tests/test_models.py::test_meter_decodes_the_last_apparent_energy_phase`
   pins the correct behaviour.
2. **Every apparent- and reactive-energy meter sensor returned `None`.**
   `MeterVAhIE` and `MetervarhIE` called `update_accum(self, value, value)`
   against a two-argument function; the `TypeError` was swallowed by a bare
   `except Exception: return None`. Both entities are disabled by default, which
   is presumably why nobody noticed.
3. **Four Advanced Power Control fields were decoded as the wrong type.**
   `PwrFrqDeratingResetTime`, `PwrFrqDeratingGradTime`, `ReactQVsVgType` and
   `PwrSoftStartTime` were listed as `uint32_fields` and then passed to
   `convert_from_registers(..., data_type=FLOAT32)`. They are now `uint32`. None
   of the four is surfaced as an entity, so this only ever affected diagnostics
   downloads.

---

## 2. What internals of modbus-connection did I have to touch?

Very little, which is the headline. Nothing was monkeypatched, no private
function was called, and no class was reached around. One thing needs more
than the documented surface; a second did until 4.4.0.

**Subclassing `StringField` to override `decode`.** `decode_string()` decodes
ASCII and strips only _trailing_ NULs, which is what the SunSpec spec describes
and not what SolarEdge sends: embedded NULs, trailing spaces, non-ASCII bytes,
and control-character padding on battery strings. Decoding as UTF-8 with
`errors="ignore"` and dropping every code point below U+0020 reproduces exactly
what the integration has always shown users, which matters because these strings
end up in device names and entity unique IDs. `StringField` is public and
`decode` is documented as the subclassing point, so this is supported — it is
just something every SunSpec library ends up writing.
(`solaredge/fields.py`, 33 lines.)

**~~Setting `register_ranges` on component _instances_.~~ Fixed in 4.4.0.** Up
to 4.3.0 every holding-space member of a `ComponentGroup` had to declare
`register_ranges` if any did, so each SunSpec component had to be given its
exact span — a value only known at runtime, assigned per instance through a
`_ranged()` helper that the documented API did not sanction (see §3.1). 4.4.0
treats a component that declares nothing as standing for the addresses it reads
by itself, which is what this library wanted all along: the helper is gone,
nothing assigns `register_ranges` at runtime any more, and only `Battery` —
whose two halves straddle a hole the device does not map — still declares a map,
as the class attribute it is documented to be.

That is the entire list. Everything else — `ComponentGroup`, `repeating_group`,
`scan()`, `SunSpecComponent`, the sunspec point helpers, the typed exception
hierarchy, `async_read_raw()`, the mock backend and its pytest plugin — was used
exactly as documented.

Worth noting on the positive side: the **SunSpec model generator produced
usable classes on the first run**. `python -m modbus_connection.model.sunspec.generate 1 103 160 203`
emitted the common block, the inverter model, the meter model, and the
multiple-MPPT model _including_ its `repeating_group` with the right stride and
the scale factors correctly left in the parent's fixed block. Models 101/102/103
turned out to share one layout, as did 201/202/203/204, so the generated output
collapsed to four classes. Almost all of `solaredge/models.py` is that output
with SolarEdge's deviations layered on; the module docstring says which three
things were changed and why.

---

## 3. What could modbus-connection do better?

Ordered by how much they cost this migration.

### 3.1 ~~`ComponentGroup` forces a choice between pooling and not over-reading~~ — fixed in 4.4.0

This was the sharpest edge by a distance, and it was a **correctness** problem,
not an ergonomics one.

Up to 4.3.0, pooling the inverter's common block and its model into one
`ComponentGroup` produced a single 121-register read spanning 40002–40122 —
because gap-based planning happily bridges the 13-register gap between the end
of one SunSpec model and the start of the next. That read covers 40113, which is
one of the registers some SolarEdge firmware refuses. On such a device the
_entire poll_ would fail, where the code being replaced only lost one optional
sensor. The test that caught it is
`tests/test_device.py::test_a_refused_optional_block_does_not_fail_the_poll`.

The fix was readable ranges, and there the group's rules bit: constraining _one_
component meant constraining _all_ of them, each with a span that for a SunSpec
component is only known at runtime — the `_ranged()` helper this branch used to
carry.

4.4.0 takes the second suggestion made here, that a component saying nothing
about the map is not _disagreeing_ with one that does: an undeclared component
now stands for the addresses it reads by itself, and the group plans with no gap
bridging at all. The planner may still join two components whose claims abut, so
the common block and the inverter model go out as one 107-register read again —
but every register in it is one a field asked for, and the 12-register gap in
front of the vendor event registers is never bridged, which is the property that
mattered. `_ranged()` is gone, and with it the runtime `register_ranges`
assignment of §2.

Two related notes are also answered: `restrict_fields()` now composes with a
group, and restricting a `SunSpecComponent` keeps `model_id`/`model_length` on
its own so `_verify_read()` cannot be broken by narrowing.

Still open, and much smaller now: **give `SunSpecComponent` its span for free.**
It is constructed with a `SunSpecModel` that carries `length`, so a component
that wants to be readable across its whole advertised model — rather than only
where its fields land — still has to say so by hand.

### 3.2 No per-request timeout

The connection has one `timeout`, set at construction. This integration wants
three: ~0.7 s while scanning 247 unit IDs (247 × 3 s of timeouts is not a setup
flow anyone will sit through), 3 s for normal polls, and ~6 s for the two
power-control blocks that some inverters are genuinely slow to answer.

Both workarounds survive the migration. The scanner constructs its own second
`ModbusConnection` with a short timeout — a second socket to the same gateway,
which is exactly what a shared-connection library exists to avoid. And the two
slow power-control blocks keep their `asyncio.timeout(6)` wrapper, now inside
the device library as `SolarEdgeOptions.slow_block_timeout`, which cancels the
request rather than letting the protocol time out cleanly.

Either `unit.read_holding_registers(addr, count, timeout=0.7)` or a scoped
`async with connection.timeout(0.7):` would remove both.

### 3.3 No request retries, and the pymodbus backend pins `retries=0`

`modbus.retries` was a documented YAML option here; there is no equivalent, so
the migration logs a warning telling users to delete it. `reconnect_delay` and
`reconnect_delay_max` went the same way (connect-on-demand replaces them, which
is a genuine improvement — that part is fine).

A retry policy on the connection — attempt count, backoff, and which exception
types are worth retrying — would let device libraries stop hand-rolling one. The
coordinator here still has a bespoke exponential-backoff retry loop wrapped
around the whole poll, which is a much blunter instrument than retrying the one
request that timed out.

### 3.4 `word_order` should be settable per component

`register_space` is a `Component` class attribute precisely so a whole
sub-system can say "I live in input registers" once. `word_order` is per-field
only, so all ~90 fields of SolarEdge's proprietary blocks repeat
`word_order="little"`, via a local `_f32()` helper that exists only to apply it.
A `word_order = "little"` class attribute, defaulting the fields that don't
override it, would make `solaredge/proprietary.py` noticeably clearer — and the
per-field override still handles the genuinely mixed case in §1.

### 3.5 `float32(nan=...)` wants a raw sentinel it doesn't use

`FloatField.decode` tests `if self.nan is not None and math.isnan(value)`, so
the _value_ passed to `nan=` is irrelevant — it is a flag. But the parameter is
typed and documented as a raw sentinel, so `solaredge/proprietary.py` passes
`nan=0x7FC00000` and a reader reasonably assumes that exact bit pattern is being
matched. `nan=True`, or a separate `nan_is_none=True`, would say what it means.
(`sunspec.float32` gets this right by doing it automatically.)

### 3.6 `StringField` is stricter than real devices

Related to §2. ASCII-only with a trailing-NUL strip is spec-correct and wrong
for essentially every inverter. Options on `StringField` — `encoding=`,
`errors=`, `strip_control=`, `strip=` — would remove a subclass that this
library, and by the survey's account most SunSpec libraries, all end up writing
independently.

### 3.7 ~~`scan()` throws away chain order, which is meaningful~~ — fixed in 4.4.0

`SunSpecModels` was a dict keyed by model ID with a `first()` helper. But a
SunSpec chain is _ordered_, and on SolarEdge position is load-bearing: meter
_n_'s identity block is a model 1, and the meter model that belongs to it is the
next model in the chain, not "the nth model 203". Reconstructing that meant
flattening every list and re-sorting by address — `_chain()` and `_at()` in
`solaredge/device.py`.

4.4.0 adds `SunSpecModels.chain`, `models.at(address)` and `SunSpecModel.span`.
Both helpers are gone, and the meter-slot shift now reads `mppt_model.span`
rather than restating "length plus its two header registers" as `length + 2`.

### 3.8 `scan()` is all-or-nothing on a malformed chain

A truncated or corrupt chain raises, and everything behind the break is lost
with no way to resume the walk from a known address. This integration is
insulated because SolarEdge's chains are well-formed in practice, but the
fallback it _would_ want — "scan failed, probe my known address table instead" —
has no supported shape. `scan(unit, base, on_error="stop")` returning what it
found, or a public `read_model_header(unit, address)`, would give libraries
somewhere to land.

### 3.9 ~~No writable bitfield~~ — fixed in 4.4.0

`E_Lim_Ctl_Mode` is one register holding five independent settings, exposed as
three entities — a select over the three mutually exclusive mode bits, and two
switches over the independent flags. Changing any of them means writing all
five back, and up to 4.3.0 there was no field-level way to express that.

`ModbusUnit.mask_write_register` (FC 0x16) would make it atomic at the device,
but **SolarEdge does not implement it**: its _SunSpec Implementation Technical
Note_ (v3.2, June 2025), Appendix A, documents the main functions as `0x03`,
`0x06` and `0x10` only, and this integration has never issued anything but
`0x03` and `0x10`. So the read-modify-write is unavoidable here.

What that changes is _where_ it belongs. Doing it in the entities means writing
back the value from the last poll, up to a full scan interval stale — anything
that touched another bit meanwhile is silently reverted. This branch first put
it on the component, as a `write_mode_bits(mask, value)` helper; 4.4.0's `bit()`
and `bits()` are that helper, and the block now simply declares what each
setting owns:

```python
limit_mode = bits(0, 0, 3, writable=True)          # bits 0-2, one selection
external_production = bit(0, 10, writable=True)    # bit 10
negative_limit = bit(0, 11, writable=True)         # bit 11
```

A write to any of them re-reads the register and merges, which narrows the
window from one poll interval to one round trip — as tight as this device
allows. Both halves the datasheet asks for are there. Eleven of the register's
sixteen bits are **Reserved**, so the write is masked and leaves them alone. And
bits 0-2 carry "Only single selection is allowed", so `bits(0, 0, 3)` covers the
whole run and changing the selection clears two and sets one _in one write_,
never passing through a state the device forbids — which three separate
single-bit fields would have done. The select and both switches now write a
field by name and hold no bit arithmetic at all
([home-assistant-libs/modbus-connection#150](https://github.com/home-assistant-libs/modbus-connection/issues/150)).

### 3.10 Smaller things

- **The generator names the same quantity two ways.** Model 103's `VAr` becomes
  `v_ar` and model 203's becomes `var`. A device library that treats inverters
  and meters uniformly — as the entity layer here does — has to rename one of
  them by hand (`solaredge/models.py` renames the inverter's).
- **SunSpec numeric helpers are typed `NumberField[float]` but return `int`**
  when the scale factor is 0, because `_scale()` short-circuits `factor == 1.0`
  to keep integers integral. That short-circuit is the right behaviour; the type
  just doesn't describe it. It means `model_id` is statically a `float`.
- **The device-library-must-not-import-Home-Assistant rule needs a note for
  custom integrations.** The documented layering assumes the library is a
  separate PyPI package. A HACS integration that ships it inside the component
  (`custom_components/<domain>/<lib>/`) can only import it standalone by putting
  the _component_ directory on `sys.path`, because importing it as
  `<domain>.<lib>` executes the integration's `__init__` and pulls in Home
  Assistant. `tests/conftest.py` here does exactly that, and it is worth a
  paragraph on the integration page — the alternative (tests that import Home
  Assistant to test a register map) is what the split exists to avoid.

---

## Things that fit without comment

Worth recording because they are the bulk of the migration and none of them
needed a workaround:

- `ModbusConnection` + `for_unit()` for three inverters on one socket.
- Connect-on-demand and automatic reconnection replaced a hand-written
  connect lock, a client-recreation path, and two YAML reconnect options.
- The typed exception hierarchy replaced an `inspect.signature()` check against
  two pymodbus versions and eight bespoke exception classes.
  `IllegalDataAddressError` in particular is what makes "this firmware doesn't
  serve that block" a one-line `except`.
- `repeating_group` sized model 160's MPPT modules from the device's own `N`
  point, replacing 120 lines of offset arithmetic — and it correctly kept each
  module's scale factors pointing at the parent's fixed block.
- Sentinel decoding (`0x8000`, `0xFFFF`, accumulator `0`, NaN) and the
  `sunssf` -10..10 spec-range check between them deleted several hundred lines of
  `== SunSpecNotImpl.INT16 or ... not in SUNSPEC_SF_RANGE` from the entity layer.
- `async_read_raw()` replaced a bespoke diagnostics formatter, and the dump
  replays into the mock with `load_raw()`, so a register dump attached to an
  issue can back a regression test.
- `SunSpecMapShiftError` gave a name to a failure mode the integration had no
  handling for at all; the coordinator now reloads the entry.
- The mock backend and its pytest plugin are why a rewrite this size is
  checkable. `read_events` in particular made the §3.1 over-reading bug visible
  as an assertion about block boundaries rather than a field report from someone
  with the wrong firmware.
- `bit()` and `bits()` (4.4.0) put the packed-register read-modify-write in the
  library, where it belongs; see §3.9.

## What the 4.3.0 → 4.4.0 bump changed here

- `_ranged()` in `solaredge/device.py` is gone: an undeclared component now
  stands for what it reads by itself, so nothing assigns `register_ranges` at
  runtime any more (§2, §3.1). That also removes a hazard the new planner
  introduced — a readable range derived from a model's advertised `length` is
  now checked against the fields inside it, and a device reporting a length
  short of its own last point would have failed the plan outright.
- `_chain()` and `_at()` are gone in favour of `SunSpecModels.chain` /
  `.at()`, and the meter-slot arithmetic uses `SunSpecModel.span` (§3.7).
- `SiteLimit.write_mode_bits()` is gone in favour of `bit()` / `bits()` fields;
  the select and the two switches write a named field (§3.9).
- Nothing else needed changing: the repo never imported the removed
  `ModbusParams` alias, already raised and caught the typed exception classes
  rather than branching on `.exception_code`, and already used `disconnect()`,
  `nan=`, `declared_fields`, `read_events` and `fail_read` with typed errors.
