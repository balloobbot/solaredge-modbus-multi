"""Diagnostics support for SolarEdge Modbus Multi Device."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from modbus_connection import ModbusError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

REDACT_CONFIG = {"unique_id", "host"}
# "sn" is the SunSpec common block's serial number point and "serial" the
# battery block's; both identify the user's hardware.
REDACT_DEVICE = {"identifiers", "serial", "serial_number", "sn", "via_device"}


def _library_version() -> str:
    try:
        return version("modbus-connection")
    except PackageNotFoundError:  # pragma: no cover - only if run from a checkout
        return "unknown"


def _component_values(component: Any) -> dict[str, Any]:
    """Every decoded field of a component, by name.

    ``declared_fields`` is the model's own view of itself, so a field added to
    the register map turns up here without anyone remembering to list it.
    """
    if component is None:
        return {}
    return {name: getattr(component, name) for name in component.declared_fields}


def _device_info(device: Any) -> dict[str, Any]:
    """The device registry entry, as plain data."""
    return dict(device.device_info)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    hub = hass.data[DOMAIN][config_entry.entry_id]["hub"]

    data: dict[str, Any] = {
        "modbus_connection_version": _library_version(),
        "config_entry": async_redact_data(config_entry.as_dict(), REDACT_CONFIG),
        "yaml": async_redact_data(hass.data[DOMAIN]["yaml"], REDACT_CONFIG),
    }

    for inverter in hub.inverters:
        entry: dict[str, Any] = {
            "device_info": _device_info(inverter),
            "unit_id": inverter.inverter_unit_id,
            "is_mmppt": inverter.is_mmppt,
            "use_status_vendor4": inverter.use_status_vendor4,
            "has_battery": inverter.has_battery,
            "optional_blocks": {
                name: inverter.device.has_block(name)
                for name in (
                    "grid_status",
                    "status_vendor4",
                    "global_power_control",
                    "advanced_power_control",
                    "advanced_power_control_2",
                    "site_limit",
                    "ext_prod_max",
                    "storage_control",
                )
            },
            "common": _component_values(inverter.common),
            "model": _component_values(inverter.model_block),
            "mmppt": _component_values(inverter.mppt),
            "mmppt_modules": [
                _component_values(unit.module) for unit in inverter.mmppt_units
            ],
            "storage_control": _component_values(inverter.storage_control),
            "site_limit": _component_values(inverter.site_limit),
            "global_power_control": _component_values(inverter.global_power_control),
            "advanced_power_control": {
                **_component_values(inverter.advanced_power_control),
                **_component_values(inverter.advanced_power_control_2),
            },
            "registers": await _async_raw_registers(inverter),
        }
        data[f"inverter_unit_id_{inverter.inverter_unit_id}"] = async_redact_data(
            entry, REDACT_DEVICE
        )

    for meter in hub.meters:
        data[f"meter_id_{meter.meter_id}"] = async_redact_data(
            {
                "device_info": _device_info(meter),
                "inverter_unit_id": meter.inverter_unit_id,
                "common": _component_values(meter.common),
                "model": _component_values(meter.model_block),
            },
            REDACT_DEVICE,
        )

    for battery in hub.batteries:
        data[f"battery_id_{battery.battery_id}"] = async_redact_data(
            {
                "device_info": _device_info(battery),
                "inverter_unit_id": battery.inverter_unit_id,
                "model": _component_values(battery.model_block),
            },
            REDACT_DEVICE,
        )

    for evse in hub.evses:
        data[f"evse_unit_id_{evse.evse_unit_id}"] = async_redact_data(
            {
                "device_info": _device_info(evse),
                "common": _component_values(evse.common),
            },
            REDACT_DEVICE,
        )

    return data


async def _async_raw_registers(inverter: Any) -> dict[str, Any] | str:
    """The undecoded register map, which replays straight into a test.

    A raw dump loads into modbus-connection's mock backend with ``load_raw``,
    so a dump attached to an issue can back a regression test with no hardware.
    """
    try:
        raw = await inverter.device.async_read_raw()
    except ModbusError as err:
        return f"read failed: {err}"
    return {
        space: {str(address): value for address, value in values.items()}
        for space, values in raw.items()
    }
