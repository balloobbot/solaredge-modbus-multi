"""Switch platform for SolarEdge Modbus Multi."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][config_entry.entry_id]["hub"]
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []

    """ Power Control Options: Site Limit Control """
    for inverter in hub.inverters:
        if hub.option_site_limit_control is True:
            entities.append(
                SolarEdgeExternalProduction(inverter, config_entry, coordinator)
            )
            entities.append(
                SolarEdgeNegativeSiteLimit(inverter, config_entry, coordinator)
            )

        if hub.option_detect_extras and inverter.device.has_block(
            "advanced_power_control"
        ):
            entities.append(SolarEdgeGridControl(inverter, config_entry, coordinator))

    if entities:
        async_add_entities(entities)


class SolarEdgeSwitchBase(CoordinatorEntity, SwitchEntity):
    should_poll = False
    _attr_has_entity_name = True

    def __init__(self, platform, config_entry, coordinator) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        """Initialize the sensor."""
        self._platform = platform
        self._config_entry = config_entry

    @property
    def device_info(self):
        return self._platform.device_info

    @property
    def config_entry_id(self):
        return self._config_entry.entry_id

    @property
    def config_entry_name(self):
        return self._config_entry.data["name"]

    @property
    def available(self) -> bool:
        return super().available and self._platform.online

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class SolarEdgeLimitControlModeBit(SolarEdgeSwitchBase):
    """One bit of the site limit control mode word.

    That register packs five independent options, so the field is declared as
    the single bit it owns and the write re-reads the register and merges,
    leaving the other options alone.
    """

    entity_category = EntityCategory.CONFIG

    _field: str

    @property
    def _bit(self) -> bool | None:
        block = self._platform.site_limit
        return None if block is None else getattr(block, self._field)

    @property
    def available(self) -> bool:
        return super().available and self._bit is not None

    @property
    def is_on(self) -> bool:
        return bool(self._bit)

    async def _async_set_bit(self, on: bool) -> None:
        _LOGGER.debug(f"set {self.unique_id} to {int(on)}")

        await self._platform.async_write(self._platform.site_limit, self._field, on)
        await self.async_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self._async_set_bit(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self._async_set_bit(False)


class SolarEdgeExternalProduction(SolarEdgeLimitControlModeBit):
    """External Production switch. Indicates a non-SolarEdge power sorce in system."""

    _field = "external_production"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_external_production"

    @property
    def name(self) -> str:
        return "External Production"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False


class SolarEdgeNegativeSiteLimit(SolarEdgeLimitControlModeBit):
    """Negative Site Limit switch. Sets minimum import power when enabled."""

    _field = "negative_limit"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_negative_site_limit"

    @property
    def name(self) -> str:
        return "Negative Site Limit"


class SolarEdgeGridControl(SolarEdgeSwitchBase):
    """Grid Control boolean switch. This is "AdvancedPwrControlEn" in specs."""

    entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        block = self._platform.advanced_power_control
        return (
            super().available
            and block is not None
            and block.adv_pwr_ctrl_en is not None
        )

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_adv_pwr_ctrl"

    @property
    def name(self) -> str:
        return "Advanced Power Control"

    @property
    def is_on(self) -> bool:
        return self._platform.advanced_power_control.adv_pwr_ctrl_en == 0x1

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug(f"set {self.unique_id} to 0x1")

        await self._platform.async_write(
            self._platform.advanced_power_control, "adv_pwr_ctrl_en", 0x1
        )
        await self.async_update()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug(f"set {self.unique_id} to 0x0")

        await self._platform.async_write(
            self._platform.advanced_power_control, "adv_pwr_ctrl_en", 0x0
        )
        await self.async_update()
