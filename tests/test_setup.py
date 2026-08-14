"""Set the integration up inside a running Home Assistant.

Every other test builds entities by hand against a stub coordinator. This one
is the only check that Home Assistant itself loads the config entry, forwards
the platforms and ends up with a register value from the mock in its state
machine — the step a migration can break without any unit test noticing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import (  # noqa: E402
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, State  # noqa: E402
from modbus_connection import ModbusExceptionError, ModbusTimeoutError  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

# Imported here rather than patched by name: Home Assistant refuses an import
# from inside the event loop, which is where the patch would otherwise happen.
from custom_components.solaredge_modbus_multi import hub  # noqa: E402

from .fixtures import seed_inverter, seed_meter, u32  # noqa: E402

DOMAIN = "solaredge_modbus_multi"


def _entry(device_list: list[int] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.0.2.10",
            CONF_PORT: 1502,
            CONF_NAME: "SolarEdge",
            "device_list": device_list or [1],
        },
        options={"detect_meters": True},
        version=2,
        minor_version=1,
        unique_id="192.0.2.10:1502",
    )


async def test_the_integration_boots_and_publishes_a_decoded_value(
    hass: HomeAssistant,
    enable_custom_integrations,
    mock_modbus_connection,
) -> None:
    unit = mock_modbus_connection.for_unit(1)
    seed_inverter(unit)
    seed_meter(unit, meter_id=1)

    entry = _entry()
    entry.add_to_hass(hass)

    with patch.object(hub, "ModbusConnection", return_value=mock_modbus_connection):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    states = {
        state.entity_id: state
        for state in hass.states.async_all()
        if state.entity_id.startswith(("sensor.", "binary_sensor."))
    }
    assert len(states) > 20

    # The seeded inverter reads 1234 with a scale factor of -2.
    assert states["sensor.solaredge_i1_ac_current"].state == "12.34"
    # And the meter, so the second device on the link is polled too.
    assert states["sensor.solaredge_i1_m1_ac_power"].state == "-1500"
    # The meter's identity block is only read by the first poll, so this is
    # also the check that its device attributes survived that ordering.
    assert states["sensor.solaredge_i1_m1_device"].state == "SE-RGMTR-1D-240C-A"


async def test_a_lifetime_total_is_restored_across_a_restart(
    hass: HomeAssistant,
    enable_custom_integrations,
    mock_modbus_connection,
) -> None:
    """A SolarEdge inverter powers down every night, which a restart can land in.

    It answers again with a lifetime counter of zero — SunSpec for "nothing
    accumulated" — so the value Home Assistant had before the restart is the
    only one there is, and publishing unknown instead would gap the energy
    dashboard.
    """
    unit = mock_modbus_connection.for_unit(1)
    seed_inverter(unit)
    unit.holding[40093] = u32(0)

    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State("sensor.solaredge_i1_ac_energy", "12.345"),
                {"native_value": 12345, "native_unit_of_measurement": "Wh"},
            ),
        ),
    )

    entry = _entry()
    entry.add_to_hass(hass)

    with patch.object(hub, "ModbusConnection", return_value=mock_modbus_connection):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.solaredge_i1_ac_energy").state == "12.345"


async def test_a_unit_that_answers_nothing_fails_the_update_with_a_reason(
    hass: HomeAssistant,
    enable_custom_integrations,
    mock_modbus_connection,
) -> None:
    """Home Assistant shows the user the message and nothing else.

    Every device on the link stops answering, so there is nothing to publish
    and the update fails — carrying one of the errors that failed it, rather
    than a bare "nothing answered".
    """
    unit = mock_modbus_connection.for_unit(1)
    seed_inverter(unit)

    entry = _entry()
    entry.add_to_hass(hass)

    with patch.object(hub, "ModbusConnection", return_value=mock_modbus_connection):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        total = hass.states.get("sensor.solaredge_i1_ac_energy").state
        # Retrying five times with a growing wait is the right thing to do
        # against a real inverter and far too slow here.
        hass.data[DOMAIN]["yaml"]["retry"] = {"limit": 1}

        unit.fail_requests(ModbusExceptionError(2))
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert "Nothing answered" in str(coordinator.last_exception)
    assert "code 2" in str(coordinator.last_exception).lower()

    assert hass.states.get("sensor.solaredge_i1_ac_current").state == STATE_UNAVAILABLE
    # The total holds instead, so long-term statistics keep their history.
    assert hass.states.get("sensor.solaredge_i1_ac_energy").state == total


async def test_one_silent_inverter_leaves_the_others_polling(
    hass: HomeAssistant,
    enable_custom_integrations,
    mock_modbus_connection,
) -> None:
    """A follower asleep at dusk, on a link that is still up.

    Nothing on that unit answers, so its own poll gives up on the first read
    rather than paying a timeout per device. The units that do answer must
    still be polled, which is the whole reason each one is read on its own.
    """
    seed_inverter(mock_modbus_connection.for_unit(1))
    seed_inverter(mock_modbus_connection.for_unit(2), serial="9A3C5D71")

    entry = _entry([1, 2])
    entry.add_to_hass(hass)

    with patch.object(hub, "ModbusConnection", return_value=mock_modbus_connection):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        mock_modbus_connection.for_unit(2).fail_requests(ModbusTimeoutError("asleep"))
        mock_modbus_connection.for_unit(1).holding[40069 + 2] = 2345
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert hass.states.get("sensor.solaredge_i1_ac_current").state == "23.45"
    assert hass.states.get("sensor.solaredge_i2_ac_current").state == STATE_UNAVAILABLE
