"""The Home Assistant side of a SolarEdge Modbus installation.

The hub owns one ``ModbusConnection`` and hands a ``ModbusUnit`` to each
inverter's device object. Everything about SolarEdge's register map lives in
the ``solaredge`` package next door; what is left here is Home Assistant:
device registry entries, options, repair issues, and turning the library's
errors into the ones the coordinator expects.
"""

from __future__ import annotations

import asyncio
import logging

from awesomeversion import AwesomeVersion
from awesomeversion.exceptions import (
    AwesomeVersionCompareException,
    AwesomeVersionStrategyException,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity import DeviceInfo
from modbus_connection import (
    ModbusConnection,
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusTcpParams,
    ModbusTimeoutError,
)
from modbus_connection.model import Component
from modbus_connection.model.sunspec import SunSpecMapShiftError

from .const import (
    DETECT_EVSE_REGEX,
    DOMAIN,
    STATUS_VENDOR4_VERSION,
    ConfDefaultFlag,
    ConfDefaultInt,
    ConfDefaultStr,
    ConfName,
    ModbusDefaults,
    RetrySettings,
    SolarEdgeTimeouts,
)
from .solaredge import (
    BatteryDevice,
    DeviceInvalid,
    MeterDevice,
    SolarEdgeDevice,
    SolarEdgeOptions,
)

_LOGGER = logging.getLogger(__name__)

# Which optional block maps onto which repair issue. The two power-control
# blocks are the slow ones, and the only two the integration warns about.
_TIMEOUT_ISSUES = {
    "global_power_control": "detect_timeout_gpc",
    "advanced_power_control": "detect_timeout_apc",
    "advanced_power_control_2": "detect_timeout_apc",
}


class SolarEdgeException(Exception):
    """Base class for other exceptions"""


class HubInitFailed(SolarEdgeException):
    """Raised when an error happens during init"""


class DataUpdateFailed(SolarEdgeException):
    """Raised when an update cycle fails"""


class ModbusMapShifted(SolarEdgeException):
    """Raised when the device moved its SunSpec models under us."""


class SolarEdgeModbusMultiHub:
    """One Modbus/TCP link, and every SolarEdge device reachable over it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        entry_data,
        entry_options,
    ):
        """Initialize the Modbus hub."""
        self._hass = hass
        self._yaml_config = hass.data[DOMAIN]["yaml"]
        self._name = entry_data[CONF_NAME]
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        self._entry_id = entry_id
        self._inverter_list = entry_data.get(
            ConfName.DEVICE_LIST, [ConfDefaultStr.DEVICE_LIST]
        )
        self._detect_meters = entry_options.get(
            ConfName.DETECT_METERS, bool(ConfDefaultFlag.DETECT_METERS)
        )
        self._detect_batteries = entry_options.get(
            ConfName.DETECT_BATTERIES, bool(ConfDefaultFlag.DETECT_BATTERIES)
        )
        self._detect_extras = entry_options.get(
            ConfName.DETECT_EXTRAS, bool(ConfDefaultFlag.DETECT_EXTRAS)
        )
        self._keep_modbus_open = entry_options.get(
            ConfName.KEEP_MODBUS_OPEN, bool(ConfDefaultFlag.KEEP_MODBUS_OPEN)
        )
        self._adv_storage_control = entry_options.get(
            ConfName.ADV_STORAGE_CONTROL, bool(ConfDefaultFlag.ADV_STORAGE_CONTROL)
        )
        self._adv_site_limit_control = entry_options.get(
            ConfName.ADV_SITE_LIMIT_CONTROL,
            bool(ConfDefaultFlag.ADV_SITE_LIMIT_CONTROL),
        )
        self._allow_battery_energy_reset = entry_options.get(
            ConfName.ALLOW_BATTERY_ENERGY_RESET,
            bool(ConfDefaultFlag.ALLOW_BATTERY_ENERGY_RESET),
        )
        self._sleep_after_write = entry_options.get(
            ConfName.SLEEP_AFTER_WRITE, ConfDefaultInt.SLEEP_AFTER_WRITE
        )
        self._battery_rating_adjust = entry_options.get(
            ConfName.BATTERY_RATING_ADJUST, ConfDefaultInt.BATTERY_RATING_ADJUST
        )
        self._battery_energy_reset_cycles = entry_options.get(
            ConfName.BATTERY_ENERGY_RESET_CYCLES,
            ConfDefaultInt.BATTERY_ENERGY_RESET_CYCLES,
        )
        self._retry_limit = self._yaml_config.get("retry", {}).get(
            "limit", RetrySettings.Limit
        )
        modbus_config = self._yaml_config.get("modbus", {})
        self._mb_timeout = modbus_config.get("timeout", ModbusDefaults.Timeout)
        self._mb_message_spacing = modbus_config.get("message_spacing", 0.0)
        for retired in ("retries", "reconnect_delay", "reconnect_delay_max"):
            if retired in modbus_config:
                _LOGGER.warning(
                    "The modbus.%s YAML option no longer does anything: "
                    "modbus-connection reconnects on demand and does not retry "
                    "individual requests. Please remove it.",
                    retired,
                )

        self._id = entry_data[CONF_NAME].lower()
        self.inverters: list[SolarEdgeInverter] = []
        self.meters: list[SolarEdgeMeter] = []
        self.batteries: list[SolarEdgeBattery] = []
        self.evses: list[SolarEdgeEVSE] = []
        self.has_write = None

        self._initalized = False
        self._online = True
        self._timeout_counter = 0

        self._connection = ModbusConnection(
            ModbusTcpParams(host=self._host, port=self._port),
            timeout=self._mb_timeout,
            message_spacing=self._mb_message_spacing,
        )

        _LOGGER.debug(
            (
                f"{DOMAIN} configuration: "
                f"inverter_list={self._inverter_list}, "
                f"detect_meters={self._detect_meters}, "
                f"detect_batteries={self._detect_batteries}, "
                f"detect_extras={self._detect_extras}, "
                f"keep_modbus_open={self._keep_modbus_open}, "
                f"adv_storage_control={self._adv_storage_control}, "
                f"adv_site_limit_control={self._adv_site_limit_control}, "
                f"allow_battery_energy_reset={self._allow_battery_energy_reset}, "
                f"sleep_after_write={self._sleep_after_write}, "
                f"battery_rating_adjust={self._battery_rating_adjust}, "
            ),
        )

    @property
    def _library_options(self) -> SolarEdgeOptions:
        """The options the device library needs to know about."""
        return SolarEdgeOptions(
            detect_meters=self._detect_meters,
            detect_batteries=self._detect_batteries,
            detect_extras=self._detect_extras,
            storage_control=self._adv_storage_control,
            site_limit_control=self._adv_site_limit_control,
            slow_block_timeout=SolarEdgeTimeouts.Read / 1000,
        )

    async def _async_init_solaredge(self) -> None:
        """Discover every device on the link and read it once."""

        # Setup runs again on every poll until it succeeds, so a run that got
        # part way through has to start from an empty slate or the devices it
        # did find would be added twice.
        self.inverters.clear()
        self.meters.clear()
        self.batteries.clear()
        self.evses.clear()

        # Requests connect on demand, so this is only to fail fast: an
        # unreachable host should say so before setup starts walking unit IDs.
        await self.connect()

        if self.option_storage_control:
            _LOGGER.warning(
                (
                    "Power Control Options: Storage Control is enabled. "
                    "Use at your own risk! "
                    "Adjustable parameters in Modbus registers are intended for "
                    "long-term storage. Periodic changes may damage the flash memory."
                ),
            )

        if self.option_site_limit_control:
            _LOGGER.warning(
                (
                    "Power Control Options: Site Limit Control is enabled. "
                    "Use at your own risk! "
                    "Adjustable parameters in Modbus registers are intended for "
                    "long-term storage. Periodic changes may damage the flash memory."
                ),
            )

        for unit_id in self._inverter_list:
            _LOGGER.debug(f"Looking for inverter at {self.hub_host} ID {unit_id}")
            device = SolarEdgeDevice(
                self._connection.for_unit(unit_id), unit_id, self._library_options
            )

            try:
                if await self._async_is_evse(device, unit_id):
                    continue
                await device.async_setup()
                await device.async_add_batteries()

            except DeviceInvalid as e:
                # Inverters are mandatory.
                _LOGGER.error(f"Inverter at {self.hub_host} ID {unit_id}: {e}")
                raise HubInitFailed(f"{e}")

            inverter = SolarEdgeInverter(unit_id, self, device)
            self.inverters.append(inverter)

            for meter in device.meters:
                self.meters.append(SolarEdgeMeter(self, inverter, meter))
                _LOGGER.debug(f"Found I{unit_id}M{meter.meter_id}")

            for battery in device.batteries:
                new_battery = SolarEdgeBattery(self, inverter, battery)
                if any(b.serial == new_battery.serial for b in self.batteries):
                    # Two batteries reporting one serial means the inverter is
                    # mirroring a slot; keeping both would collide in the
                    # device registry.
                    _LOGGER.warning(
                        f"Duplicate serial {new_battery.serial} on {new_battery.tag}"
                    )
                    continue
                self.batteries.append(new_battery)
                _LOGGER.debug(f"Found I{unit_id}B{battery.battery_id}")

        self._warn_duplicate_meter_serials()

        for inverter in self.inverters:
            await inverter.async_update()
        for evse in self.evses:
            await evse.async_update()

        self.initalized = True

    async def _async_is_evse(self, device: SolarEdgeDevice, unit_id: int) -> bool:
        """Read the identity block and decide whether this unit is an EVSE.

        An EVSE answers the same SunSpec identity block as an inverter but has
        none of the inverter models behind it, so it has to be recognised from
        its model name before setup goes looking for them.
        """
        common = await device.async_read_identity()
        if not DETECT_EVSE_REGEX.match(common.md or ""):
            return False
        _LOGGER.debug(f"Device model matches EVSE at {self.hub_host} ID {unit_id}")
        self.evses.append(SolarEdgeEVSE(unit_id, self, device))
        return True

    def _warn_duplicate_meter_serials(self) -> None:
        """Warn about meters that report a serial another meter already used.

        Unlike batteries these are kept: SolarEdge ships meters that genuinely
        share a serial, and dropping one would take a real meter's entities
        away (see upstream PR#412).
        """
        seen: dict[str | None, str] = {}
        for meter in self.meters:
            if meter.serial in seen:
                _LOGGER.warning(
                    f"Duplicate serial {meter.serial} on {meter.tag} "
                    f"(also {seen[meter.serial]})"
                )
            seen[meter.serial] = meter.tag

    async def async_refresh_modbus_data(self) -> bool:
        """Refresh modbus data from inverters."""

        if not self.initalized:
            try:
                async with asyncio.timeout(self.coordinator_timeout):
                    await self._async_init_solaredge()

            except (ModbusError, TimeoutError) as e:
                await self.disconnect()
                self._async_create_issue("check_configuration", fixable=True)
                raise HubInitFailed(f"Setup failed: {e}")

            ir.async_delete_issue(self._hass, DOMAIN, "check_configuration")

            if not self.keep_modbus_open and not self.has_write:
                await self.disconnect()

            return True

        if not self.online:
            ir.async_delete_issue(self._hass, DOMAIN, "check_configuration")

        self.online = True

        try:
            async with asyncio.timeout(self.coordinator_timeout):
                for inverter in self.inverters:
                    await inverter.async_update()
                for evse in self.evses:
                    await evse.async_update()

        except SunSpecMapShiftError as e:
            # The device rearranged its model chain, so every component is at a
            # stale address. Setup has to run again to rescan.
            await self.disconnect()
            raise ModbusMapShifted(f"{e}")

        except ModbusConnectionError as e:
            self.online = False
            await self.disconnect()
            self._async_create_issue("check_configuration", fixable=True)
            raise DataUpdateFailed(f"Connection failed: {e}")

        except ModbusExceptionError as e:
            await self.disconnect()
            raise DataUpdateFailed(f"Update failed: {e}")

        except TimeoutError as e:
            # Covers both the per-request ModbusTimeoutError and the overall
            # coordinator budget; ModbusTimeoutError is a builtin TimeoutError.
            await self.disconnect()
            self._timeout_counter += 1

            _LOGGER.debug(
                f"Refresh timeout {self._timeout_counter} limit {self._retry_limit}"
            )

            if self._timeout_counter >= self._retry_limit:
                self._timeout_counter = 0
                raise TimeoutError

            raise DataUpdateFailed(f"Timeout error: {e}")

        except ModbusError as e:
            await self.disconnect()
            raise DataUpdateFailed(f"Modbus error: {e}")

        if self._timeout_counter > 0:
            _LOGGER.debug(
                f"Timeout count {self._timeout_counter} limit {self._retry_limit}"
            )
            self._timeout_counter = 0

        self._raise_issues_for_slow_blocks()

        if not self.keep_modbus_open and not self.has_write:
            await self.disconnect()

        return True

    def _raise_issues_for_slow_blocks(self) -> None:
        """Warn about optional blocks the inverter was too slow to answer.

        An inverter that starts answering again clears its warning, which the
        version this replaced never did.
        """
        slow = {
            _TIMEOUT_ISSUES[name]
            for inverter in self.inverters
            for name in inverter.device.timed_out_blocks()
            if name in _TIMEOUT_ISSUES
        }
        for issue in set(_TIMEOUT_ISSUES.values()):
            if issue in slow:
                self._async_create_issue(issue, fixable=False)
            else:
                ir.async_delete_issue(self._hass, DOMAIN, issue)

    def _async_create_issue(self, translation_key: str, *, fixable: bool) -> None:
        """Raise one of this integration's repair issues."""
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            translation_key,
            is_fixable=fixable,
            severity=ir.IssueSeverity.ERROR if fixable else ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            data={"entry_id": self._entry_id},
        )

    async def connect(self) -> None:
        """Open the link.

        Requests connect on demand, so this exists only so setup can fail with
        a connection error before it starts walking unit IDs.
        """
        await self._connection.connect()

    async def disconnect(self) -> None:
        """Drop the link. The next request opens a new one."""
        _LOGGER.debug(f"Disconnecting from {self._host}:{self._port}.")
        await self._connection.disconnect()

    async def shutdown(self) -> None:
        """Shut down the hub and close the connection for good."""
        self.online = False
        await self._connection.close()

    async def async_write(self, component: Component, field: str, value) -> None:
        """Write one field."""
        await self._async_guarded_write(field, lambda: component.write(field, value))

    async def _async_guarded_write(self, field: str, write) -> None:
        """Run a write, holding off the coordinator while it happens.

        The connection serializes requests already, but a write and the poll
        that follows it must not interleave: SolarEdge's control registers are
        flash-backed and take a moment to settle, so a poll racing the write
        reads the old value back and the entity flickers.
        """
        self.has_write = field

        try:
            await write()

            if self.sleep_after_write > 0:
                _LOGGER.debug(
                    f"Sleep {self.sleep_after_write} seconds after write {field}."
                )
                await asyncio.sleep(self.sleep_after_write)

            _LOGGER.debug(f"Finished with write {field}.")

        except ValueError as e:
            raise HomeAssistantError(f"Value invalid for {field}: {e}")

        except ModbusExceptionError as e:
            raise HomeAssistantError(f"Device rejected the write to {field}: {e}")

        except ModbusTimeoutError:
            await self.disconnect()
            raise HomeAssistantError(f"No response writing {field}.")

        except ModbusError as e:
            await self.disconnect()
            raise HomeAssistantError(f"Error writing {field}: {e}")

        finally:
            self.has_write = None

    @property
    def online(self):
        return self._online

    @online.setter
    def online(self, value: bool) -> None:
        self._online = bool(value)

    @property
    def initalized(self):
        return self._initalized

    @initalized.setter
    def initalized(self, value: bool) -> None:
        self._initalized = bool(value)

    @property
    def name(self):
        """Return the name of this hub."""
        return self._name

    @property
    def hub_id(self) -> str:
        """Return the ID of this hub."""
        return self._id

    @property
    def hub_host(self) -> str:
        """Return the modbus client host."""
        return self._host

    @property
    def hub_port(self) -> int:
        """Return the modbus client port."""
        return self._port

    @property
    def option_storage_control(self) -> bool:
        return self._adv_storage_control

    @property
    def option_site_limit_control(self) -> bool:
        return self._adv_site_limit_control

    @property
    def option_detect_extras(self) -> bool:
        return self._detect_extras

    @property
    def keep_modbus_open(self) -> bool:
        return self._keep_modbus_open

    @keep_modbus_open.setter
    def keep_modbus_open(self, value: bool) -> None:
        self._keep_modbus_open = bool(value)
        _LOGGER.debug(f"keep_modbus_open={self._keep_modbus_open}")

    @property
    def allow_battery_energy_reset(self) -> bool:
        return self._allow_battery_energy_reset

    @property
    def battery_rating_adjust(self) -> int:
        return (self._battery_rating_adjust + 100) / 100

    @property
    def battery_energy_reset_cycles(self) -> int:
        return self._battery_energy_reset_cycles

    @property
    def number_of_meters(self) -> int:
        return len(self.meters)

    @property
    def number_of_batteries(self) -> int:
        return len(self.batteries)

    @property
    def number_of_inverters(self) -> int:
        return len(self._inverter_list)

    @property
    def sleep_after_write(self) -> int:
        return self._sleep_after_write

    @property
    def coordinator_timeout(self) -> int:
        if not self.initalized:
            this_timeout = SolarEdgeTimeouts.Inverter * self.number_of_inverters
            this_timeout += SolarEdgeTimeouts.Init * self.number_of_inverters
            this_timeout += (SolarEdgeTimeouts.Device * 2) * 3  # max 3 per inverter
            this_timeout += (SolarEdgeTimeouts.Device * 2) * 2  # max 2 per inverter
            if self.option_detect_extras:
                this_timeout += (SolarEdgeTimeouts.Read * 3) * self.number_of_inverters

        else:
            this_timeout = SolarEdgeTimeouts.Inverter * self.number_of_inverters
            this_timeout += SolarEdgeTimeouts.Device * self.number_of_meters
            this_timeout += SolarEdgeTimeouts.Device * self.number_of_batteries
            if self.option_detect_extras:
                this_timeout += (SolarEdgeTimeouts.Read * 3) * self.number_of_inverters

        this_timeout = this_timeout / 1000

        _LOGGER.debug(f"coordinator timeout is {this_timeout}")
        return this_timeout

    @property
    def is_connected(self) -> bool:
        """Check modbus client connection status."""
        return self._connection.connected


class SolarEdgeInverter:
    """A SolarEdge inverter, as Home Assistant sees it."""

    def __init__(
        self, unit_id: int, hub: SolarEdgeModbusMultiHub, device: SolarEdgeDevice
    ) -> None:
        self.inverter_unit_id = unit_id
        self.hub = hub
        self.device = device
        self.has_parent = False
        self.mmppt_units: list[SolarEdgeMMPPTUnit] = []
        self._use_status_vendor4 = False

        self.manufacturer = device.common.mn
        self.model = device.common.md
        self.option = device.common.opt
        self.serial = device.common.sn
        self.device_address = device.common.da
        self.name = f"{hub.hub_id.capitalize()} I{unit_id}"
        self.uid_base = f"{self.model}_{self.serial}"

        try:
            this_ver = AwesomeVersion(device.common.vr)
            self._use_status_vendor4 = this_ver >= AwesomeVersion(
                STATUS_VENDOR4_VERSION
            )
        except (AwesomeVersionCompareException, AwesomeVersionStrategyException) as e:
            _LOGGER.error(
                f"Error checking inverter version: {e}. Please report this issue."
            )

        if device.mppt is not None:
            self.mmppt_units = [
                SolarEdgeMMPPTUnit(self, hub, index)
                for index in range(int(device.mppt.n or 0))
            ]

    async def async_update(self) -> None:
        """Refresh this inverter and everything behind it."""
        await self.device.async_update()
        self._resize_mmppt_units()

    def _resize_mmppt_units(self) -> None:
        """Track a module count that changed between polls.

        The count is read every cycle, so an inverter that reports a different
        number of modules changes the list under us. Entities for a module that
        went away read ``None`` rather than raising.
        """
        if self.mppt is None:
            return
        wanted = len(self.mppt.module)
        while len(self.mmppt_units) < wanted:
            self.mmppt_units.append(
                SolarEdgeMMPPTUnit(self, self.hub, len(self.mmppt_units))
            )

    async def async_write(self, component: Component | None, field: str, value) -> None:
        """Write a field on one of this inverter's control blocks."""
        if component is None:
            raise HomeAssistantError(
                f"Inverter ID {self.inverter_unit_id} does not serve {field}."
            )
        await self.hub.async_write(component, field, value)

    @property
    def common(self):
        """The SunSpec identity block."""
        return self.device.common

    @property
    def model_block(self):
        """The SunSpec inverter model (101/102/103)."""
        return self.device.inverter

    @property
    def mppt(self):
        """The multiple MPPT model, or None."""
        return self.device.mppt

    @property
    def storage_control(self):
        return self.device.storage_control

    @property
    def site_limit(self):
        return self.device.site_limit

    @property
    def ext_prod_max(self):
        return self.device.ext_prod_max

    @property
    def global_power_control(self):
        return self.device.global_power_control

    @property
    def advanced_power_control(self):
        return self.device.advanced_power_control

    @property
    def advanced_power_control_2(self):
        return self.device.advanced_power_control_2

    @property
    def grid_status(self):
        return self.device.grid_status

    @property
    def status_vendor4(self):
        return self.device.status_vendor4

    @property
    def has_battery(self) -> bool:
        return bool(self.device.batteries)

    @property
    def online(self) -> bool:
        """Device is online."""
        return self.hub.online

    @property
    def fw_version(self) -> str | None:
        return self.device.common.vr

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.uid_base)},
            name=self.name,
            manufacturer=self.manufacturer,
            model=self.model,
            serial_number=self.serial,
            sw_version=self.fw_version,
            hw_version=self.option,
        )

    @property
    def is_mmppt(self) -> bool:
        return self.device.is_mmppt

    @property
    def use_status_vendor4(self) -> bool:
        return self._use_status_vendor4


class SolarEdgeMMPPTUnit:
    """One MPPT module of a Synergy inverter."""

    def __init__(
        self, inverter: SolarEdgeInverter, hub: SolarEdgeModbusMultiHub, unit: int
    ) -> None:
        self.inverter = inverter
        self.hub = hub
        self.unit = unit

    @property
    def module(self):
        """This module's component, or None if the inverter stopped reporting it."""
        mppt = self.inverter.mppt
        if mppt is None or self.unit >= len(mppt.module):
            return None
        return mppt.module[self.unit]

    @property
    def online(self) -> bool:
        """Device is online."""
        return self.hub.online and self.inverter.is_mmppt and self.inverter.online

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.inverter.uid_base, f"mmppt_{self.unit}")},
            name=f"{self.inverter.name} MPPT{self.unit}",
            manufacturer=self.inverter.manufacturer,
            model=self.inverter.model,
            hw_version=f"ID {self.mmppt_id}",
            serial_number=f"{self.mmppt_idstr}",
            via_device=(DOMAIN, self.inverter.uid_base),
        )

    @property
    def mmppt_id(self):
        module = self.module
        return None if module is None else module.id

    @property
    def mmppt_idstr(self):
        module = self.module
        return None if module is None else module.id_str


class SolarEdgeMeter:
    """A meter behind an inverter, as Home Assistant sees it."""

    def __init__(
        self,
        hub: SolarEdgeModbusMultiHub,
        inverter: SolarEdgeInverter,
        meter: MeterDevice,
    ) -> None:
        self.hub = hub
        self.inverter = inverter
        self.inverter_unit_id = inverter.inverter_unit_id
        self.meter_id = meter.meter_id
        self.device = meter
        self.has_parent = True

        self.manufacturer = meter.common.mn
        self.model = meter.common.md
        self.option = meter.common.opt
        self.fw_version = meter.common.vr
        self.serial = meter.common.sn
        self.device_address = meter.common.da
        self.name = (
            f"{hub.hub_id.capitalize()} I{self.inverter_unit_id} M{self.meter_id}"
        )
        self.uid_base = f"{inverter.uid_base}_M{self.meter_id}"

    @property
    def tag(self) -> str:
        """Short identifier used in log messages."""
        return f"I{self.inverter_unit_id}M{self.meter_id}"

    @property
    def common(self):
        """The SunSpec identity block."""
        return self.device.common

    @property
    def model_block(self):
        """The SunSpec meter model (201/202/203/204)."""
        return self.device.meter

    @property
    def online(self) -> bool:
        """Device is online."""
        return self.hub.online

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.uid_base)},
            name=self.name,
            manufacturer=self.manufacturer,
            model=self.model,
            serial_number=self.serial,
            sw_version=self.fw_version,
            hw_version=self.option,
            via_device=(DOMAIN, self.inverter.uid_base),
        )


class SolarEdgeBattery:
    """A battery behind an inverter, as Home Assistant sees it."""

    def __init__(
        self,
        hub: SolarEdgeModbusMultiHub,
        inverter: SolarEdgeInverter,
        battery: BatteryDevice,
    ) -> None:
        self.hub = hub
        self.inverter = inverter
        self.inverter_unit_id = inverter.inverter_unit_id
        self.battery_id = battery.battery_id
        self.device = battery
        self.has_parent = True

        block = battery.battery
        serial = block.serial or ""
        # SolarEdge appends the serial number to both identity strings on some
        # battery models; strip it back off so the device page reads sensibly.
        self.manufacturer = (block.manufacturer or "").removesuffix(serial)
        self.model = (block.model or "").removesuffix(serial)
        self.option = ""
        self.fw_version = block.version
        self.serial = serial
        self.device_address = block.device_address
        self.name = (
            f"{hub.hub_id.capitalize()} I{self.inverter_unit_id} B{self.battery_id}"
        )
        self.uid_base = f"{inverter.uid_base}_B{self.battery_id}"

    @property
    def tag(self) -> str:
        """Short identifier used in log messages."""
        return f"I{self.inverter_unit_id}B{self.battery_id}"

    @property
    def model_block(self):
        """The battery's proprietary register block."""
        return self.device.battery

    @property
    def online(self) -> bool:
        """Device is online."""
        return self.hub.online

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.uid_base)},
            name=self.name,
            manufacturer=self.manufacturer,
            model=self.model,
            serial_number=self.serial,
            sw_version=self.fw_version,
            via_device=(DOMAIN, self.inverter.uid_base),
        )

    @property
    def allow_battery_energy_reset(self) -> bool:
        return self.hub.allow_battery_energy_reset

    @property
    def battery_rating_adjust(self) -> int:
        return self.hub.battery_rating_adjust

    @property
    def battery_energy_reset_cycles(self) -> int:
        return self.hub.battery_energy_reset_cycles


class SolarEdgeEVSE:
    """A SolarEdge EVSE: the SunSpec identity block and nothing else."""

    def __init__(
        self, unit_id: int, hub: SolarEdgeModbusMultiHub, device: SolarEdgeDevice
    ) -> None:
        self.evse_unit_id = unit_id
        self.hub = hub
        self.device = device
        self.has_parent = False

        self.manufacturer = device.common.mn
        self.model = device.common.md
        self.option = device.common.opt
        self.serial = device.common.sn
        self.device_address = device.common.da
        self.name = f"{hub.hub_id.capitalize()} E{unit_id}"
        self.uid_base = f"{self.model}_{self.serial}"

    async def async_update(self) -> None:
        """Refresh the identity block, which is where the firmware version is."""
        await self.device.async_update()

    @property
    def common(self):
        """The SunSpec identity block."""
        return self.device.common

    @property
    def online(self) -> bool:
        """Device is online."""
        return self.hub.online

    @property
    def fw_version(self) -> str | None:
        return self.device.common.vr

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.uid_base)},
            name=self.name,
            manufacturer=self.manufacturer,
            model=self.model,
            serial_number=self.serial,
            sw_version=self.fw_version,
            hw_version=self.option,
        )
