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

    async def async_write_mode_bits(self, component, mask, value):
        self.writes.append((type(component).__name__, f"bits {mask:#06x}", value))
        await component.write_mode_bits(mask, value)


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
    await device.async_update()

    inverter = SolarEdgeInverter(1, hub, device)
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

    # Serial numbers are redacted; the values that identify a fault are not.
    assert data["meter_id_1"]["common"]["sn"] == "**REDACTED**"


async def test_a_mode_bit_write_rereads_first(mock_modbus_unit) -> None:
    """A change made since the last poll survives flipping another bit.

    The register packs five settings and SolarEdge serves no atomic mask
    write, so setting one is a read-modify-write. Doing it on the component
    means re-reading here rather than writing back the value from the last
    poll — which is the difference between losing a concurrent change and
    keeping it.
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
    assert hub.inverters[0].site_limit.e_lim_ctl_mode == 0b0000_1000_0000_0000

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
