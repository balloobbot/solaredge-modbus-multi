"""Discovery and polling tests for the device object."""

from __future__ import annotations

import pytest
from modbus_connection import IllegalDataAddressError, ModbusTimeoutError
from modbus_connection.model.sunspec import SunSpecMapShiftError
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

    # The first poll sizes the group; from the second on, the modules read
    # from the component's own folded plan. Both must decode the same.
    for _ in range(2):
        await device.async_update()

        assert device.mppt is not None
        assert device.mppt.n == units
        assert len(device.mppt.module) == units

        first, second = device.mppt.module[0], device.mppt.module[1]
        assert first.id == 1
        assert first.id_str == "String 1"
        # Every module scales off the shared factors in model 160's fixed
        # block, so DCA_SF = -2 applies to each module's own raw current.
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

    # A component that declares no readable map stands for the addresses it
    # reads by itself, so the planner may join models that sit back to back but
    # never bridges a gap no field claims. That is the property that matters:
    # the inverter model's claim stops at ``st_vnd`` (40108), so no pooled read
    # reaches the vendor event registers at 40113 and 40119 that some firmware
    # refuses — those stay their own small reads. Where models do abut, the
    # reads merge and are cut at the 125-register Modbus ceiling instead of at
    # a model boundary, which costs nothing: every register in them is one a
    # field asked for.
    assert [(block.address, block.count) for block in blocks] == [
        (40002, 107),  # inverter common and inverter model, back to back
        (40121, 124),  # both meters' common blocks and models, in three reads
        (40245, 125),
        (40370, 99),
        (57600, 76),  # battery identity and power limits
        (57708, 46),  # battery measurements, past the unmapped hole
        (40113, 2),  # grid status, on its own
        (40119, 2),  # extended vendor status, on its own
    ]


@pytest.mark.parametrize("mppt_units", [None, 2, 3])
async def test_no_pooled_read_reaches_the_vendor_event_registers(
    mock_modbus_unit, mppt_units
) -> None:
    # 40109-40120 is claimed by nothing: the inverter model's fields stop at
    # ``st_vnd`` (40108) and the next model in the chain starts at 40121. A read
    # crossing it would touch 40113 and 40119, which some firmware refuses and
    # which would then fail the whole poll rather than one optional block. A gap
    # no member claims separates two readable runs, so the planner may not
    # bridge it however the models around it are laid out.
    seed_inverter(mock_modbus_unit, mppt_units=mppt_units)
    seed_meter(mock_modbus_unit, meter_id=1, mppt_units=mppt_units)
    seed_battery(mock_modbus_unit, battery_id=1)

    device = SolarEdgeDevice(
        mock_modbus_unit, 1, SolarEdgeOptions(detect_batteries=True)
    )
    await device.async_setup()
    await device.async_add_batteries()

    # The steady-state poll runs a wider plan than the first — the MPPT
    # modules fold into model 160's own run once the count is known — so the
    # gap has to survive both plans.
    for _ in range(2):
        mock_modbus_unit.read_events.clear()
        await device.async_update()

        for address in (40113, 40119):
            covering = [
                (event.address, event.count)
                for event in mock_modbus_unit.read_events
                if event.address <= address < event.address + event.count
            ]
            # Only the optional block's own two-register read reaches it.
            assert covering == [(address, 2)]


async def test_raw_read_covers_the_optional_blocks(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    raw = await device.async_read_raw()

    holding = raw["holding"]
    assert holding[40002] == 1  # the common model header
    assert holding[40069 + 44] == 0  # an optional block's registers are in there
    assert sorted(holding) == list(holding)  # addresses ascending, for diagnostics


async def test_a_moved_model_is_reported_rather_than_read_stale(
    mock_modbus_unit,
) -> None:
    seed_inverter(mock_modbus_unit)

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    await device.async_setup()
    await device.async_update()

    # A firmware update rearranges the chain, so the components built at setup
    # now point at the wrong registers. The header check catches it.
    mock_modbus_unit.holding[40069] = [160, 48]

    with pytest.raises(SunSpecMapShiftError):
        await device.async_update()


async def test_the_slow_blocks_get_their_own_budget(mock_modbus_unit) -> None:
    seed_inverter(mock_modbus_unit)

    device = SolarEdgeDevice(
        mock_modbus_unit,
        1,
        SolarEdgeOptions(detect_extras=True, slow_block_timeout=6.0),
    )
    await device.async_setup()

    # The power control blocks answer slowly on some inverters and need more
    # than the connection-wide timeout; nothing else does.
    budgets = {name: block.timeout for name, block in device._optional.items()}
    assert budgets == {
        "grid_status": None,
        "status_vendor4": None,
        "global_power_control": 6.0,
        "advanced_power_control": 6.0,
        "advanced_power_control_2": 6.0,
    }


async def test_an_evse_stops_at_its_identity_block(mock_modbus_unit) -> None:
    from .fixtures import END_OF_CHAIN, seed_common

    # An EVSE publishes the SunSpec identity block and no models behind it.
    mock_modbus_unit.holding[40000] = [0x5375, 0x6E53]
    after = seed_common(
        mock_modbus_unit.holding, 40002, model="SE-EV-SA-KIT-A", serial="EV0001"
    )
    mock_modbus_unit.holding[after] = END_OF_CHAIN

    device = SolarEdgeDevice(mock_modbus_unit, 2)
    common = await device.async_read_identity()

    assert common.md == "SE-EV-SA-KIT-A"
    assert common.sn == "EV0001"

    await device.async_update()  # polling one only refreshes its identity
    assert device.common.vr == "0004.0019.0033"

    with pytest.raises(DeviceInvalid):
        await device.async_setup()  # there is no inverter model behind it
