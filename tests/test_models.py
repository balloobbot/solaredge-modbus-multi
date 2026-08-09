"""Decode tests for the register map itself.

Every expected value here is worked out from the SunSpec definition and
SolarEdge's application notes by hand, so a passing test says the model agrees
with the spec rather than with itself.
"""

from __future__ import annotations

import pytest
from modbus_connection.model.sunspec import SunSpecModel
from solaredge.models import Common, Inverter, Meter, OperatingState
from solaredge.proprietary import Battery, StorageControl

from .fixtures import f32, i16, seed_battery, seed_inverter, seed_meter, u32


def _model(address: int, model_id: int, length: int) -> SunSpecModel:
    return SunSpecModel(model_id=model_id, address=address, length=length)


async def test_common_block_decodes_identity(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit, serial="ABC12345", model="SE7600A-US")

    common = Common(mock_modbus_unit, _model(40002, 1, 65))
    await common.async_update()

    assert common.mn == "SolarEdge"
    assert common.md == "SE7600A-US"
    assert common.vr == "0004.0019.0033"
    assert common.sn == "ABC12345"
    assert common.da == 1


async def test_inverter_applies_scale_factor_registers(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)

    inverter = Inverter(mock_modbus_unit, _model(40069, 103, 50))
    await inverter.async_update()

    assert inverter.a == pytest.approx(12.34)  # 1234 * 10**-2
    assert inverter.a_sf == -2
    assert inverter.ph_vph_a == pytest.approx(230.1)  # 2301 * 10**-1
    assert inverter.hz == pytest.approx(49.99)  # 4999 * 10**-2
    assert inverter.pf == pytest.approx(99.85)
    assert inverter.dcv == pytest.approx(380.5)
    assert inverter.tmp_snk == pytest.approx(45.2)


async def test_inverter_unscaled_points_stay_integers(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)

    inverter = Inverter(mock_modbus_unit, _model(40069, 103, 50))
    await inverter.async_update()

    # A scale factor of 0 must not turn a watt count into a float: entities
    # derive their display precision from the exponent.
    assert inverter.w == 5432
    assert inverter.w_sf == 0
    assert inverter.wh == 12345678
    assert inverter.st is OperatingState.MPPT
    assert inverter.st_vnd == 0


async def test_inverter_unimplemented_points_read_as_none(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)

    inverter = Inverter(mock_modbus_unit, _model(40069, 103, 50))
    await inverter.async_update()

    # The three temperature sensors this inverter does not have send 0x8000.
    assert inverter.tmp_cab is None
    assert inverter.tmp_trns is None
    assert inverter.tmp_ot is None


async def test_inverter_out_of_spec_scale_factor_reads_as_none(
    mock_modbus_unit,
) -> None:
    seed_inverter(mock_modbus_unit)
    # SolarEdge inverters have been seen reporting a garbage exponent around
    # their sleep/wake transition. Scaling by 10**-24 would look like a real
    # (tiny) reading, so the point has to read as unknown instead.
    mock_modbus_unit.holding[40069 + 6] = i16(-24)

    inverter = Inverter(mock_modbus_unit, _model(40069, 103, 50))
    await inverter.async_update()

    assert inverter.a is None
    assert inverter.a_sf == -24  # the raw exponent is still readable


async def test_inverter_reads_its_model_in_one_block(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)
    inverter = Inverter(mock_modbus_unit, _model(40069, 103, 50))

    await inverter.async_update()

    blocks = mock_modbus_unit.read_events
    assert len(blocks) == 1
    assert blocks[0].address == 40069
    assert blocks[0].count == 40  # header through StVnd, nothing past it


async def test_meter_decodes_the_last_apparent_energy_phase(
    mock_modbus_unit,
) -> None:
    seed_meter(mock_modbus_unit, meter_id=1)
    # Phase C of imported apparent energy sits at offset 69-70, immediately
    # before the scale factor at 71. The hand-sliced decoder this migration
    # replaced read registers 69 and *71*, skipping 70, so this point came out
    # wrong on every three-phase meter.
    mock_modbus_unit.holding[40188 + 69] = u32(0x0102_0304)

    meter = Meter(mock_modbus_unit, _model(40188, 203, 105))
    await meter.async_update()

    assert meter.tot_v_ah_imp_ph_c == 0x0102_0304


async def test_meter_scales_and_signs(mock_modbus_unit) -> None:
    seed_meter(mock_modbus_unit, meter_id=1)

    meter = Meter(mock_modbus_unit, _model(40188, 203, 105))
    await meter.async_update()

    assert meter.a == pytest.approx(3.0)  # 300 * 10**-2
    assert meter.w == -1500  # exporting, scale factor 0
    assert meter.pf == pytest.approx(-98.0)
    assert meter.tot_wh_exp == 1_000_000
    assert meter.tot_wh_imp == 1_004_000


async def test_battery_decodes_cdab_words(mock_modbus_unit) -> None:
    base = seed_battery(mock_modbus_unit, battery_id=1, rated_energy=9800.0)

    battery = Battery(mock_modbus_unit, base_offset=base)
    await battery.async_update()

    assert battery.manufacturer == "SolarEdge"
    assert battery.serial == "BATT0001"
    assert battery.rated_energy == pytest.approx(9800.0)
    assert battery.dc_voltage == pytest.approx(52.4)
    assert battery.dc_current == pytest.approx(-12.5)
    assert battery.soe == pytest.approx(75.0)
    # 64-bit counters are word-swapped the same way the 32-bit ones are.
    assert battery.export_energy_wh == 4_000_000
    assert battery.import_energy_wh == 4_500_000
    assert battery.status == 4


async def test_battery_unimplemented_float_reads_as_none(mock_modbus_unit) -> None:
    base = seed_battery(mock_modbus_unit, battery_id=1)
    mock_modbus_unit.holding[base + 130] = [0x0000, 0x7FC0]  # NaN, CDAB

    battery = Battery(mock_modbus_unit, base_offset=base)
    await battery.async_update()

    assert battery.soh is None


async def test_battery_skips_the_hole_in_its_block(mock_modbus_unit) -> None:
    base = seed_battery(mock_modbus_unit, battery_id=1)
    battery = Battery(mock_modbus_unit, base_offset=base)

    await battery.async_update()

    blocks = mock_modbus_unit.read_events
    # SolarEdge leaves 32 unmapped registers in the middle of the block, and
    # the whole thing is wider than a single Modbus request anyway.
    assert len(blocks) == 2
    assert all(block.count <= 125 for block in blocks)
    covered = {addr for b in blocks for addr in range(b.address, b.address + b.count)}
    assert not covered & set(range(base + 76, base + 108))


async def test_storage_control_round_trips_a_cdab_float(mock_modbus_unit) -> None:
    mock_modbus_unit.holding[57348] = [3, 1]
    mock_modbus_unit.holding[57350] = f32(100.0)
    mock_modbus_unit.holding[57352] = f32(10.0)
    mock_modbus_unit.holding[57354] = 1
    mock_modbus_unit.holding[57355] = [0, 0]
    mock_modbus_unit.holding[57357] = 0
    mock_modbus_unit.holding[57358] = f32(5000.0)
    mock_modbus_unit.holding[57360] = f32(5000.0)

    storage = StorageControl(mock_modbus_unit, base_offset=57348)
    await storage.async_update()
    assert storage.control_mode == 3
    assert storage.backup_reserve == pytest.approx(10.0)
    assert storage.charge_limit == pytest.approx(5000.0)

    await storage.write("charge_limit", 3300.0)

    assert await mock_modbus_unit.read_holding_registers(57358, 2) == f32(3300.0)
    await storage.async_update()
    assert storage.charge_limit == pytest.approx(3300.0)
