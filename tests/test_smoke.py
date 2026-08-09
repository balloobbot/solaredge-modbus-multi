"""Smoke test: does the device library read a seeded inverter at all?"""

from __future__ import annotations

from solaredge import SolarEdgeDevice, SolarEdgeOptions


async def test_reads_a_seeded_inverter(inverter_unit) -> None:
    device = SolarEdgeDevice(inverter_unit, 1, SolarEdgeOptions())
    await device.async_setup()
    await device.async_update()

    assert device.common.mn == "SolarEdge"
    assert device.inverter is not None
    assert device.inverter.a == 12.34
    assert device.inverter.w == 5432
    assert len(device.meters) == 1
