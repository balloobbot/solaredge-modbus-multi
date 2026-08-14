"""One device that stops answering must not take the rest of the poll with it.

A SolarEdge unit carries an inverter, its meters and its batteries behind one
Modbus id. They used to be read as a single pooled plan, so a meter that went
quiet discarded the inverter's readings too and every entity in Home Assistant
went unavailable. Each is polled on its own now, and the poll reports by name
what refreshed and what did not.
"""

from __future__ import annotations

import pytest
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
)
from solaredge import DeviceNotSetUp, SolarEdgeDevice, SolarEdgeOptions

from .fixtures import i16, seed_battery, seed_inverter, seed_meter

METER_1_BASE = 40121


async def _seeded(unit, **options) -> SolarEdgeDevice:
    """An inverter with one meter and one battery, set up and polled once."""
    seed_inverter(unit)
    seed_meter(unit, meter_id=1)
    seed_battery(unit, battery_id=1)
    device = SolarEdgeDevice(
        unit, 1, SolarEdgeOptions(detect_batteries=True, **options)
    )
    await device.async_setup()
    await device.async_add_batteries()
    return device


async def test_a_failed_meter_leaves_the_rest_fresh(mock_modbus_unit) -> None:
    device = await _seeded(mock_modbus_unit)
    await device.async_update()
    before = device.meters[0].meter.w

    mock_modbus_unit.holding[40069 + 14] = 6543  # inverter power changes
    mock_modbus_unit.holding[METER_1_BASE + 67 + 18] = i16(-3000)  # so does the meter's
    mock_modbus_unit.fail_read(METER_1_BASE, ModbusTimeoutError("slow meter"))
    report = await device.async_update()

    assert not report.complete
    assert set(report.failed) == {"meter_1"}
    assert isinstance(report.failed["meter_1"], ModbusTimeoutError)
    assert {"inverter", "battery_1"} <= report.updated
    assert device.inverter.w == 6543
    assert device.batteries[0].battery.soe == pytest.approx(75.0)
    assert device.meters[0].meter.w == before  # previous value kept

    # And the value really did move on the device, so the line above is about
    # containment rather than a register nobody changed.
    mock_modbus_unit.fail_read(METER_1_BASE, None)
    assert (await device.async_update()).complete
    assert device.meters[0].meter.w == -3000


async def test_listeners_fire_at_the_end_and_only_for_fresh_devices(
    mock_modbus_unit,
) -> None:
    device = await _seeded(mock_modbus_unit)
    await device.async_update()
    seen: list[int] = []
    device.inverter.add_update_listener(
        lambda: seen.append(len(mock_modbus_unit.read_events))
    )
    device.meters[0].meter.add_update_listener(lambda: seen.append(-1))

    mock_modbus_unit.fail_read(METER_1_BASE, ModbusTimeoutError("slow meter"))
    mock_modbus_unit.read_events.clear()
    await device.async_update()

    # One notification, after every read was tried; none for the failure.
    assert seen == [len(mock_modbus_unit.read_events)]


async def test_a_unit_that_answers_nothing_raises_instead_of_being_walked(
    mock_modbus_unit,
) -> None:
    """An inverter asleep behind a bridge that keeps the socket open.

    The link is up, so nothing raises a connection error; every device on the
    unit would simply time out in turn. The first read is the probe, and its
    timeout ends the poll rather than costing one per device.
    """
    device = await _seeded(mock_modbus_unit)
    await device.async_update()

    mock_modbus_unit.fail_requests(ModbusTimeoutError("asleep"))
    mock_modbus_unit.read_events.clear()
    with pytest.raises(ModbusTimeoutError):
        await device.async_update()

    # The mock records a read before it fails it, so this counts what was
    # actually asked for: the probe, and nothing behind it.
    assert len(mock_modbus_unit.read_events) == 1


async def test_a_dead_link_raises_instead_of_reporting(mock_modbus_unit) -> None:
    device = await _seeded(mock_modbus_unit)
    await device.async_update()

    mock_modbus_unit.fail_requests(ModbusConnectionError("link down"))
    with pytest.raises(ModbusConnectionError):
        await device.async_update()


async def test_every_device_refreshes_on_a_healthy_unit(mock_modbus_unit) -> None:
    device = await _seeded(mock_modbus_unit)
    report = await device.async_update()

    assert report.complete
    assert report.failed == {}
    assert report.updated == {
        "inverter",
        "meter_1",
        "battery_1",
        "grid_status",
        "status_vendor4",
    }


async def test_a_failing_optional_block_is_reported_but_contained(
    mock_modbus_unit,
) -> None:
    device = await _seeded(mock_modbus_unit)
    mock_modbus_unit.fail_read(40113, ModbusTimeoutError("slow grid status"))
    report = await device.async_update()

    assert set(report.failed) == {"grid_status"}
    assert device.timed_out_blocks() == ["grid_status"]
    assert "inverter" in report.updated
    assert device.inverter.w == 5432


async def test_a_refused_optional_block_is_absent_rather_than_failed(
    mock_modbus_unit,
) -> None:
    device = await _seeded(mock_modbus_unit)
    mock_modbus_unit.fail_read(40113, IllegalDataAddressError())
    report = await device.async_update()

    # The inverter does not serve it, which is not a failure to report.
    assert report.complete
    assert "grid_status" not in report.updated
    assert device.has_block("grid_status") is False


async def test_a_failed_setup_leaves_the_device_unset_up(mock_modbus_unit) -> None:
    """The identity block is the poll's foundation; without it nothing is partial."""
    seed_inverter(mock_modbus_unit)
    mock_modbus_unit.fail_read(40002, ModbusTimeoutError("no identity"))

    device = SolarEdgeDevice(mock_modbus_unit, 1)
    with pytest.raises(ModbusTimeoutError):
        await device.async_setup()
    assert device.common is None
    with pytest.raises(DeviceNotSetUp):
        await device.async_update()

    # The next attempt retries the read rather than settling for what it got.
    mock_modbus_unit.fail_read(40002, None)
    await device.async_setup()
    assert device.common.sn == "7E1B4C29"
    assert (await device.async_update()).complete
