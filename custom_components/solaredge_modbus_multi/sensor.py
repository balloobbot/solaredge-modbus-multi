from __future__ import annotations

import datetime
import logging
import re

from awesomeversion import AwesomeVersion
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
)
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BATTERY_STATUS,
    BATTERY_STATUS_TEXT,
    DEVICE_STATUS,
    DEVICE_STATUS_TEXT,
    DOMAIN,
    ENERGY_VOLT_AMPERE_HOUR,
    ENERGY_VOLT_AMPERE_REACTIVE_HOUR,
    INVERTED_POWER_VERSION,
    METER_EVENTS,
    MMPPT_EVENTS,
    RRCR_STATUS,
    SUNSPEC_DID,
    VENDOR4_STATUS,
    VENDOR_STATUS,
    BatteryLimit,
)

_LOGGER = logging.getLogger(__name__)

# Which model field backs a phase, per device kind. SunSpec names the same
# quantity differently on an inverter and on a meter, and a meter carries both
# line-to-neutral and line-to-line voltages where an inverter carries one set.
_INVERTER_VOLTAGE = {
    "AB": "pp_vph_ab",
    "BC": "pp_vph_bc",
    "CA": "pp_vph_ca",
    "AN": "ph_vph_a",
    "BN": "ph_vph_b",
    "CN": "ph_vph_c",
}
_METER_VOLTAGE = {
    "LN": "ph_v",
    "AN": "ph_vph_a",
    "BN": "ph_vph_b",
    "CN": "ph_vph_c",
    "LL": "ppv",
    "AB": "ph_vph_ab",
    "BC": "ph_vph_bc",
    "CA": "ph_vph_ca",
}
_CURRENT_PHASES = {None: "a", "A": "aph_a", "B": "aph_b", "C": "aph_c"}
_POWER_PHASES = {None: "w", "A": "wph_a", "B": "wph_b", "C": "wph_c"}
_VA_PHASES = {None: "va", "A": "v_aph_a", "B": "v_aph_b", "C": "v_aph_c"}
_VAR_PHASES = {None: "var", "A": "va_rph_a", "B": "va_rph_b", "C": "va_rph_c"}
_PF_PHASES = {None: "pf", "A": "p_fph_a", "B": "p_fph_b", "C": "p_fph_c"}

_PHASE_SUFFIX = {"": "", "_A": "_ph_a", "_B": "_ph_b", "_C": "_ph_c"}


def _accumulator_field(prefix: str, phase: str) -> str:
    """Resolve an energy accumulator field from its ``Exported_A`` style phase.

    ``prefix`` is the field stem without direction, e.g. ``tot_wh``; the phase
    carries both the direction and which line it is, e.g. ``Imported_B``.
    """
    direction, _, line = phase.partition("_")
    stem = {
        "Exported": "exp",
        "Imported": "imp",
        "Import": "imp",
        "Export": "exp",
    }[direction]
    return f"{prefix}_{stem}{_PHASE_SUFFIX['_' + line if line else '']}"


def _quadrant_field(phase: str) -> str:
    """Resolve a reactive energy field from its ``Import_Q1_A`` style phase."""
    direction, quadrant, *line = phase.split("_")
    stem = "imp" if direction == "Import" else "exp"
    suffix = _PHASE_SUFFIX[f"_{line[0]}"] if line else ""
    return f"tot_v_arh_{stem}_{quadrant.lower()}{suffix}"


def _precision(scale_factor: int | None, default: int = 1) -> int:
    """Display precision, taken from the device's own scale factor.

    A SunSpec point scaled by ``10**-2`` is meaningful to two decimals and no
    further, so the exponent is exactly the precision to show. An inverter that
    does not report the exponent falls back to ``default``.
    """
    return default if scale_factor is None else abs(scale_factor)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][config_entry.entry_id]["hub"]
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []

    for inverter in hub.inverters:
        entities.append(SolarEdgeLastUpdate(inverter, config_entry, coordinator))
        entities.append(SolarEdgeDevice(inverter, config_entry, coordinator))
        entities.append(Version(inverter, config_entry, coordinator))
        entities.append(SolarEdgeInverterStatus(inverter, config_entry, coordinator))
        entities.append(StatusVendor(inverter, config_entry, coordinator))
        if inverter.use_status_vendor4:
            entities.append(StatusVendor4(inverter, config_entry, coordinator))
        entities.append(ACCurrentSensor(inverter, config_entry, coordinator))
        entities.append(ACCurrentSensor(inverter, config_entry, coordinator, "A"))
        entities.append(ACCurrentSensor(inverter, config_entry, coordinator, "B"))
        entities.append(ACCurrentSensor(inverter, config_entry, coordinator, "C"))
        entities.append(VoltageSensor(inverter, config_entry, coordinator, "AB"))
        entities.append(VoltageSensor(inverter, config_entry, coordinator, "BC"))
        entities.append(VoltageSensor(inverter, config_entry, coordinator, "CA"))
        entities.append(VoltageSensor(inverter, config_entry, coordinator, "AN"))
        entities.append(VoltageSensor(inverter, config_entry, coordinator, "BN"))
        entities.append(VoltageSensor(inverter, config_entry, coordinator, "CN"))
        entities.append(ACPower(inverter, config_entry, coordinator))
        entities.append(ACFrequency(inverter, config_entry, coordinator))
        entities.append(ACVoltAmp(inverter, config_entry, coordinator))
        entities.append(ACVoltAmpReactive(inverter, config_entry, coordinator))
        entities.append(ACPowerFactor(inverter, config_entry, coordinator))
        entities.append(SolarEdgeACEnergy(inverter, config_entry, coordinator))
        entities.append(DCCurrent(inverter, config_entry, coordinator))
        entities.append(DCVoltage(inverter, config_entry, coordinator))
        entities.append(DCPower(inverter, config_entry, coordinator))
        entities.append(HeatSinkTemperature(inverter, config_entry, coordinator))

        if hub.option_detect_extras and inverter.device.has_block(
            "global_power_control"
        ):
            entities.append(SolarEdgeRRCR(inverter, config_entry, coordinator))
            entities.append(
                SolarEdgeActivePowerLimit(inverter, config_entry, coordinator)
            )
            entities.append(SolarEdgeCosPhi(inverter, config_entry, coordinator))

        if hub.option_detect_extras and inverter.device.has_block(
            "advanced_power_control"
        ):
            entities.append(
                SolarEdgeCommitControlSettings(inverter, config_entry, coordinator)
            )
            entities.append(
                SolarEdgeDefaultControlSettings(inverter, config_entry, coordinator)
            )

        if inverter.is_mmppt:
            entities.append(SolarEdgeMMPPTEvents(inverter, config_entry, coordinator))

            for mmppt_unit in inverter.mmppt_units:
                entities.append(
                    SolarEdgeDCCurrentMMPPT(mmppt_unit, config_entry, coordinator)
                )
                entities.append(
                    SolarEdgeDCVoltageMMPPT(mmppt_unit, config_entry, coordinator)
                )
                entities.append(
                    SolarEdgeDCPowerMMPPT(mmppt_unit, config_entry, coordinator)
                )
                entities.append(
                    SolarEdgeTemperatureMMPPT(mmppt_unit, config_entry, coordinator)
                )

    for meter in hub.meters:
        entities.append(SolarEdgeLastUpdate(meter, config_entry, coordinator))
        entities.append(SolarEdgeDevice(meter, config_entry, coordinator))
        entities.append(Version(meter, config_entry, coordinator))
        entities.append(MeterEvents(meter, config_entry, coordinator))
        entities.append(ACCurrentSensor(meter, config_entry, coordinator))
        entities.append(ACCurrentSensor(meter, config_entry, coordinator, "A"))
        entities.append(ACCurrentSensor(meter, config_entry, coordinator, "B"))
        entities.append(ACCurrentSensor(meter, config_entry, coordinator, "C"))
        entities.append(VoltageSensor(meter, config_entry, coordinator, "LN"))
        entities.append(VoltageSensor(meter, config_entry, coordinator, "AN"))
        entities.append(VoltageSensor(meter, config_entry, coordinator, "BN"))
        entities.append(VoltageSensor(meter, config_entry, coordinator, "CN"))
        entities.append(VoltageSensor(meter, config_entry, coordinator, "LL"))
        entities.append(VoltageSensor(meter, config_entry, coordinator, "AB"))
        entities.append(VoltageSensor(meter, config_entry, coordinator, "BC"))
        entities.append(VoltageSensor(meter, config_entry, coordinator, "CA"))
        entities.append(ACFrequency(meter, config_entry, coordinator))
        entities.append(ACPower(meter, config_entry, coordinator))
        entities.append(ACPower(meter, config_entry, coordinator, "A"))
        entities.append(ACPower(meter, config_entry, coordinator, "B"))
        entities.append(ACPower(meter, config_entry, coordinator, "C"))
        entities.append(ACPowerInverted(meter, config_entry, coordinator))
        entities.append(ACVoltAmp(meter, config_entry, coordinator))
        entities.append(ACVoltAmp(meter, config_entry, coordinator, "A"))
        entities.append(ACVoltAmp(meter, config_entry, coordinator, "B"))
        entities.append(ACVoltAmp(meter, config_entry, coordinator, "C"))
        entities.append(ACVoltAmpReactive(meter, config_entry, coordinator))
        entities.append(ACVoltAmpReactive(meter, config_entry, coordinator, "A"))
        entities.append(ACVoltAmpReactive(meter, config_entry, coordinator, "B"))
        entities.append(ACVoltAmpReactive(meter, config_entry, coordinator, "C"))
        entities.append(ACPowerFactor(meter, config_entry, coordinator))
        entities.append(ACPowerFactor(meter, config_entry, coordinator, "A"))
        entities.append(ACPowerFactor(meter, config_entry, coordinator, "B"))
        entities.append(ACPowerFactor(meter, config_entry, coordinator, "C"))
        entities.append(SolarEdgeACEnergy(meter, config_entry, coordinator, "Exported"))
        entities.append(
            SolarEdgeACEnergy(meter, config_entry, coordinator, "Exported_A")
        )
        entities.append(
            SolarEdgeACEnergy(meter, config_entry, coordinator, "Exported_B")
        )
        entities.append(
            SolarEdgeACEnergy(meter, config_entry, coordinator, "Exported_C")
        )
        entities.append(SolarEdgeACEnergy(meter, config_entry, coordinator, "Imported"))
        entities.append(
            SolarEdgeACEnergy(meter, config_entry, coordinator, "Imported_A")
        )
        entities.append(
            SolarEdgeACEnergy(meter, config_entry, coordinator, "Imported_B")
        )
        entities.append(
            SolarEdgeACEnergy(meter, config_entry, coordinator, "Imported_C")
        )
        entities.append(MeterVAhIE(meter, config_entry, coordinator, "Exported"))
        entities.append(MeterVAhIE(meter, config_entry, coordinator, "Exported_A"))
        entities.append(MeterVAhIE(meter, config_entry, coordinator, "Exported_B"))
        entities.append(MeterVAhIE(meter, config_entry, coordinator, "Exported_C"))
        entities.append(MeterVAhIE(meter, config_entry, coordinator, "Imported"))
        entities.append(MeterVAhIE(meter, config_entry, coordinator, "Imported_A"))
        entities.append(MeterVAhIE(meter, config_entry, coordinator, "Imported_B"))
        entities.append(MeterVAhIE(meter, config_entry, coordinator, "Imported_C"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Import_Q1"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Import_Q1_A"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Import_Q1_B"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Import_Q1_C"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Import_Q2"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Import_Q2_A"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Import_Q2_B"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Import_Q2_C"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Export_Q3"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Export_Q3_A"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Export_Q3_B"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Export_Q3_C"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Export_Q4"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Export_Q4_A"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Export_Q4_B"))
        entities.append(MetervarhIE(meter, config_entry, coordinator, "Export_Q4_C"))

    for battery in hub.batteries:
        entities.append(SolarEdgeLastUpdate(battery, config_entry, coordinator))
        entities.append(SolarEdgeDevice(battery, config_entry, coordinator))
        entities.append(Version(battery, config_entry, coordinator))
        entities.append(SolarEdgeBatteryAvgTemp(battery, config_entry, coordinator))
        entities.append(SolarEdgeBatteryMaxTemp(battery, config_entry, coordinator))
        entities.append(SolarEdgeBatteryVoltage(battery, config_entry, coordinator))
        entities.append(SolarEdgeBatteryCurrent(battery, config_entry, coordinator))
        entities.append(SolarEdgeBatteryPower(battery, config_entry, coordinator))
        entities.append(
            SolarEdgeBatteryPowerInverted(battery, config_entry, coordinator)
        )
        entities.append(
            SolarEdgeBatteryEnergyExport(battery, config_entry, coordinator)
        )
        entities.append(
            SolarEdgeBatteryEnergyImport(battery, config_entry, coordinator)
        )
        entities.append(SolarEdgeBatteryMaxEnergy(battery, config_entry, coordinator))
        entities.append(
            SolarEdgeBatteryMaxChargePower(battery, config_entry, coordinator)
        )
        entities.append(
            SolarEdgeBatteryMaxDischargePower(battery, config_entry, coordinator)
        )
        entities.append(
            SolarEdgeBatteryMaxChargePeakPower(battery, config_entry, coordinator)
        )
        entities.append(
            SolarEdgeBatteryMaxDischargePeakPower(battery, config_entry, coordinator)
        )
        entities.append(
            SolarEdgeBatteryAvailableEnergy(battery, config_entry, coordinator)
        )
        entities.append(SolarEdgeBatterySOH(battery, config_entry, coordinator))
        entities.append(SolarEdgeBatterySOE(battery, config_entry, coordinator))
        entities.append(SolarEdgeBatteryStatus(battery, config_entry, coordinator))

    for evse in hub.evses:
        entities.append(Version(evse, config_entry, coordinator))

    if entities:
        async_add_entities(entities)


class SolarEdgeSensorBase(CoordinatorEntity, SensorEntity):
    should_poll = False
    suggested_display_precision = None
    _attr_has_entity_name = True

    def __init__(self, platform, config_entry, coordinator):
        super().__init__(coordinator)

        self._platform = platform
        self._config_entry = config_entry

    @property
    def block(self):
        """The component this sensor reads from.

        Values arrive already decoded and scaled: an unimplemented point, an
        out-of-spec scale factor, or an accumulator the device has not started
        counting all read as ``None``, so entities test for that and nothing
        else.
        """
        return self._platform.model_block

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
    def is_total(self) -> bool:
        """Whether this sensor accumulates rather than measures."""
        return self.state_class in (
            SensorStateClass.TOTAL,
            SensorStateClass.TOTAL_INCREASING,
        )

    @property
    def available(self) -> bool:
        # A total that goes unavailable leaves a gap in long-term statistics
        # and the energy dashboard, and a SolarEdge inverter powers down every
        # night, so totals hold their last value instead — whether one device
        # failed its poll or nothing answered at all. The trade is that they
        # never read unavailable, even for an inverter that is gone for good;
        # reporting that is the connectivity and diagnostic entities' job.
        return self.is_total or (super().available and self._platform.online)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class SolarEdgeTotalBase(SolarEdgeSensorBase, RestoreSensor):
    """A lifetime counter, which never publishes an empty reading.

    The value lives in ``_attr_native_value``: a poll that has nothing to
    publish leaves it alone rather than writing ``None``, which would show as
    unknown and gap long-term statistics as badly as unavailable does. A
    SolarEdge inverter powers down every night, so the same value is restored
    across a Home Assistant restart.
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last_data.native_value
        self._process_data()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._process_data()
        super()._handle_coordinator_update()

    def _process_data(self) -> None:
        """Publish what this poll returned, or keep the last total."""
        raise NotImplementedError


class SolarEdgeDevice(SolarEdgeSensorBase):
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_device"

    @property
    def name(self) -> str:
        return "Device"

    @property
    def native_value(self):
        return self._platform.model

    @property
    def extra_state_attributes(self):
        attrs = {}

        rated_energy = getattr(self.block, "rated_energy", None)
        if rated_energy is not None and rated_energy > 0:
            attrs["batt_rated_energy"] = rated_energy

        attrs["device_id"] = self._platform.device_address
        attrs["manufacturer"] = self._platform.manufacturer
        attrs["model"] = self._platform.model

        if len(self._platform.option) > 0:
            attrs["option"] = self._platform.option

        if self._platform.has_parent:
            attrs["parent_device_id"] = self._platform.inverter_unit_id

        attrs["serial_number"] = self._platform.serial

        did = getattr(self.block, "model_id", None)
        if did is not None:
            if did in SUNSPEC_DID:
                attrs["sunspec_device"] = SUNSPEC_DID[did]
            attrs["sunspec_did"] = did

        mppt = getattr(self._platform, "mppt", None)
        if mppt is not None:
            if mppt.model_id in SUNSPEC_DID:
                attrs["mmppt_device"] = SUNSPEC_DID[mppt.model_id]
            attrs["mmppt_did"] = mppt.model_id
            attrs["mmppt_units"] = mppt.n

        return attrs


class Version(SolarEdgeSensorBase):
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_version"

    @property
    def name(self) -> str:
        return "Version"

    @property
    def native_value(self):
        return self._platform.fw_version


class ACCurrentSensor(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.CURRENT
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator)

        self._phase = phase
        self._did = getattr(platform.model_block, "model_id", None)
        self._field = _CURRENT_PHASES[phase]

    @property
    def unique_id(self) -> str:
        if self._phase is None:
            return f"{self._platform.uid_base}_ac_current"
        else:
            return f"{self._platform.uid_base}_ac_current_{self._phase.lower()}"

    @property
    def entity_registry_enabled_default(self) -> bool:
        if self._phase is None:
            return True

        return self._did in [103, 203, 204] and self._phase in ["A", "B", "C"]

    @property
    def name(self) -> str:
        if self._phase is None:
            return "AC Current"
        else:
            return f"AC Current {self._phase.upper()}"

    @property
    def native_value(self):
        return getattr(self.block, self._field)

    @property
    def suggested_display_precision(self):
        return _precision(self.block.a_sf)


class VoltageSensor(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.VOLTAGE
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfElectricPotential.VOLT

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator)

        self._phase = phase
        self._did = getattr(platform.model_block, "model_id", None)
        fields = (
            _METER_VOLTAGE if self._did in [201, 202, 203, 204] else _INVERTER_VOLTAGE
        )
        self._field = fields[phase]

    @property
    def unique_id(self) -> str:
        if self._phase is None:
            return f"{self._platform.uid_base}_ac_voltage"
        else:
            return f"{self._platform.uid_base}_ac_voltage_{self._phase.lower()}"

    @property
    def entity_registry_enabled_default(self) -> bool:
        if self._phase is None:
            raise NotImplementedError

        elif self._phase in ["LN", "LL", "AB"]:
            return True

        return self._did in [103, 203, 204] and self._phase in [
            "BC",
            "CA",
            "AN",
            "BN",
            "CN",
        ]

    @property
    def name(self) -> str:
        if self._phase is None:
            return "AC Voltage"
        else:
            return f"AC Voltage {self._phase.upper()}"

    @property
    def native_value(self):
        return getattr(self.block, self._field)

    @property
    def suggested_display_precision(self):
        return _precision(self.block.v_sf)


class ACPower(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.POWER
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfPower.WATT
    icon = "mdi:solar-power"

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator)

        self._phase = phase
        self._did = getattr(platform.model_block, "model_id", None)
        self._field = _POWER_PHASES[phase]

    @property
    def unique_id(self) -> str:
        if self._phase is None:
            return f"{self._platform.uid_base}_ac_power"
        else:
            return f"{self._platform.uid_base}_ac_power_{self._phase.lower()}"

    @property
    def entity_registry_enabled_default(self) -> bool:
        if self._phase is None:
            return True

        return self._did in [203, 204] and self._phase in ["A", "B", "C"]

    @property
    def name(self) -> str:
        if self._phase is None:
            return "AC Power"
        else:
            return f"AC Power {self._phase.upper()}"

    @property
    def native_value(self):
        return getattr(self.block, self._field)

    @property
    def suggested_display_precision(self):
        return _precision(self.block.w_sf)


class ACPowerInverted(ACPower):
    """Inverted AC power sensor for Home Assistant energy dashboard compatibility.

    This class exists solely due to a design decision by the Home Assistant team
    for their energy dashboard, which requires power to be represented opposite
    to how a grid-tie inverter normally reports it. The native_value is negated
    to meet this requirement.

    This does not represent how the inverter or SolarEdge dashboard will represent
    the same sensor. You should normally refer to the non-inverted version.
    """

    def __init__(self, platform, config_entry, coordinator):
        super().__init__(platform, config_entry, coordinator)

    @property
    def unique_id(self) -> str:
        return f"{super().unique_id}_inverted"

    @property
    def name(self) -> str:
        return f"{super().name} Inverted"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return AwesomeVersion(HA_VERSION) < AwesomeVersion(INVERTED_POWER_VERSION)

    @property
    def native_value(self):
        value = super().native_value
        if value is None:
            return None
        return -value


class ACFrequency(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.FREQUENCY
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfFrequency.HERTZ

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_ac_frequency"

    @property
    def name(self) -> str:
        return "AC Frequency"

    @property
    def native_value(self):
        return self.block.hz

    @property
    def suggested_display_precision(self):
        return _precision(self.block.hz_sf)


class ACVoltAmp(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.APPARENT_POWER
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfApparentPower.VOLT_AMPERE

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator)

        self._phase = phase
        self._field = _VA_PHASES[phase]

    @property
    def unique_id(self) -> str:
        if self._phase is None:
            return f"{self._platform.uid_base}_ac_va"
        else:
            return f"{self._platform.uid_base}_ac_va_{self._phase.lower()}"

    @property
    def name(self) -> str:
        if self._phase is None:
            return "AC Apparent Power"
        else:
            return f"AC Apparent Power {self._phase.upper()}"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def native_value(self):
        return getattr(self.block, self._field)

    @property
    def suggested_display_precision(self):
        return _precision(self.block.va_sf)


class ACVoltAmpReactive(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.REACTIVE_POWER
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfReactivePower.VOLT_AMPERE_REACTIVE

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator)

        self._phase = phase
        self._field = _VAR_PHASES[phase]

    @property
    def unique_id(self) -> str:
        if self._phase is None:
            return f"{self._platform.uid_base}_ac_var"
        else:
            return f"{self._platform.uid_base}_ac_var_{self._phase.lower()}"

    @property
    def name(self) -> str:
        if self._phase is None:
            return "AC Reactive Power"
        else:
            return f"AC Reactive Power {self._phase.upper()}"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def native_value(self):
        return getattr(self.block, self._field)

    @property
    def suggested_display_precision(self):
        return _precision(self.block.var_sf)


class ACPowerFactor(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.POWER_FACTOR
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = PERCENTAGE

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator)

        self._phase = phase
        self._field = _PF_PHASES[phase]

    @property
    def unique_id(self) -> str:
        if self._phase is None:
            return f"{self._platform.uid_base}_ac_pf"
        else:
            return f"{self._platform.uid_base}_ac_pf_{self._phase.lower()}"

    @property
    def name(self) -> str:
        if self._phase is None:
            return "AC Power Factor"
        else:
            return f"AC Power Factor {self._phase.upper()}"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def native_value(self):
        return getattr(self.block, self._field)

    @property
    def suggested_display_precision(self):
        return _precision(self.block.pf_sf)


class SolarEdgeAccumulatorBase(SolarEdgeTotalBase):
    """A TOTAL_INCREASING sensor over a SunSpec accumulator.

    SolarEdge accumulators have been seen going backwards, which Home Assistant
    reads as a meter reset and turns into a spike. The last good total is
    published instead until the counter recovers, which it usually does on the
    next poll.
    """

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator)

        self._phase = phase
        self._log_once = False

    @property
    def icon(self) -> str:
        if self._phase is None:
            return None

        elif re.match("import", self._phase.lower()):
            return "mdi:transmission-tower-export"

        elif re.match("export", self._phase.lower()):
            return "mdi:transmission-tower-import"

        else:
            return None

    @property
    def _current(self):
        """The counter as the device last reported it, or None if unimplemented."""
        return getattr(self.block, self._field)

    def _process_data(self) -> None:
        value = self._current
        last = self._attr_native_value

        if value is None:
            return

        if last is not None and value < last:
            if not self._log_once:
                _LOGGER.warning(
                    "Accumulator went backwards; this is a SolarEdge bug: "
                    f"{self._field} {value} < {last}"
                )
                self._log_once = True
            return

        self._log_once = False
        self._attr_native_value = value


class SolarEdgeACEnergy(SolarEdgeAccumulatorBase):
    """SolarEdge sensor for AC Energy watt-hour meters."""

    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL_INCREASING
    native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    suggested_display_precision = 3

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator, phase)

        self._did = getattr(platform.model_block, "model_id", None)
        if phase is None:
            self._field = "wh"  # the inverter's own lifetime energy
        else:
            self._field = _accumulator_field("tot_wh", phase)

    @property
    def unique_id(self) -> str:
        # older versions of the integration converted to kWh internally
        # before home assistant had UI configurable units and precision
        # changing the unique_id now would cause new entities to be created
        if self._phase is None:
            return f"{self._platform.uid_base}_ac_energy_kwh"
        else:
            return f"{self._platform.uid_base}_{self._phase.lower()}_kwh"

    @property
    def entity_registry_enabled_default(self) -> bool:
        if self._phase is None or self._phase in [
            "Exported",
            "Imported",
            "Exported_A",
            "Imported_A",
        ]:
            return True

        return self._did in [203, 204] and self._phase in [
            "Exported_B",
            "Exported_C",
            "Imported_B",
            "Imported_C",
        ]

    @property
    def name(self) -> str:
        if self._phase is None:
            return "AC Energy"
        else:
            return f"AC Energy {re.sub('_', ' ', self._phase)}"


class DCCurrent(SolarEdgeSensorBase):
    """DC Current for a SolarEdge inverter."""

    device_class = SensorDeviceClass.CURRENT
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    icon = "mdi:current-dc"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_dc_current"

    @property
    def name(self) -> str:
        return "DC Current"

    @property
    def available(self) -> bool:
        return super().available and self.block.dca is not None

    @property
    def native_value(self):
        return self.block.dca

    @property
    def suggested_display_precision(self) -> int:
        return _precision(self.block.dca_sf)


class SolarEdgeMMPPTSensorBase(SolarEdgeSensorBase):
    """A sensor over one module of the multiple MPPT model.

    The module list is rebuilt from the count the inverter reports each poll,
    so a module can go away; ``module`` is then ``None`` and the entity reads
    as unavailable rather than raising.
    """

    @property
    def block(self):
        return self._platform.module

    @property
    def mppt(self):
        """The model 160 block, which holds the modules' shared scale factors."""
        return self._platform.inverter.mppt

    @property
    def available(self) -> bool:
        return super().available and self.block is not None


class SolarEdgeDCCurrentMMPPT(SolarEdgeMMPPTSensorBase):
    """DC Current for Synergy MMPPT units."""

    device_class = SensorDeviceClass.CURRENT
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    icon = "mdi:current-dc"

    @property
    def unique_id(self) -> str:
        return (
            f"{self._platform.inverter.uid_base}_dc_current_mmppt{self._platform.unit}"
        )

    @property
    def name(self) -> str:
        return "DC Current"

    @property
    def available(self) -> bool:
        return super().available and self.block.dca is not None

    @property
    def native_value(self):
        return self.block.dca

    @property
    def suggested_display_precision(self) -> int:
        return _precision(self.mppt.dca_sf)


class DCVoltage(SolarEdgeSensorBase):
    """DC Voltage for a SolarEdge inverter."""

    device_class = SensorDeviceClass.VOLTAGE
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfElectricPotential.VOLT

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_dc_voltage"

    @property
    def name(self) -> str:
        return "DC Voltage"

    @property
    def native_value(self):
        return self.block.dcv

    @property
    def suggested_display_precision(self):
        return _precision(self.block.dcv_sf)


class SolarEdgeDCVoltageMMPPT(SolarEdgeMMPPTSensorBase):
    """DC Voltage for Synergy MMPPT units."""

    device_class = SensorDeviceClass.VOLTAGE
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfElectricPotential.VOLT

    @property
    def unique_id(self) -> str:
        return (
            f"{self._platform.inverter.uid_base}_dc_voltage_mmppt{self._platform.unit}"
        )

    @property
    def name(self) -> str:
        return "DC Voltage"

    @property
    def available(self) -> bool:
        return super().available and self.block.dcv is not None

    @property
    def native_value(self):
        return self.block.dcv

    @property
    def suggested_display_precision(self) -> int:
        return _precision(self.mppt.dcv_sf)


class DCPower(SolarEdgeSensorBase):
    """DC Power for a SolarEdge inverter."""

    device_class = SensorDeviceClass.POWER
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfPower.WATT
    icon = "mdi:solar-power"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_dc_power"

    @property
    def name(self) -> str:
        return "DC Power"

    @property
    def native_value(self):
        return self.block.dcw

    @property
    def suggested_display_precision(self):
        return _precision(self.block.dcw_sf)


class SolarEdgeDCPowerMMPPT(SolarEdgeMMPPTSensorBase):
    """DC Power for Synergy MMPPT units."""

    device_class = SensorDeviceClass.POWER
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfPower.WATT
    icon = "mdi:solar-power"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.inverter.uid_base}_dc_power_mmppt{self._platform.unit}"

    @property
    def name(self) -> str:
        return "DC Power"

    @property
    def available(self) -> bool:
        return super().available and self.block.dcw is not None

    @property
    def native_value(self):
        return self.block.dcw

    @property
    def suggested_display_precision(self) -> int:
        return _precision(self.mppt.dcw_sf)


class HeatSinkTemperature(SolarEdgeSensorBase):
    """Heat sink temperature for a SolarEdge inverter."""

    device_class = SensorDeviceClass.TEMPERATURE
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfTemperature.CELSIUS
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_temp_sink"

    @property
    def name(self) -> str:
        return "Temperature"

    @property
    def native_value(self):
        value = self.block.tmp_snk
        # An inverter without a heat sink probe reports a hard zero rather than
        # the unimplemented sentinel, which would read as a real 0 °C.
        return None if value == 0 else value

    @property
    def suggested_display_precision(self):
        return _precision(self.block.tmp_sf)


class SolarEdgeTemperatureMMPPT(SolarEdgeMMPPTSensorBase):
    """Temperature for Synergy MMPPT units."""

    device_class = SensorDeviceClass.TEMPERATURE
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfTemperature.CELSIUS
    entity_category = EntityCategory.DIAGNOSTIC
    suggested_display_precision = 0

    @property
    def unique_id(self) -> str:
        return f"{self._platform.inverter.uid_base}_tmp_mmppt{self._platform.unit}"

    @property
    def name(self) -> str:
        return "Temperature"

    @property
    def available(self) -> bool:
        return super().available and self.block.tmp is not None

    @property
    def native_value(self):
        return self.block.tmp


class SolarEdgeStatusSensor(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.ENUM
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_status"

    @property
    def name(self) -> str:
        return "Status"


class SolarEdgeInverterStatus(SolarEdgeStatusSensor):
    options = list(DEVICE_STATUS.values())

    @property
    def native_value(self):
        status = self.block.st
        if status is None:
            return None
        return str(DEVICE_STATUS.get(int(status)))

    @property
    def extra_state_attributes(self):
        status = self.block.st
        if status is None or int(status) not in DEVICE_STATUS_TEXT:
            return {}

        return {
            "status_text": DEVICE_STATUS_TEXT[int(status)],
            "status_value": int(status),
        }


class SolarEdgeBatteryStatus(SolarEdgeStatusSensor):
    options = list(BATTERY_STATUS.values())

    @property
    def native_value(self):
        status = self.block.status
        if status is None or status not in BATTERY_STATUS:
            return None
        return str(BATTERY_STATUS[status])

    @property
    def extra_state_attributes(self):
        status = self.block.status
        if status is None:
            return {}

        attrs = {"status_value": status}
        if status in BATTERY_STATUS_TEXT:
            attrs["status_text"] = BATTERY_STATUS_TEXT[status]
        return attrs


class StatusVendor(SolarEdgeSensorBase):
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_status_vendor"

    @property
    def name(self) -> str:
        return "Status Vendor"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return not self._platform.use_status_vendor4

    @property
    def native_value(self):
        status = self.block.st_vnd
        return None if status is None else str(int(status))

    @property
    def extra_state_attributes(self):
        status = self.block.st_vnd
        if status is None or int(status) not in VENDOR_STATUS:
            return None
        return {"description": VENDOR_STATUS[int(status)]}


class StatusVendor4(SolarEdgeSensorBase):
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_status_vendor4"

    @property
    def name(self) -> str:
        return "Status Vendor 4"

    @property
    def _code(self) -> int | None:
        block = self._platform.status_vendor4
        if block is None:
            return None
        value = block.status_vendor4
        return None if value in (None, 0xFFFFFFFF) else value

    @property
    def available(self) -> bool:
        return super().available and self._code is not None

    @property
    def native_value(self):
        value = self._code
        if value is None:
            return None
        return f"{(value >> 24) & 0xFF:X}x{value & 0xFFFF:X}"

    @property
    def extra_state_attributes(self):
        value = self._code
        if value is None:
            return None

        controller = (value >> 24) & 0xFF
        error = value & 0xFFFF
        attrs = {"controller": hex(controller), "error_code": hex(error)}

        if controller in VENDOR4_STATUS and error in VENDOR4_STATUS[controller]:
            attrs["description"] = VENDOR4_STATUS[controller][error]

        return attrs


class SolarEdgeGlobalPowerControlBlock(SolarEdgeSensorBase):
    @property
    def block(self):
        return self._platform.global_power_control

    @property
    def available(self) -> bool:
        return super().available and self.block is not None

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self._platform.device.has_block("global_power_control") is not False


class SolarEdgeRRCR(SolarEdgeGlobalPowerControlBlock):
    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_rrcr"

    @property
    def name(self) -> str:
        return "RRCR Status"

    @property
    def native_value(self):
        value = self.block.rrcr if self.block is not None else None
        # Only the low four bits are inputs; anything else is not an RRCR word.
        return None if value is None or value > 0xF else value

    @property
    def extra_state_attributes(self):
        value = self.native_value
        if value is None:
            return None

        inputs = [RRCR_STATUS[i] for i in range(4) if value & (1 << i)]
        return {"inputs": str(inputs)}


class SolarEdgeActivePowerLimit(SolarEdgeGlobalPowerControlBlock):
    """Global Dynamic Power Control: Inverter Active Power Limit"""

    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = PERCENTAGE
    suggested_display_precision = 0
    icon = "mdi:percent"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_active_power_limit"

    @property
    def name(self) -> str:
        return "Active Power Limit"

    @property
    def native_value(self) -> int:
        value = self.block.power_limit if self.block is not None else None
        return None if value is None or not 0 <= value <= 100 else value


class SolarEdgeCosPhi(SolarEdgeGlobalPowerControlBlock):
    """Global Dynamic Power Control: Inverter CosPhi"""

    state_class = SensorStateClass.MEASUREMENT
    suggested_display_precision = 1
    icon = "mdi:angle-acute"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_cosphi"

    @property
    def name(self) -> str:
        return "CosPhi"

    @property
    def native_value(self) -> float:
        value = self.block.cos_phi if self.block is not None else None
        return None if value is None or not -1.0 <= value <= 1.0 else round(value, 1)


class MeterEvents(SolarEdgeSensorBase):
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_meter_events"

    @property
    def name(self) -> str:
        return "Meter Events"

    @property
    def native_value(self):
        events = self.block.evt
        return None if events is None else int(events)

    @property
    def extra_state_attributes(self):
        events = self.block.evt
        if events is None:
            return {}

        value = int(events)
        active = [
            METER_EVENTS[i]
            for i in range(2, 31)
            if value & (1 << i) and i in METER_EVENTS
        ]
        return {"bits": f"{value:032b}", "events": str(active)}


class SolarEdgeMMPPTEvents(SolarEdgeSensorBase):
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def block(self):
        return self._platform.mppt

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_mmppt_events"

    @property
    def name(self) -> str:
        return "MMPPT Events"

    @property
    def available(self) -> bool:
        return (
            super().available and self.block is not None and self.block.evt is not None
        )

    @property
    def native_value(self) -> int:
        events = self.block.evt if self.block is not None else None
        return None if events is None else int(events)

    @property
    def extra_state_attributes(self) -> dict:
        value = self.native_value
        if value is None:
            return {}

        active = [
            MMPPT_EVENTS[i] for i in range(31) if value & (1 << i) and i in MMPPT_EVENTS
        ]
        return {"events": str(active), "bits": f"{value:032b}"}


class MeterVAhIE(SolarEdgeAccumulatorBase):
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL_INCREASING
    native_unit_of_measurement = ENERGY_VOLT_AMPERE_HOUR

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator, phase)

        if phase is None:
            raise NotImplementedError
        self._field = _accumulator_field("tot_v_ah", phase)

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_{self._phase.lower()}_vah"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return f"Apparent Energy {re.sub('_', ' ', self._phase)}"

    @property
    def suggested_display_precision(self):
        return _precision(self.block.tot_v_ah_sf)


class MetervarhIE(SolarEdgeAccumulatorBase):
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL_INCREASING
    native_unit_of_measurement = ENERGY_VOLT_AMPERE_REACTIVE_HOUR

    def __init__(self, platform, config_entry, coordinator, phase: str = None):
        super().__init__(platform, config_entry, coordinator, phase)

        if phase is None:
            raise NotImplementedError
        self._field = _quadrant_field(phase)

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_{self._phase.lower()}_varh"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return f"Reactive Energy {re.sub('_', ' ', self._phase)}"

    @property
    def suggested_display_precision(self):
        return _precision(self.block.tot_v_arh_sf)


class SolarEdgeBatteryAvgTemp(HeatSinkTemperature):
    suggested_display_precision = 1

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_avg_temp"

    @property
    def name(self) -> str:
        return "Average Temperature"

    @property
    def native_value(self):
        return _within(self.block.temp_average, BatteryLimit.Tmin, BatteryLimit.Tmax)


class SolarEdgeBatteryMaxTemp(HeatSinkTemperature):
    suggested_display_precision = 1

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_max_temp"

    @property
    def name(self) -> str:
        return "Max Temperature"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def native_value(self):
        return _within(self.block.temp_max, BatteryLimit.Tmin, BatteryLimit.Tmax)


class SolarEdgeBatteryVoltage(DCVoltage):
    suggested_display_precision = 2

    @property
    def native_value(self):
        if self.block.status == 0:
            return None
        return _within(self.block.dc_voltage, BatteryLimit.Vmin, BatteryLimit.Vmax)


class SolarEdgeBatteryCurrent(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.CURRENT
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    suggested_display_precision = 2
    icon = "mdi:current-dc"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_dc_current"

    @property
    def name(self) -> str:
        return "DC Current"

    @property
    def available(self) -> bool:
        if self.block.status == 0:
            return False
        current = _within(self.block.dc_current, BatteryLimit.Amin, BatteryLimit.Amax)
        return super().available and current is not None

    @property
    def native_value(self):
        return self.block.dc_current


class SolarEdgeBatteryPower(DCPower):
    suggested_display_precision = 2
    icon = "mdi:lightning-bolt"

    @property
    def native_value(self):
        if self.block.status == 0:
            return None
        power = self.block.dc_power
        # ±FLT_MAX is how a battery says "no reading" here, alongside the NaN
        # the field already decodes to None.
        if power is None or abs(power) == _FLOAT32_MAX:
            return None
        return power


class SolarEdgeBatteryPowerInverted(SolarEdgeBatteryPower):
    """Inverted battery power sensor for Home Assistant energy dashboard compatibility.

    This class exists solely due to a design decision by the Home Assistant team
    for their energy dashboard, which requires power to be represented opposite
    to how a grid-tie inverter normally reports it. The native_value is negated
    to meet this requirement.

    This does not represent how the inverter or SolarEdge dashboard will represent
    the same sensor. You should normally refer to the non-inverted version.
    """

    def __init__(self, platform, config_entry, coordinator):
        super().__init__(platform, config_entry, coordinator)

    @property
    def unique_id(self) -> str:
        return f"{super().unique_id}_inverted"

    @property
    def name(self) -> str:
        return f"{super().name} Inverted"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return AwesomeVersion(HA_VERSION) < AwesomeVersion(INVERTED_POWER_VERSION)

    @property
    def native_value(self):
        value = super().native_value
        if value is None:
            return None
        return -value


class SolarEdgeBatteryEnergyBase(SolarEdgeTotalBase):
    """A battery lifetime energy counter.

    SolarEdge resets these to zero when a battery is replaced or its firmware
    is updated, which Home Assistant would otherwise take as a real drop to
    zero. The reset is only accepted after the user has opted in and the
    counter has stayed low for the configured number of polls.
    """

    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL_INCREASING
    native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    suggested_display_precision = 3

    def __init__(self, platform, config_entry, coordinator):
        super().__init__(platform, config_entry, coordinator)

        self._count = 0
        self._log_once = None

    def _process_data(self) -> None:
        value = getattr(self.block, self._field)
        last = self._attr_native_value

        # Every gate below keeps the last total. The gates themselves stay — a
        # battery reporting 0 from standby would read as a counter reset, which
        # is worse than a stale total.
        if value is None or value == 0xFFFFFFFFFFFFFFFF:
            return

        if value == 0 and not self._platform.allow_battery_energy_reset:
            return

        if last is None or value >= last:
            self._attr_native_value = value
            self._log_once = False
            self._count = 0
            return

        if not self._platform.allow_battery_energy_reset:
            if not self._log_once:
                _LOGGER.warning(
                    f"Battery {self._label} went backwards: Current value "
                    f"{value} is less than last value of {last}"
                )
                self._log_once = True
            return

        self._count += 1
        _LOGGER.debug(
            f"{self._field} went backwards: {value} < {last} "
            f"cycle {self._count} of "
            f"{self._platform.battery_energy_reset_cycles}"
        )

        # A counter that has stayed low for long enough is a real reset, so
        # the new total is published rather than held.
        if self._count > self._platform.battery_energy_reset_cycles:
            _LOGGER.debug(f"{self._field} reset at cycle {self._count}")
            self._attr_native_value = value
            self._count = 0


class SolarEdgeBatteryEnergyExport(SolarEdgeBatteryEnergyBase):
    icon = "mdi:battery-charging-20"

    _field = "export_energy_wh"
    _label = "Export Energy"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_energy_export"

    @property
    def name(self) -> str:
        return "Energy Export"


class SolarEdgeBatteryEnergyImport(SolarEdgeBatteryEnergyBase):
    icon = "mdi:battery-charging-100"

    _field = "import_energy_wh"
    _label = "Import Energy"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_energy_import"

    @property
    def name(self) -> str:
        return "Energy Import"


class SolarEdgeBatteryMaxEnergy(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.ENERGY_STORAGE
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    suggested_display_precision = 3

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_max_energy"

    @property
    def name(self) -> str:
        return "Maximum Energy"

    @property
    def native_value(self):
        rated = self.block.rated_energy
        return _within(self.block.energy_max, 0, rated)


class SolarEdgeBatteryPowerBase(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.POWER
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfPower.WATT
    entity_category = EntityCategory.DIAGNOSTIC
    suggested_display_precision = 0

    @property
    def available(self):
        value = getattr(self.block, self._field)
        return super().available and value is not None and value >= 0

    @property
    def native_value(self):
        return getattr(self.block, self._field)


class SolarEdgeBatteryMaxChargePower(SolarEdgeBatteryPowerBase):
    _field = "max_charge_power"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_max_charge_power"

    @property
    def name(self) -> str:
        return "Max Charge Power"


class SolarEdgeBatteryMaxChargePeakPower(SolarEdgeBatteryPowerBase):
    _field = "max_charge_peak_power"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_max_charge_peak_power"

    @property
    def name(self) -> str:
        return "Peak Charge Power"


class SolarEdgeBatteryMaxDischargePower(SolarEdgeBatteryPowerBase):
    _field = "max_discharge_power"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_max_discharge_power"

    @property
    def name(self) -> str:
        return "Max Discharge Power"


class SolarEdgeBatteryMaxDischargePeakPower(SolarEdgeBatteryPowerBase):
    _field = "max_discharge_peak_power"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_max_discharge_peak_power"

    @property
    def name(self) -> str:
        return "Peak Discharge Power"


class SolarEdgeBatteryAvailableEnergy(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.ENERGY_STORAGE
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    suggested_display_precision = 3

    def __init__(self, platform, config_entry, coordinator):
        super().__init__(platform, config_entry, coordinator)
        self._log_warning = True

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_avail_energy"

    @property
    def name(self) -> str:
        return "Available Energy"

    @property
    def native_value(self):
        available = self.block.energy_available
        rated = self.block.rated_energy

        if available is None or available < 0 or rated is None:
            return None

        if available > rated * self._platform.battery_rating_adjust:
            if self._log_warning:
                _LOGGER.warning(
                    f"{self._platform.tag}: "
                    "Battery available energy exceeds rated energy. "
                    "Set configuration for Battery Rating Adjustment when necessary."
                )
                self._log_warning = False

            return None

        return available


class SolarEdgeBatterySOH(SolarEdgeSensorBase):
    state_class = SensorStateClass.MEASUREMENT
    entity_category = EntityCategory.DIAGNOSTIC
    native_unit_of_measurement = PERCENTAGE
    suggested_display_precision = 0
    icon = "mdi:battery-heart-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_battery_soh"

    @property
    def name(self) -> str:
        return "State of Health"

    @property
    def native_value(self):
        return _within(self.block.soh, 0, 100)


class SolarEdgeBatterySOE(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.BATTERY
    state_class = SensorStateClass.MEASUREMENT
    native_unit_of_measurement = PERCENTAGE
    suggested_display_precision = 0

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_battery_soe"

    @property
    def name(self) -> str:
        return "State of Energy"

    @property
    def native_value(self):
        return _within(self.block.soe, 0, 100)


class SolarEdgeAdvancedPowerControlBlock(SolarEdgeSensorBase):
    @property
    def block(self):
        return self._platform.advanced_power_control

    @property
    def available(self) -> bool:
        return super().available and self.block is not None


class SolarEdgeCommitControlSettings(SolarEdgeAdvancedPowerControlBlock):
    """Entity to show the results of Commit Power Control Settings button."""

    entity_category = EntityCategory.DIAGNOSTIC
    icon = "mdi:content-save-cog-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_commit_pwr_settings"

    @property
    def name(self) -> str:
        return "Commit Power Settings"

    @property
    def native_value(self):
        return self.block.commit_pwr_ctl_settings if self.block is not None else None

    @property
    def extra_state_attributes(self):
        value = self.native_value
        if value is None:
            return {}

        attrs = {"hex_value": hex(value)}

        if value == 0x0:
            attrs["status"] = "SUCCESS"
        elif value in [0x1, 0x2, 0x3, 0x4]:
            attrs["status"] = "INTERNAL_ERROR"
        elif value == 0xFFFF:
            attrs["status"] = "UNKNOWN_ERROR"
        elif 0xF102 <= value < 0xFFFF:
            attrs["status"] = "VALUE_ERROR"

        return attrs


class SolarEdgeDefaultControlSettings(SolarEdgeAdvancedPowerControlBlock):
    """Entity to show the results of Restore Power Control Default Settings button."""

    entity_category = EntityCategory.DIAGNOSTIC
    icon = "mdi:restore-alert"

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_default_pwr_settings"

    @property
    def name(self) -> str:
        return "Default Power Settings"

    @property
    def native_value(self):
        return self.block.restore_pwr_ctl_defaults if self.block is not None else None

    @property
    def extra_state_attributes(self):
        value = self.native_value
        if value is None:
            return {}

        attrs = {"hex_value": hex(value)}

        if value == 0x0:
            attrs["status"] = "SUCCESS"
        elif value == 0xFFFF:
            attrs["status"] = "ERROR"

        return attrs


class SolarEdgeLastUpdate(SolarEdgeSensorBase):
    device_class = SensorDeviceClass.TIMESTAMP
    entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._platform.uid_base}_last_update_timestamp"

    @property
    def name(self) -> str:
        return "Last Update"

    @property
    def available(self) -> bool:
        return True

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def native_value(self) -> datetime.datetime | None:
        return self.coordinator.last_update_success_time


# The largest finite 32-bit float; SolarEdge batteries send ±this to mean
# "no reading", separately from the NaN the field decodes to None.
_FLOAT32_MAX = 3.4028234663852886e38


def _within(value, low, high):
    """Return ``value`` when it is inside the plausible range, else ``None``.

    Batteries report values well outside physical possibility while they are
    waking up, and a temperature of -3000 °C on a graph is worse than a gap.
    """
    if value is None or low is None or high is None:
        return None
    return None if value < low or value > high else value
