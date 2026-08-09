from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, BatteryLimit

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
        """Dynamic Power Control"""
        if hub.option_detect_extras and inverter.device.has_block(
            "global_power_control"
        ):
            entities.append(
                SolarEdgeActivePowerLimitSet(inverter, config_entry, coordinator)
            )
            entities.append(SolarEdgeCosPhiSet(inverter, config_entry, coordinator))

        """ Power Control Block """
        if hub.option_detect_extras and inverter.device.has_block(
            "advanced_power_control"
        ):
            entities.append(SolarEdgePowerReduce(inverter, config_entry, coordinator))
            entities.append(SolarEdgeCurrentLimit(inverter, config_entry, coordinator))

    """ Power Control Options: Storage Control """
    if hub.option_storage_control is True:
        for inverter in hub.inverters:
            if inverter.device.has_block("storage_control") is False:
                continue
            entities.append(StorageACChargeLimit(inverter, config_entry, coordinator))
            entities.append(StorageBackupReserve(inverter, config_entry, coordinator))
            entities.append(StorageCommandTimeout(inverter, config_entry, coordinator))
            if inverter.has_battery is True:
                entities.append(StorageChargeLimit(inverter, config_entry, coordinator))
                entities.append(
                    StorageDischargeLimit(inverter, config_entry, coordinator)
                )

    """ Power Control Options: Site Limit Control """
    if hub.option_site_limit_control is True:
        for inverter in hub.inverters:
            entities.append(SolarEdgeSiteLimit(inverter, config_entry, coordinator))
            entities.append(
                SolarEdgeExternalProductionMax(inverter, config_entry, coordinator)
            )

    if entities:
        async_add_entities(entities)


def get_key(d, search):
    for k, v in d.items():
        if v == search:
            return k
    return None


class SolarEdgeNumberBase(CoordinatorEntity, NumberEntity):
    should_poll = False
    _attr_has_entity_name = True
    entity_category = EntityCategory.CONFIG

    def __init__(self, platform, config_entry, coordinator):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        """Initialize the number."""
        self._platform = platform
        self._config_entry = config_entry

    @property
    def block(self):
        """The component holding this number's register."""
        raise NotImplementedError

    @property
    def _value(self):
        """The decoded field, or ``None`` if the block is absent."""
        block = self.block
        return None if block is None else getattr(block, self._field)

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

    async def _async_write(self, value) -> None:
        _LOGGER.debug(f"set {self.unique_id} to {value}")
        await self._platform.async_write(self.block, self._field, value)
        await self.async_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class SolarEdgeStorageNumber(SolarEdgeNumberBase):
    """A number over the storage control block."""

    @property
    def block(self):
        return self._platform.storage_control

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self._platform.has_battery is True

    @property
    def _remote_control(self) -> bool:
        """Whether the inverter is taking storage commands over Modbus."""
        block = self.block
        return block is not None and block.control_mode == 4


class StorageACChargeLimit(SolarEdgeStorageNumber):
    icon = "mdi:lightning-bolt"

    _field = "ac_charge_limit"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_storage_ac_charge_limit"

    @property
    def name(self) -> str:
        return "AC Charge Limit"

    @property
    def _policy(self) -> int | None:
        block = self.block
        return None if block is None else block.ac_charge_policy

    @property
    def available(self) -> bool:
        value = self._value
        if value is None or value < 0:
            return False

        # Available for AC charge policies 2 & 3
        return super().available and self._policy in [2, 3]

    @property
    def native_unit_of_measurement(self) -> str | None:
        # kWh in AC policy "Fixed Energy Limit", % in AC policy "Percent of Production"
        if self._policy == 2:
            return UnitOfEnergy.KILO_WATT_HOUR
        elif self._policy == 3:
            return PERCENTAGE
        else:
            return None

    @property
    def native_min_value(self) -> int:
        return 0

    @property
    def native_max_value(self) -> int:
        # 100MWh in AC policy "Fixed Energy Limit"
        if self._policy == 2:
            return 100000000
        elif self._policy == 3:
            return 100
        else:
            return 0

    @property
    def native_value(self) -> int:
        return int(self._value)

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(float(value))


class StorageBackupReserve(SolarEdgeStorageNumber):
    native_unit_of_measurement = PERCENTAGE
    native_min_value = 0
    native_max_value = 100
    icon = "mdi:battery-positive"

    _field = "backup_reserve"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_storage_backup_reserve"

    @property
    def name(self) -> str:
        return "Backup Reserve"

    @property
    def available(self) -> bool:
        value = self._value
        return super().available and value is not None and 0 <= value <= 100

    @property
    def native_value(self) -> int:
        return int(self._value)

    async def async_set_native_value(self, value: int) -> None:
        await self._async_write(float(int(value)))


class StorageCommandTimeout(SolarEdgeStorageNumber):
    native_min_value = 0
    native_max_value = 86400  # 24h
    native_unit_of_measurement = UnitOfTime.SECONDS
    icon = "mdi:clock-end"

    _field = "command_timeout"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_storage_command_timeout"

    @property
    def name(self) -> str:
        return "Storage Command Timeout"

    @property
    def available(self) -> bool:
        value = self._value
        if value is None or value > 86400:
            return False

        # Available only in remote control mode
        return super().available and self._remote_control

    @property
    def native_value(self) -> int:
        return int(self._value)

    async def async_set_native_value(self, value: int) -> None:
        await self._async_write(int(value))


class StorageChargeLimit(SolarEdgeStorageNumber):
    native_min_value = 0
    native_step = 1.0
    native_unit_of_measurement = UnitOfPower.WATT
    icon = "mdi:lightning-bolt"

    _field = "charge_limit"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_storage_charge_limit"

    @property
    def name(self) -> str:
        return "Storage Charge Limit"

    @property
    def available(self) -> bool:
        value = self._value
        if value is None or value < 0:
            return False

        # Available only in remote control mode
        return super().available and self._remote_control

    @property
    def native_max_value(self) -> int:
        return BatteryLimit.ChargeMax

    @property
    def native_value(self) -> int:
        return int(self._value)

    async def async_set_native_value(self, value: int) -> None:
        await self._async_write(float(int(value)))


class StorageDischargeLimit(StorageChargeLimit):
    _field = "discharge_limit"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_storage_discharge_limit"

    @property
    def name(self) -> str:
        return "Storage Discharge Limit"

    @property
    def native_max_value(self) -> int:
        return BatteryLimit.DischargeMax


class SolarEdgeSiteLimitNumber(SolarEdgeNumberBase):
    """A number over the site limit block."""

    @property
    def block(self):
        return self._platform.site_limit

    @property
    def _mode(self) -> int | None:
        block = self._platform.site_limit
        return None if block is None else block.e_lim_ctl_mode


class SolarEdgeSiteLimit(SolarEdgeSiteLimitNumber):
    native_min_value = 0
    native_max_value = 1000000
    native_unit_of_measurement = UnitOfPower.WATT
    icon = "mdi:lightning-bolt"

    _field = "e_site_limit"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_site_limit"

    @property
    def name(self) -> str:
        return "Site Limit"

    @property
    def available(self) -> bool:
        mode = self._mode
        if self._value is None or mode is None:
            return False

        # Only meaningful while one of the three limiting modes is selected.
        return super().available and bool(int(mode) & 0b111)

    @property
    def native_value(self) -> int:
        value = self._value
        return 0 if value < 0 else int(value)

    async def async_set_native_value(self, value: int) -> None:
        await self._async_write(float(int(value)))


class SolarEdgeExternalProductionMax(SolarEdgeSiteLimitNumber):
    native_min_value = 0
    native_max_value = 1000000
    native_unit_of_measurement = UnitOfPower.WATT
    icon = "mdi:lightning-bolt"

    _field = "ext_prod_max"

    @property
    def block(self):
        return self._platform.ext_prod_max

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_external_production_max"

    @property
    def name(self) -> str:
        return "External Production Max"

    @property
    def available(self) -> bool:
        value = self._value
        mode = self._mode
        if value is None or value < 0 or mode is None:
            return False

        return super().available and bool((int(mode) >> 10) & 1)

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def native_value(self) -> int:
        return int(self._value)

    async def async_set_native_value(self, value: int) -> None:
        await self._async_write(float(int(value)))


class SolarEdgeGlobalPowerControlNumber(SolarEdgeNumberBase):
    """A number over the global dynamic power control block."""

    @property
    def block(self):
        return self._platform.global_power_control


class SolarEdgeActivePowerLimitSet(SolarEdgeGlobalPowerControlNumber):
    """Global Dynamic Power Control: Set Inverter Active Power Limit"""

    native_unit_of_measurement = PERCENTAGE
    native_min_value = 0
    native_max_value = 100
    mode = "slider"
    icon = "mdi:percent"

    _field = "power_limit"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_active_power_limit_set"

    @property
    def name(self) -> str:
        return "Active Power Limit"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self._platform.device.has_block("global_power_control") is not False

    @property
    def available(self) -> bool:
        value = self._value
        return super().available and value is not None and 0 <= value <= 100

    @property
    def native_value(self) -> int:
        return self._value

    async def async_set_native_value(self, value: int) -> None:
        await self._async_write(int(value))


class SolarEdgeCosPhiSet(SolarEdgeGlobalPowerControlNumber):
    """Global Dynamic Power Control: Set Inverter CosPhi"""

    native_min_value = -1.0
    native_max_value = 1.0
    native_step = 0.1
    mode = "slider"
    icon = "mdi:angle-acute"

    _field = "cos_phi"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_cosphi_set"

    @property
    def name(self) -> str:
        return "CosPhi"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        value = self._value
        return super().available and value is not None and -1.0 <= value <= 1.0

    @property
    def native_value(self) -> float:
        return round(self._value, 1)

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(float(value))


class SolarEdgePowerReduce(SolarEdgeNumberBase):
    """Limits the inverter's maximum output power from 0-100%"""

    native_unit_of_measurement = PERCENTAGE
    native_min_value = 0
    native_max_value = 100
    mode = "slider"
    icon = "mdi:percent"

    _field = "power_reduce"

    @property
    def block(self):
        return self._platform.advanced_power_control

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_power_reduce"

    @property
    def name(self) -> str:
        return "Power Reduce"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        value = self._value
        return super().available and value is not None and 0 <= value <= 100

    @property
    def native_value(self) -> int:
        return round(self._value, 0)

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(float(value))


class SolarEdgeCurrentLimit(SolarEdgeNumberBase):
    """Limits the inverter's maximum output current."""

    native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    native_min_value = 0
    native_max_value = 256
    icon = "mdi:current-ac"

    _field = "max_current"

    @property
    def block(self):
        # Lives in the second power control block, past the 125-register
        # ceiling that splits the two.
        return self._platform.advanced_power_control_2

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_max_current"

    @property
    def name(self) -> str:
        return "Current Limit"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        value = self._value
        return super().available and value is not None and 0 <= value <= 256

    @property
    def native_value(self) -> int:
        return round(self._value, 0)

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(float(value))
