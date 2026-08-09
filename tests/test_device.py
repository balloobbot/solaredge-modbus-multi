"""Discovery and polling tests for the device object."""

from __future__ import annotations

import pytest
from modbus_connection import IllegalDataAddressError, ModbusTimeoutError
from solaredge import DeviceInvalid, SolarEdgeDevice, SolarEdgeOptions

from .fixtures import seed_battery, seed_inverter, seed_meter


async def test_setup_rejects_a_unit_with_no_sunspec_marker(mock_modbus_unit) -> None:
    device = SolarEdgeDevice(mock_modbus_unit, 1)

    with pytest.raises(DeviceInvalid):
        await device.async_setup()


async def test_setup_rejects_a_sunspec_device_with_no_inverter_model(
    mock_modbus_unit,
) -> None:
    from .fixtures import END_OF_CHAIN, seed_common

    mock_modbus_unit.holding[40000] = [0x5375, 0x6E53]
    after = seed_common(mock_modbus_unit.holding, 40002)
    mock_modbus_unit.holding[after] = END_OF_CHAIN

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    with pytest.raises(DeviceInvalid):
        await device.async_setup()


@pytest.mark.parametrize("model_id", [101, 102, 103])
async def test_setup_accepts_every_inverter_model(mock_modbus_unit, model_id) -> None:
    seed_inverter(mock_modbus_unit, model_id=model_id)

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    await device.async_update()

    assert device.inverter is not None
    assert device.inverter.model_id == model_id
    assert device.inverter.a == pytest.approx(12.34)


async def test_meters_are_found_at_their_slots(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)
    seed_meter(mock_modbus_unit, meter_id=1, model_id=203)

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    await device.async_update()

    assert [meter.meter_id for meter in device.meters] == [1]
    assert device.meters[0].model_id == 203
    assert device.meters[0].common.md == "SE-RGMTR-1D-240C-A"
    assert device.meters[0].meter.w == -1500


async def test_meter_slots_shift_when_the_inverter_has_multiple_mppt(
    mock_modbus_unit,
) -> None:
    # Model 160 sits between the inverter model and the first meter, so every
    # meter slot moves along by the model's whole length. The old code carried
    # a hardcoded 50-or-70; the length now comes off the model chain.
    seed_inverter(mock_modbus_unit, mppt_units=3)
    seed_meter(mock_modbus_unit, meter_id=1, mppt_units=3)

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    await device.async_update()

    assert device.is_mmppt
    assert [meter.meter_id for meter in device.meters] == [1]
    assert device.meters[0].meter.w == -1500


@pytest.mark.parametrize("units", [2, 3])
async def test_mppt_modules_are_sized_from_the_device(mock_modbus_unit, units) -> None:
    seed_inverter(mock_modbus_unit, mppt_units=units)

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    await device.async_update()

    assert device.mppt is not None
    assert device.mppt.n == units
    assert len(device.mppt.module) == units

    first, second = device.mppt.module[0], device.mppt.module[1]
    assert first.id == 1
    assert first.id_str == "String 1"
    # Every module scales off the shared factors in model 160's fixed block,
    # so DCA_SF = -2 applies to each module's own raw current.
    assert first.dca == pytest.approx(4.45)
    assert second.dca == pytest.approx(4.46)
    assert first.dcv == pytest.approx(380.1)
    assert first.dcw == 2740


async def test_batteries_are_probed_and_empty_slots_dropped(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)
    seed_battery(mock_modbus_unit, battery_id=1)
    # Slot 2 answers, but with a nameplate energy of zero: no battery there.
    seed_battery(mock_modbus_unit, battery_id=2, rated_energy=0.0)
    # Slot 3 is not served at all.
    mock_modbus_unit.fail_read(58368, IllegalDataAddressError())

    device = SolarEdgeDevice(
        mock_modbus_unit, 1, SolarEdgeOptions(detect_batteries=True)
    )
    await device.async_setup()
    await device.async_add_batteries()
    await device.async_update()

    assert [battery.battery_id for battery in device.batteries] == [1]
    assert device.batteries[0].battery.soe == pytest.approx(75.0)


async def test_a_refused_optional_block_is_disabled_for_good(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)
    mock_modbus_unit.fail_read(40069 + 44, IllegalDataAddressError())

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    assert device.has_block("grid_status") is None  # untried

    await device.async_update()
    assert device.has_block("grid_status") is False
    assert device.grid_status is None

    reads_before = len(mock_modbus_unit.read_events)
    await device.async_update()
    covered = [
        event
        for event in mock_modbus_unit.read_events[reads_before:]
        if event.address == 40069 + 44
    ]
    assert not covered  # never asked for again


async def test_a_timing_out_optional_block_is_retried(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)
    mock_modbus_unit.fail_read(40069 + 44, ModbusTimeoutError("no answer"))

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    await device.async_update()

    assert device.timed_out_blocks() == ["grid_status"]
    assert device.has_block("grid_status") is None  # not settled

    mock_modbus_unit.fail_read(40069 + 44, None)
    await device.async_update()

    assert device.timed_out_blocks() == []
    assert device.has_block("grid_status") is True
    assert device.grid_status is not None
    assert device.grid_status.grid_status == 0


async def test_a_refused_optional_block_does_not_fail_the_poll(
    mock_modbus_unit,
) -> None:
    seed_inverter(mock_modbus_unit)
    seed_meter(mock_modbus_unit, meter_id=1)
    mock_modbus_unit.fail_read(40069 + 44, IllegalDataAddressError())
    mock_modbus_unit.fail_read(40069 + 50, IllegalDataAddressError())

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    await device.async_update()

    assert device.inverter.a == pytest.approx(12.34)
    assert device.meters[0].meter.w == -1500


async def test_one_poll_pools_the_whole_unit(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)
    seed_meter(mock_modbus_unit, meter_id=1)
    seed_meter(mock_modbus_unit, meter_id=2)
    seed_battery(mock_modbus_unit, battery_id=1)

    device = SolarEdgeDevice(
        mock_modbus_unit, 1, SolarEdgeOptions(detect_batteries=True)
    )
    await device.async_setup()
    await device.async_add_batteries()

    mock_modbus_unit.read_events.clear()
    await device.async_update()

    blocks = mock_modbus_unit.read_events
    assert all(block.count <= 125 for block in blocks)
    assert all(block.register_type == "holding" for block in blocks)

    # One read per SunSpec model, two for the battery's split block, and one
    # each for the two optional inverter blocks. Pooling does not collapse
    # these further, because each component's readable range stops the planner
    # from merging across a model boundary — which is the point: a merged read
    # would cover registers no field asked for.
    assert [(block.address, block.count) for block in blocks] == [
        (40002, 67),  # inverter common
        (40069, 40),  # inverter model, stopping short of the vendor events
        (40121, 67),  # meter 1 common
        (40188, 107),  # meter 1 model
        (40295, 67),  # meter 2 common
        (40362, 107),  # meter 2 model
        (57600, 76),  # battery identity and power limits
        (57708, 46),  # battery measurements, past the unmapped hole
        (40113, 2),  # grid status, on its own
        (40119, 2),  # extended vendor status, on its own
    ]


async def test_raw_read_covers_the_optional_blocks(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    raw = await device.async_read_raw()

    holding = raw["holding"]
    assert holding[40002] == 1  # the common model header
    assert holding[40069 + 44] == 0  # an optional block's registers are in there
    assert sorted(holding) == list(holding)  # addresses ascending, for diagnostics
