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
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

# Imported here rather than patched by name: Home Assistant refuses an import
# from inside the event loop, which is where the patch would otherwise happen.
from custom_components.solaredge_modbus_multi import hub  # noqa: E402

from .fixtures import seed_inverter, seed_meter  # noqa: E402

DOMAIN = "solaredge_modbus_multi"


async def test_the_integration_boots_and_publishes_a_decoded_value(
    hass: HomeAssistant,
    enable_custom_integrations,
    mock_modbus_connection,
) -> None:
    unit = mock_modbus_connection.for_unit(1)
    seed_inverter(unit)
    seed_meter(unit, meter_id=1)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.0.2.10",
            CONF_PORT: 1502,
            CONF_NAME: "SolarEdge",
            "device_list": [1],
        },
        options={"detect_meters": True},
        version=2,
        minor_version=1,
        unique_id="192.0.2.10:1502",
    )
    entry.add_to_hass(hass)

    with patch.object(
        hub, "ModbusConnection", return_value=mock_modbus_connection
    ):
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
