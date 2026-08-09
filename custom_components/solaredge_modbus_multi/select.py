from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    LIMIT_CONTROL,
    LIMIT_CONTROL_MODE,
    REACTIVE_POWER_CONFIG,
    STORAGE_AC_CHARGE_POLICY,
    STORAGE_CONTROL_MODE,
    STORAGE_MODE,
)
from .solaredge import SiteLimit

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][config_entry.entry_id]["hub"]
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []

    for inverter in hub.inverters:
        """Power Control Options: Storage Control"""
        if hub.option_storage_control and inverter.device.has_block("storage_control"):
            entities.append(StorageControlMode(inverter, config_entry, coordinator))
            entities.append(StorageACChargePolicy(inverter, config_entry, coordinator))
            entities.append(StorageDefaultMode(inverter, config_entry, coordinator))
            entities.append(StorageCommandMode(inverter, config_entry, coordinator))

        """ Power Control Options: Site Limit Control """
        if hub.option_site_limit_control:
            entities.append(
                SolaredgeLimitControlMode(inverter, config_entry, coordinator)
            )
            entities.append(SolaredgeLimitControl(inverter, config_entry, coordinator))

        """ Power Control Block """
        if hub.option_detect_extras and inverter.device.has_block(
            "advanced_power_control"
        ):
            entities.append(
                SolarEdgeReactivePowerMode(inverter, config_entry, coordinator)
            )

    if entities:
        async_add_entities(entities)


def get_key(d, search):
    for k, v in d.items():
        if v == search:
            return k
    return None


class SolarEdgeSelectBase(CoordinatorEntity, SelectEntity):
    should_poll = False
    _attr_has_entity_name = True
    entity_category = EntityCategory.CONFIG

    def __init__(self, platform, config_entry, coordinator):
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


class SolarEdgeEnumSelect(SolarEdgeSelectBase):
    """A select over one register that holds an enumerated mode.

    Subclasses name the block, the field on it, and the code-to-label mapping;
    a code the mapping does not cover leaves the entity unavailable rather than
    showing a value that is not one of its options.
    """

    _options: dict
    _field: str

    def __init__(self, platform, config_entry, coordinator):
        super().__init__(platform, config_entry, coordinator)
        self._attr_options = list(self._options.values())

    @property
    def block(self):
        """The component holding this select's register."""
        raise NotImplementedError

    @property
    def _value(self):
        block = self.block
        return None if block is None else getattr(block, self._field)

    @property
    def available(self) -> bool:
        value = self._value
        return super().available and value is not None and value in self._options

    @property
    def current_option(self) -> str | None:
        # A code outside the mapping already makes the entity unavailable, but
        # Home Assistant may still read this; None beats raising.
        return self._options.get(self._value)

    async def async_select_option(self, option: str) -> None:
        _LOGGER.debug(f"set {self.unique_id} to {option}")
        await self._platform.async_write(
            self.block, self._field, get_key(self._options, option)
        )
        await self.async_update()


class SolarEdgeStorageSelect(SolarEdgeEnumSelect):
    """A select over the storage control block."""

    @property
    def block(self):
        return self._platform.storage_control

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self._platform.has_battery is True


class StorageControlMode(SolarEdgeStorageSelect):
    _options = STORAGE_CONTROL_MODE
    _field = "control_mode"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_storage_control_mode"

    @property
    def name(self) -> str:
        return "Storage Control Mode"


class StorageACChargePolicy(SolarEdgeStorageSelect):
    _options = STORAGE_AC_CHARGE_POLICY
    _field = "ac_charge_policy"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_ac_charge_policy"

    @property
    def name(self) -> str:
        return "AC Charge Policy"


class SolarEdgeRemoteControlSelect(SolarEdgeStorageSelect):
    """A storage select that only means anything in remote control mode."""

    @property
    def available(self) -> bool:
        block = self.block
        return (
            super().available
            and block is not None
            and block.control_mode == 4  # remote control
        )


class StorageDefaultMode(SolarEdgeRemoteControlSelect):
    _options = STORAGE_MODE
    _field = "default_mode"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_storage_default_mode"

    @property
    def name(self) -> str:
        return "Storage Default Mode"


class StorageCommandMode(SolarEdgeRemoteControlSelect):
    _options = STORAGE_MODE
    _field = "command_mode"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_storage_command_mode"

    @property
    def name(self) -> str:
        return "Storage Command Mode"


class SolaredgeLimitControlMode(SolarEdgeSelectBase):
    """The three mutually exclusive low bits of the limit control mode word.

    Unlike the other selects this one is not a value but a bit position, so
    changing it is a read-modify-write of the whole mode register.
    """

    def __init__(self, platform, config_entry, coordinator):
        super().__init__(platform, config_entry, coordinator)
        self._options = LIMIT_CONTROL_MODE
        self._attr_options = list(self._options.values())

    @property
    def _mode(self) -> int | None:
        block = self._platform.site_limit
        return None if block is None else block.e_lim_ctl_mode

    @property
    def available(self) -> bool:
        return super().available and self._mode is not None

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_limit_control_mode"

    @property
    def name(self) -> str:
        return "Limit Control Mode"

    @property
    def current_option(self) -> str:
        mode = int(self._mode)
        for bit in (0, 1, 2):
            if (mode >> bit) & 1:
                return self._options[bit]
        return self._options[None]

    async def async_select_option(self, option: str) -> None:
        new_mode = get_key(self._options, option)
        # The three modes are mutually exclusive: selecting one clears the
        # other two, and "none" clears all three.
        value = 0 if new_mode is None else 1 << int(new_mode)

        _LOGGER.debug(f"set {self.unique_id} to bit {new_mode}")
        await self._platform.async_write_mode_bits(SiteLimit.LIMIT_MODE_BITS, value)
        await self.async_update()


class SolaredgeLimitControl(SolarEdgeEnumSelect):
    _options = LIMIT_CONTROL
    _field = "e_lim_ctl"

    @property
    def block(self):
        return self._platform.site_limit

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_limit_control"

    @property
    def name(self) -> str:
        return "Limit Control"


class SolarEdgeReactivePowerMode(SolarEdgeEnumSelect):
    _options = REACTIVE_POWER_CONFIG
    _field = "reactive_pwr_config"

    @property
    def block(self):
        return self._platform.advanced_power_control

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_reactive_power_mode"

    @property
    def name(self) -> str:
        return "Reactive Power Mode"
