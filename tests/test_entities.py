"""Build every entity from a seeded device and read all of it.

This is the check the migration most needed: the entity layer names model
fields as strings in a dozen places, and a typo in one of them is invisible
until a user's inverter is polled. Here every entity the platforms create is
constructed against a mock device and every property that reads the model is
evaluated, so a name that does not exist fails the test.

Home Assistant is imported, but not run: the entities are plain objects here,
with a stub coordinator standing in for the one that would drive them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.sensor import SensorStateClass  # noqa: E402
from solaredge_modbus_multi import (  # noqa: E402
    binary_sensor,
    button,
    number,
    select,
    sensor,
    switch,
)
from solaredge_modbus_multi.const import DOMAIN  # noqa: E402
from solaredge_modbus_multi.hub import (  # noqa: E402
    SolarEdgeBattery,
    SolarEdgeInverter,
    SolarEdgeMeter,
)
from solaredge_modbus_multi.solaredge import (  # noqa: E402
    SolarEdgeDevice,
    SolarEdgeOptions,
)

from .fixtures import seed_battery, seed_inverter, seed_meter  # noqa: E402

ENTRY_ID = "test_entry"

# The properties that read the model. Reading all of them on every entity is
# what turns a bad field name into a test failure.
_READ_PROPERTIES = (
    "unique_id",
    "name",
    "available",
    "entity_registry_enabled_default",
    "native_value",
    "native_unit_of_measurement",
    "native_min_value",
    "native_max_value",
    "suggested_display_precision",
    "extra_state_attributes",
    "current_option",
    "options",
    "is_on",
    "icon",
    "device_info",
)


class StubCoordinator:
    """The parts of a DataUpdateCoordinator an entity touches when read."""

    last_update_success = True
    last_update_success_time = None

    def async_add_listener(self, update_callback, context=None):
        return lambda: None

    async def async_request_refresh(self):
        """Entities ask for a refresh after a write; nothing to do here."""


class StubConfigEntry:
    entry_id = ENTRY_ID
    data = {"name": "SolarEdge"}
    options = {}


class StubHub:
    """Enough of the hub for the wrappers and the platform setup functions."""

    hub_id = "solaredge"
    online = True
    allow_battery_energy_reset = False
    battery_rating_adjust = 1.0
    battery_energy_reset_cycles = 0

    def __init__(self, *, storage=False, site_limit=False, extras=False):
        self.inverters = []
        self.meters = []
        self.batteries = []
        self.evses = []
        self.option_storage_control = storage
        self.option_site_limit_control = site_limit
        self.option_detect_extras = extras
        self.writes = []

    async def async_write(self, component, field, value):
        self.writes.append((type(component).__name__, field, value))
        await component.write(field, value)


async def _build_hub(unit, **options) -> StubHub:
    """Discover a seeded unit and wrap it the way the real hub does."""
    hub = StubHub(
        storage=options.get("storage_control", False),
        site_limit=options.get("site_limit_control", False),
        extras=options.get("detect_extras", False),
    )
    device = SolarEdgeDevice(unit, 1, SolarEdgeOptions(**options))
    await device.async_setup()
    await device.async_add_batteries()

    # Polled through the wrapper, and after it is built, exactly as the hub
    # does it: the report the wrapper keeps is what the entities read.
    inverter = SolarEdgeInverter(1, hub, device)
    await inverter.async_update()
    hub.inverters.append(inverter)
    for meter in device.meters:
        hub.meters.append(SolarEdgeMeter(hub, inverter, meter))
    for battery in device.batteries:
        hub.batteries.append(SolarEdgeBattery(hub, inverter, battery))
    return hub


async def _entities_for(hub, *modules):
    """Run each platform's setup and collect the entities it created."""
    hass = type("Hass", (), {})()
    hass.data = {DOMAIN: {ENTRY_ID: {"hub": hub, "coordinator": StubCoordinator()}}}

    created = []
    for module in modules:
        await module.async_setup_entry(hass, StubConfigEntry(), created.extend)
    return created


def _process_totals(entities) -> None:
    """Push a finished poll into the totals, as a coordinator update would.

    A total holds its value in ``_attr_native_value`` rather than reading the
    model on demand, so nothing reaches it until the coordinator fires. These
    entities are never added to Home Assistant, so the call is made here.
    """
    for entity in entities:
        if isinstance(entity, sensor.SolarEdgeTotalBase):
            entity._process_data()


def _read_everything(entity) -> dict:
    """Evaluate every model-reading property, letting failures propagate."""
    values = {}
    for name in _READ_PROPERTIES:
        if hasattr(type(entity), name) or hasattr(entity, name):
            values[name] = getattr(entity, name)
    return values


ALL_PLATFORMS = (sensor, number, select, switch, button, binary_sensor)


async def test_every_entity_reads_without_error(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit, mppt_units=2)
    seed_meter(mock_modbus_unit, meter_id=1, mppt_units=2)
    seed_battery(mock_modbus_unit, battery_id=1)

    hub = await _build_hub(
        mock_modbus_unit,
        detect_meters=True,
        detect_batteries=True,
    )
    entities = await _entities_for(hub, *ALL_PLATFORMS)

    assert len(entities) > 100
    unique_ids = set()
    for entity in entities:
        _read_everything(entity)
        unique_ids.add(entity.unique_id)

    assert len(unique_ids) == len(entities)  # no entity shadows another


async def test_control_entities_read_without_error(mock_modbus_unit) -> None:
    """The same, with every optional control block present."""
    from .fixtures import f32, u32

    seed_inverter(mock_modbus_unit)
    seed_battery(mock_modbus_unit, battery_id=1)

    holding = mock_modbus_unit.holding
    holding[57344] = [0b0000_0100_0000_0001, 0]  # limit mode + limit control
    holding[57346] = f32(5000.0)  # site limit
    holding[57362] = f32(7000.0)  # external production max
    holding[57348] = [4, 2]  # storage control mode 4 (remote), AC policy 2
    holding[57350] = f32(1000.0)
    holding[57352] = f32(10.0)
    holding[57354] = 1
    holding[57355] = u32(300, little=True)  # command timeout
    holding[57357] = 1
    holding[57358] = f32(5000.0)
    holding[57360] = f32(5000.0)
    holding[61440] = [0x0, 75]  # RRCR, power limit
    holding[61442] = f32(0.9)  # cos phi
    holding[61696] = [0, 0]  # commit / restore result registers
    holding[61700] = u32(4, little=True)  # reactive power config
    holding[61760] = f32(100.0)  # power reduce
    holding[61762] = u32(1, little=True)  # advanced power control enabled
    holding[61838] = f32(32.0)  # max current

    hub = await _build_hub(
        mock_modbus_unit,
        detect_batteries=True,
        detect_extras=True,
        storage_control=True,
        site_limit_control=True,
    )
    entities = await _entities_for(hub, *ALL_PLATFORMS)

    by_id = {}
    for entity in entities:
        _read_everything(entity)
        by_id[entity.unique_id] = entity

    uid = hub.inverters[0].uid_base
    assert by_id[f"{uid}_site_limit"].native_value == 5000
    assert by_id[f"{uid}_active_power_limit_set"].native_value == 75
    assert by_id[f"{uid}_cosphi_set"].native_value == pytest.approx(0.9)
    assert by_id[f"{uid}_max_current"].native_value == 32
    assert by_id[f"{uid}_adv_pwr_ctrl"].is_on is True
    assert by_id[f"{uid}_storage_backup_reserve"].native_value == 10


async def test_writing_a_number_reaches_the_register(mock_modbus_unit) -> None:
    from .fixtures import f32, u32

    seed_inverter(mock_modbus_unit)
    seed_battery(mock_modbus_unit, battery_id=1)
    mock_modbus_unit.holding[57348] = [4, 2]
    mock_modbus_unit.holding[57350] = f32(1000.0)
    mock_modbus_unit.holding[57352] = f32(10.0)
    mock_modbus_unit.holding[57354] = 1
    mock_modbus_unit.holding[57355] = u32(300, little=True)
    mock_modbus_unit.holding[57357] = 1
    mock_modbus_unit.holding[57358] = f32(5000.0)
    mock_modbus_unit.holding[57360] = f32(5000.0)

    hub = await _build_hub(
        mock_modbus_unit, detect_batteries=True, storage_control=True
    )
    entities = await _entities_for(hub, number)
    uid = hub.inverters[0].uid_base
    charge_limit = next(
        e for e in entities if e.unique_id == f"{uid}_storage_charge_limit"
    )

    await hub.async_write(charge_limit.block, charge_limit._field, 2500.0)

    assert await mock_modbus_unit.read_holding_registers(57358, 2) == f32(2500.0)


async def test_switching_a_mode_bit_preserves_the_others(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)
    # Bit 0 (limit by production) and bit 11 (negative site limit) are set.
    mock_modbus_unit.holding[57344] = [0b0000_1000_0000_0001, 0]

    hub = await _build_hub(mock_modbus_unit, site_limit_control=True)
    entities = await _entities_for(hub, switch)
    uid = hub.inverters[0].uid_base
    external = next(e for e in entities if e.unique_id == f"{uid}_external_production")

    assert external.is_on is False
    await external._async_set_bit(True)

    written = (await mock_modbus_unit.read_holding_registers(57344, 1))[0]
    assert written == 0b0000_1100_0000_0001  # bit 10 added, nothing else changed


async def test_diagnostics_carry_decoded_values_and_raw_registers(
    mock_modbus_unit,
) -> None:
    from solaredge_modbus_multi import diagnostics

    seed_inverter(mock_modbus_unit, mppt_units=2)
    seed_meter(mock_modbus_unit, meter_id=1, mppt_units=2)
    seed_battery(mock_modbus_unit, battery_id=1)

    hub = await _build_hub(mock_modbus_unit, detect_meters=True, detect_batteries=True)
    hass = type("Hass", (), {})()
    hass.data = {DOMAIN: {ENTRY_ID: {"hub": hub, "coordinator": StubCoordinator()}}}

    entry = StubConfigEntry()
    entry.as_dict = lambda: {"entry_id": ENTRY_ID, "data": {"host": "192.0.2.10"}}
    hass.data[DOMAIN]["yaml"] = {}

    data = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    inverter = data["inverter_unit_id_1"]
    assert inverter["model"]["a"] == pytest.approx(12.34)
    assert inverter["mmppt"]["n"] == 2
    assert len(inverter["mmppt_modules"]) == 2
    assert data["meter_id_1"]["model"]["w"] == -1500
    assert data["battery_id_1"]["model"]["soe"] == pytest.approx(75.0)

    # The raw map is what makes a bug report replayable: it loads straight back
    # into the mock backend with load_raw().
    registers = inverter["registers"]["holding"]
    assert registers["40002"] == 1  # the common model header
    assert inverter["optional_blocks"]["grid_status"] is True

    # Which device stopped answering is the other half of a bug report.
    assert "meter_1" in inverter["last_poll"]["updated"]
    assert inverter["last_poll"]["failed"] == {}

    # Serial numbers are redacted; the values that identify a fault are not.
    assert data["meter_id_1"]["common"]["sn"] == "**REDACTED**"


async def test_a_mode_bit_write_rereads_first(mock_modbus_unit) -> None:
    """A change made since the last poll survives flipping another bit.

    The register packs five settings and SolarEdge serves no atomic mask
    write, so setting one is a read-modify-write. Declaring each setting as
    the bits it owns puts that read in the write itself, rather than writing
    back the value from the last poll — which is the difference between losing
    a concurrent change and keeping it.
    """
    seed_inverter(mock_modbus_unit)
    mock_modbus_unit.holding[57344] = [0b0000_1000_0000_0000, 0]  # bit 11 set

    hub = await _build_hub(mock_modbus_unit, site_limit_control=True)
    entities = await _entities_for(hub, switch)
    uid = hub.inverters[0].uid_base
    external = next(e for e in entities if e.unique_id == f"{uid}_external_production")

    # Something else — the installer app, the inverter itself — sets bit 0
    # after our last poll. Home Assistant's cached copy does not have it.
    mock_modbus_unit.holding[57344] = 0b0000_1000_0000_0001
    assert hub.inverters[0].site_limit.limit_mode == 0
    assert hub.inverters[0].site_limit.negative_limit is True
    assert hub.inverters[0].site_limit.external_production is False

    await external._async_set_bit(True)

    written = (await mock_modbus_unit.read_holding_registers(57344, 1))[0]
    assert written == 0b0000_1100_0000_0001  # bit 10 added, bits 0 and 11 kept


async def test_selecting_a_limit_mode_clears_only_its_own_group(
    mock_modbus_unit,
) -> None:
    seed_inverter(mock_modbus_unit)
    # Limit mode bit 1, plus both independent flags.
    mock_modbus_unit.holding[57344] = [0b0000_1100_0000_0010, 0]

    hub = await _build_hub(mock_modbus_unit, site_limit_control=True)
    entities = await _entities_for(hub, select)
    uid = hub.inverters[0].uid_base
    mode = next(e for e in entities if e.unique_id == f"{uid}_limit_control_mode")

    await mode.async_select_option("Production Control")  # bit 2

    written = (await mock_modbus_unit.read_holding_registers(57344, 1))[0]
    # Bit 1 replaced by bit 2; bits 10 and 11 untouched.
    assert written == 0b0000_1100_0000_0100


async def test_a_failed_meter_takes_only_its_own_entities_unavailable(
    mock_modbus_unit,
) -> None:
    """The bug this migration was asked to fix, seen from the entity layer.

    One pooled read meant a meter that went quiet failed the coordinator and
    every entity on the hub went unavailable. The poll now contains it, and the
    report says which device it was.
    """
    from modbus_connection import ModbusTimeoutError

    seed_inverter(mock_modbus_unit)
    seed_meter(mock_modbus_unit, meter_id=1)
    seed_battery(mock_modbus_unit, battery_id=1)

    hub = await _build_hub(mock_modbus_unit, detect_meters=True, detect_batteries=True)
    entities = await _entities_for(hub, *ALL_PLATFORMS)
    meter_uid = hub.meters[0].uid_base
    meter_entities = [e for e in entities if e.unique_id.startswith(f"{meter_uid}_")]

    before = {e.unique_id: e.available for e in entities}
    assert len(meter_entities) > 50
    assert all(before[e.unique_id] for e in meter_entities)

    mock_modbus_unit.fail_read(40121, ModbusTimeoutError("slow meter"))
    await hub.inverters[0].async_update()

    after = {e.unique_id: e.available for e in entities}
    lost = {uid for uid, was in before.items() if was and not after[uid]}
    # Everything the failure took belonged to the meter, and it took all of it
    # bar the Last Update diagnostic, which reports the coordinator rather than
    # the device, and the energy totals, which hold their last value so that a
    # device dropping out does not gap long-term statistics.
    kept = {f"{meter_uid}_last_update_timestamp"} | {
        e.unique_id for e in meter_entities if getattr(e, "is_total", False)
    }
    assert lost == {e.unique_id for e in meter_entities} - kept
    assert hub.inverters[0].online and hub.batteries[0].online
    assert not hub.meters[0].online


async def test_energy_totals_survive_an_inverter_that_stopped_answering(
    mock_modbus_unit,
) -> None:
    """The nightly powerdown, which is the case that damages statistics.

    A SolarEdge inverter powers down every night and takes the link with it,
    so nothing answers and the coordinator itself fails — not just one device.
    Instantaneous readings go unavailable, but the lifetime counters have to
    hold their last value: an unavailable total-increasing sensor puts a gap
    in long-term statistics and the energy dashboard.
    """
    seed_inverter(mock_modbus_unit)
    seed_meter(mock_modbus_unit, meter_id=1)

    hub = await _build_hub(mock_modbus_unit, detect_meters=True)
    entities = await _entities_for(hub, sensor)
    _process_totals(entities)

    totals = [e for e in entities if e.state_class == SensorStateClass.TOTAL_INCREASING]
    live = [e for e in entities if e.state_class == SensorStateClass.MEASUREMENT]
    assert totals and live
    before = {e.unique_id: e.native_value for e in totals}

    # Everything is gone: no device refreshed and the coordinator update failed.
    hub.online = False
    entities[0].coordinator.last_update_success = False

    assert not any(e.available for e in live)
    assert all(e.available for e in totals)
    assert {e.unique_id: e.native_value for e in totals} == before


async def test_a_total_holds_its_last_value_when_a_good_poll_gates_it(
    mock_modbus_unit,
) -> None:
    """A successful poll can gap statistics too, by publishing unknown.

    The device keeps answering but the value is one the sensor refuses to
    publish: the inverter's lifetime counter jumps backwards, and the battery
    reports zero from standby. Both gates stay — republishing either would
    read as a counter reset — but the sensors now hold their last total
    instead of going unknown, which breaks statistics just as badly.
    """
    from .fixtures import u32, u64

    seed_inverter(mock_modbus_unit)
    seed_battery(mock_modbus_unit, battery_id=1)

    hub = await _build_hub(mock_modbus_unit, detect_batteries=True)
    entities = await _entities_for(hub, sensor)
    _process_totals(entities)
    by_id = {e.unique_id: e for e in entities}

    lifetime = by_id[f"{hub.inverters[0].uid_base}_ac_energy_kwh"]
    export = by_id[f"{hub.batteries[0].uid_base}_energy_export"]
    assert lifetime.native_value == 12345678
    assert export.native_value == 4_000_000

    mock_modbus_unit.holding[40093] = u32(9_000)  # inverter WH, gone backwards
    mock_modbus_unit.holding[57718] = u64(0, little=True)  # battery in standby
    await hub.inverters[0].async_update()
    _process_totals(entities)

    assert lifetime.available and lifetime.native_value == 12345678
    assert export.available and export.native_value == 4_000_000


async def test_an_accepted_battery_reset_publishes_the_new_total(
    mock_modbus_unit,
) -> None:
    """A replaced battery starts counting again, without a gap on the way.

    The reset is only accepted once the counter has stayed low for the
    configured number of polls. Neither the waiting nor the acceptance may
    publish unknown, which would break statistics as badly as unavailable.
    """
    from .fixtures import u64

    seed_inverter(mock_modbus_unit)
    seed_battery(mock_modbus_unit, battery_id=1)

    hub = await _build_hub(mock_modbus_unit, detect_batteries=True)
    hub.allow_battery_energy_reset = True
    hub.battery_energy_reset_cycles = 1
    entities = await _entities_for(hub, sensor)
    _process_totals(entities)

    export = next(
        e
        for e in entities
        if e.unique_id == f"{hub.batteries[0].uid_base}_energy_export"
    )
    assert export.native_value == 4_000_000

    mock_modbus_unit.holding[57718] = u64(1_000, little=True)  # a new battery
    await hub.inverters[0].async_update()
    _process_totals(entities)
    assert export.native_value == 4_000_000  # one cycle short of a reset

    await hub.inverters[0].async_update()
    _process_totals(entities)
    assert export.native_value == 1_000


async def test_writing_a_control_re_reads_its_block_on_the_next_poll(
    mock_modbus_unit,
) -> None:
    seed_inverter(mock_modbus_unit)
    hub = await _build_hub(
        mock_modbus_unit, detect_extras=True, slow_block_interval=600
    )
    entities = await _entities_for(hub, number)
    uid = hub.inverters[0].uid_base
    reduce = next(e for e in entities if e.unique_id == f"{uid}_power_reduce")

    # Without the write the block would sit out this poll; the write puts it
    # back in, so the entity shows what the inverter took rather than what it
    # was asked for.
    mark = len(mock_modbus_unit.read_events)
    await hub.inverters[0].async_write(reduce.block, reduce._field, 55.0)
    await hub.inverters[0].async_update()

    covered = [
        event
        for event in mock_modbus_unit.read_events[mark:]
        if event.address <= 61696 < event.address + event.count
    ]
    assert len(covered) == 1
    assert reduce.native_value == pytest.approx(55.0)
