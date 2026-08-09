"""Tests for the unit ID scanner.

The scan has to tell three cases apart, and the third one is the reason this
used to be written against a raw socket: an inverter, some other Modbus device,
and nothing at all at that unit ID.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from modbus_connection import IllegalDataAddressError, ModbusTimeoutError  # noqa: E402
from modbus_connection.mock import MockModbusConnection  # noqa: E402
from solaredge_modbus_multi.scanner import SolarEdgeDeviceScanner  # noqa: E402

from .fixtures import seed_inverter, string_registers  # noqa: E402


@pytest.fixture
def scanner(monkeypatch):
    """A scanner wired to the in-memory backend instead of a real socket."""
    connection = MockModbusConnection()
    monkeypatch.setattr(
        "solaredge_modbus_multi.scanner.ModbusConnection",
        lambda *args, **kwargs: connection,
    )
    scanner = SolarEdgeDeviceScanner(host="192.0.2.10", port=1502, scan_retries=2)
    scanner.connection = connection
    return scanner


async def test_finds_a_solaredge_inverter(scanner) -> None:
    seed_inverter(scanner.connection.for_unit(3))

    assert await scanner.scan_device_id(3) == SolarEdgeDeviceScanner.FOUND_INV


async def test_another_manufacturers_sunspec_device_is_not_an_inverter(
    scanner,
) -> None:
    unit = scanner.connection.for_unit(4)
    seed_inverter(unit)
    unit.holding[40004] = string_registers("Acme Power", 16)

    assert await scanner.scan_device_id(4) == SolarEdgeDeviceScanner.FOUND


async def test_a_device_that_refuses_the_registers_still_counts_as_present(
    scanner,
) -> None:
    scanner.connection.for_unit(5).fail_read(40000, IllegalDataAddressError())

    assert await scanner.scan_device_id(5) == SolarEdgeDeviceScanner.FOUND


async def test_an_empty_unit_id_is_not_found(scanner) -> None:
    scanner.connection.for_unit(6).fail_read(40000, ModbusTimeoutError("no answer"))

    assert await scanner.scan_device_id(6) == SolarEdgeDeviceScanner.NOT_FOUND


async def test_check_list_sorts_the_three_outcomes(scanner) -> None:
    seed_inverter(scanner.connection.for_unit(1))
    other = scanner.connection.for_unit(2)
    seed_inverter(other)
    other.holding[40004] = string_registers("Acme Power", 16)
    scanner.connection.for_unit(3).fail_read(40000, ModbusTimeoutError("no answer"))

    result = await scanner.check_list([1, 2, 3])

    assert result == {
        "inverters": [1],
        "other_devices": [2],
        "no_response": [3],
    }


async def test_scan_list_reports_progress(scanner) -> None:
    seed_inverter(scanner.connection.for_unit(2))
    for unit_id in (1, 3):
        scanner.connection.for_unit(unit_id).fail_read(
            40000, ModbusTimeoutError("no answer")
        )

    progress = []

    async def report(scanned, total):
        progress.append((scanned, total))

    found = await scanner.scan_list([1, 2, 3], progress_callback=report)

    assert found == [2]
    assert progress == [(0, 3), (1, 3), (2, 3), (3, 3)]
