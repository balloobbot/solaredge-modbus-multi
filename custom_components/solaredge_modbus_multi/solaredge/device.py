"""Device objects: what lives on one SolarEdge Modbus unit id, and how to poll it.

A SolarEdge installation puts several logical devices behind a single unit id.
The inverter answers the SunSpec map at 40000; up to three meters follow it in
the same model chain; up to three batteries sit in a proprietary range; and the
inverter's own control blocks are scattered across two more. Each of those is
polled on its own, so one that stops answering keeps its previous values while
the rest still refresh.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from modbus_connection import (
    IllegalDataAddressError,
    IllegalFunctionError,
    ModbusConnectionError,
    ModbusError,
    ModbusTimeoutError,
)
from modbus_connection.model import Component, ComponentGroup
from modbus_connection.model.sunspec import SunSpecError, SunSpecModel, scan

from .const import (
    ADVANCED_POWER_CONTROL_2_ADDRESS,
    ADVANCED_POWER_CONTROL_ADDRESS,
    BATTERY_REG_BASE,
    COMMON_MODEL_ID,
    EXT_PROD_MAX_ADDRESS,
    GLOBAL_POWER_CONTROL_ADDRESS,
    INVERTER_MODEL_IDS,
    METER_MODEL_IDS,
    METER_MODEL_OFFSET,
    METER_REG_BASE,
    MMPPT_MODEL_ID,
    SITE_LIMIT_ADDRESS,
    STORAGE_CONTROL_ADDRESS,
    SUNSPEC_BASE_ADDRESS,
)
from .errors import DeviceInvalid, DeviceNotSetUp
from .models import Common, GridStatus, Inverter, Meter, Mppt, StatusVendor4
from .proprietary import (
    AdvancedPowerControl,
    AdvancedPowerControl2,
    Battery,
    ExternalProductionMax,
    GlobalPowerControl,
    SiteLimit,
    StorageControl,
)

if TYPE_CHECKING:
    from modbus_connection import ModbusUnit
    from modbus_connection.model.sunspec import SunSpecModels

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SolarEdgeOptions:
    """What the user asked this integration to look for and control."""

    detect_meters: bool = True
    detect_batteries: bool = False
    detect_extras: bool = False
    storage_control: bool = False
    site_limit_control: bool = False

    slow_block_timeout: float | None = None
    """Seconds to allow the power control blocks, which answer slowly.

    ``modbus_connection`` takes one timeout for the whole connection, and it has
    to suit an ordinary poll. These two blocks are known to take several times
    as long on some inverters, so they get their own budget on top.
    """


@dataclass(frozen=True)
class UpdateReport:
    """What one poll refreshed, by the name each thing polled goes under.

    The names are ``inverter``, ``meter_1`` to ``meter_3``, ``battery_1`` to
    ``battery_3``, and each optional block's own key.

    A failed name kept its previous values and did not notify; the error that
    failed it rides along. An optional block the inverter refuses outright is
    absent rather than failed, and appears in neither set. A dead link is never
    in here — the update raises ``ModbusConnectionError`` instead of reporting
    partial silence.
    """

    updated: set[str]
    failed: dict[str, ModbusError]

    @property
    def complete(self) -> bool:
        """Whether everything polled refreshed."""
        return not self.failed


@dataclass
class OptionalBlock:
    """One block that only some firmware serves, and what we know about it.

    ``supported`` starts as ``None`` — untried. An address the inverter refuses
    settles it to ``False`` for good; a block that merely times out is left
    unsettled so a later poll can pick it up, which is how the integration has
    always treated the two power-control blocks.
    """

    component: Component
    supported: bool | None = None
    timed_out: bool = False
    timeout: float | None = None
    """Seconds this block is allowed, over the connection's own timeout."""


class MeterDevice:
    """A meter, as a SunSpec common block plus one of models 201-204."""

    def __init__(
        self,
        unit: ModbusUnit,
        meter_id: int,
        common_model: SunSpecModel,
        meter_model: SunSpecModel,
    ) -> None:
        self.meter_id = meter_id
        self.model_id = meter_model.model_id
        self.common = Common(unit, common_model)
        self.meter = Meter(unit, meter_model)

    @property
    def components(self) -> list[Component]:
        """The components this meter contributes to its inverter's poll."""
        return [self.common, self.meter]


class BatteryDevice:
    """A StorEdge battery at one of the three proprietary base addresses."""

    def __init__(self, unit: ModbusUnit, battery_id: int) -> None:
        self.battery_id = battery_id
        self.battery = Battery(unit, base_offset=BATTERY_REG_BASE[battery_id])

    @property
    def components(self) -> list[Component]:
        """The components this battery contributes to its inverter's poll."""
        return [self.battery]

    @property
    def usable(self) -> bool:
        """Whether a battery is actually installed in this slot.

        An empty slot answers with a nameplate energy of zero or the
        unimplemented float, which decodes to ``None``.
        """
        rated = self.battery.rated_energy
        return rated is not None and rated > 0


class SolarEdgeDevice:
    """Everything that answers on one SolarEdge Modbus unit id."""

    def __init__(
        self,
        unit: ModbusUnit,
        unit_id: int,
        options: SolarEdgeOptions | None = None,
    ) -> None:
        self._unit = unit
        self.unit_id = unit_id
        self.options = options or SolarEdgeOptions()

        self.common: Common | None = None
        self.inverter: Inverter | None = None
        self.mppt: Mppt | None = None
        self.meters: list[MeterDevice] = []
        self.batteries: list[BatteryDevice] = []

        # One read plan per logical device, keyed by the name it reports under.
        # ``None`` until setup has run, which is what marks the device unset up.
        self._polled: dict[str, ComponentGroup] | None = None
        self._optional: dict[str, OptionalBlock] = {}
        self._models: SunSpecModels | None = None

    # -- setup -----------------------------------------------------------------

    async def async_scan(self) -> SunSpecModels:
        """Walk the SunSpec model chain once, and remember what it found.

        Raises :class:`DeviceInvalid` if the unit is not a SunSpec device.
        """
        if self._models is not None:
            return self._models
        try:
            self._models = await scan(self._unit, SUNSPEC_BASE_ADDRESS)
        except SunSpecError as err:
            raise DeviceInvalid(f"ID {self.unit_id} is not a SunSpec device: {err}")
        return self._models

    async def async_setup(self) -> None:
        """Discover what this unit serves and build the components for it.

        Raises :class:`DeviceInvalid` if the unit is not a SunSpec device or has
        no inverter model, and the usual ``ModbusError`` subclasses if it cannot
        be reached at all.
        """
        models = await self.async_scan()
        if self.common is None:
            await self.async_read_identity()

        inverter_model = models.first(*INVERTER_MODEL_IDS)
        if inverter_model is None:
            raise DeviceInvalid(f"ID {self.unit_id} is not a SunSpec inverter")
        # ``Inverter`` deliberately stops at ``st_vnd``, short of the model's
        # end, and a component that declares no readable map stands for exactly
        # the addresses it reads — so no pooled read can reach the vendor event
        # registers behind it. Firmware that refuses those would otherwise fail
        # the whole poll instead of one optional block.
        self.inverter = Inverter(self._unit, inverter_model)

        self._setup_mppt(models)
        if self.options.detect_meters:
            self._setup_meters(models)
        self._setup_optional_blocks(inverter_model)
        self._build_polled()

    async def async_read_identity(self) -> Common:
        """Read the SunSpec identity block, and stop there.

        A caller that has to tell an inverter from an EVSE needs the model name
        before setup goes looking for inverter models, and an EVSE never gets
        any further than this: it publishes the identity block and nothing
        else. The scan and the block read are both cached, so a caller that
        goes on to :meth:`async_setup` does not pay for them twice.
        """
        models = await self.async_scan()
        common_model = models.first(COMMON_MODEL_ID)
        if common_model is None:
            raise DeviceInvalid(f"ID {self.unit_id} publishes no SunSpec common model")
        common = Common(self._unit, common_model)
        await common.async_update()
        # Settled only once the read landed: a block left half read would stop
        # the next attempt from trying again, and setup would build on it.
        self.common = common
        self._polled = {"inverter": ComponentGroup(self._unit, [common])}
        return self.common

    def _setup_mppt(self, models: SunSpecModels) -> None:
        """Attach the multiple-MPPT model, if the inverter publishes one."""
        mppt_model = models.first(MMPPT_MODEL_ID)
        if mppt_model is None:
            _LOGGER.debug("I%s is NOT Multiple MPPT", self.unit_id)
            return
        self.mppt = Mppt(self._unit, mppt_model)
        _LOGGER.debug("I%s is Multiple MPPT", self.unit_id)

    def _setup_meters(self, models: SunSpecModels) -> None:
        """Attach every meter the chain shows at a known meter slot.

        SolarEdge keeps meter *n* at a fixed address, shifted along by the whole
        multiple-MPPT model when the inverter publishes one. The shift used to
        be hardcoded as 50 or 70 registers; the model chain reports the model's
        real span instead, so the arithmetic comes from the device.
        """
        mppt_model = models.first(MMPPT_MODEL_ID)
        shift = 0 if mppt_model is None else mppt_model.span

        for meter_id, base in METER_REG_BASE.items():
            common_model = models.at(base + shift)
            if common_model is None or common_model.model_id != COMMON_MODEL_ID:
                _LOGGER.debug("I%sM%s: no common block", self.unit_id, meter_id)
                continue
            meter_model = models.at(base + shift + METER_MODEL_OFFSET)
            if meter_model is None or meter_model.model_id not in METER_MODEL_IDS:
                _LOGGER.debug("I%sM%s: no meter model", self.unit_id, meter_id)
                continue
            self.meters.append(
                MeterDevice(self._unit, meter_id, common_model, meter_model)
            )
            _LOGGER.debug(
                "Found I%sM%s (model %s)", self.unit_id, meter_id, meter_model.model_id
            )

    def _setup_optional_blocks(self, inverter_model: SunSpecModel) -> None:
        """Register the blocks that are read on their own, each with its own fate."""
        base = inverter_model.address
        self._optional = {
            "grid_status": OptionalBlock(GridStatus(self._unit, base_offset=base)),
            "status_vendor4": OptionalBlock(
                StatusVendor4(self._unit, base_offset=base)
            ),
        }
        if self.options.detect_extras:
            slow = self.options.slow_block_timeout
            self._optional["global_power_control"] = OptionalBlock(
                GlobalPowerControl(
                    self._unit, base_offset=GLOBAL_POWER_CONTROL_ADDRESS
                ),
                timeout=slow,
            )
            self._optional["advanced_power_control"] = OptionalBlock(
                AdvancedPowerControl(
                    self._unit, base_offset=ADVANCED_POWER_CONTROL_ADDRESS
                ),
                timeout=slow,
            )
            self._optional["advanced_power_control_2"] = OptionalBlock(
                AdvancedPowerControl2(
                    self._unit, base_offset=ADVANCED_POWER_CONTROL_2_ADDRESS
                ),
                timeout=slow,
            )
        if self.options.site_limit_control:
            self._optional["site_limit"] = OptionalBlock(
                SiteLimit(self._unit, base_offset=SITE_LIMIT_ADDRESS)
            )
            self._optional["ext_prod_max"] = OptionalBlock(
                ExternalProductionMax(self._unit, base_offset=EXT_PROD_MAX_ADDRESS)
            )
        if self.options.storage_control:
            self._optional["storage_control"] = OptionalBlock(
                StorageControl(self._unit, base_offset=STORAGE_CONTROL_ADDRESS)
            )

    async def async_add_batteries(self) -> None:
        """Probe the three battery slots and keep the ones that hold a battery.

        Batteries live outside the SunSpec chain, so unlike meters there is
        nothing to discover them with — each slot has to be read.
        """
        if not self.options.detect_batteries:
            return
        for battery_id in BATTERY_REG_BASE:
            battery = BatteryDevice(self._unit, battery_id)
            try:
                await battery.battery.async_update()
            except (IllegalDataAddressError, IllegalFunctionError):
                _LOGGER.debug("I%sB%s: address not served", self.unit_id, battery_id)
                continue
            if not battery.usable:
                _LOGGER.debug("I%sB%s: no battery installed", self.unit_id, battery_id)
                continue
            self.batteries.append(battery)
            _LOGGER.debug("Found I%sB%s", self.unit_id, battery_id)
        self._build_polled()

    def _build_polled(self) -> None:
        """Pool each logical device's components into its own read plan.

        Pooling stops at the device boundary rather than covering the unit:
        everything in one plan stands or falls together, and a meter that goes
        quiet must not take the inverter's values with it. Within a device the
        blocks sit back to back anyway, so the reads still merge.
        """
        assert self.common is not None
        inverter: list[Component] = [self.common]
        if self.inverter is not None:
            inverter.append(self.inverter)
        if self.mppt is not None:
            inverter.append(self.mppt)
        polled = {"inverter": ComponentGroup(self._unit, inverter)}
        for meter in self.meters:
            polled[f"meter_{meter.meter_id}"] = ComponentGroup(
                self._unit, meter.components
            )
        for battery in self.batteries:
            polled[f"battery_{battery.battery_id}"] = ComponentGroup(
                self._unit, battery.components
            )
        self._polled = polled

    # -- polling ---------------------------------------------------------------

    async def async_update(self) -> UpdateReport:
        """Refresh this unit, and report what came back.

        Each logical device goes out as its own pooled set of reads and each
        optional block follows on its own, so neither a device that stops
        answering nor a block the inverter does not serve can fail the rest —
        what failed keeps its previous values and is named in the report.
        Listeners fire once everything has been tried, and only for what
        refreshed. A failure of the link itself raises instead of reporting.
        """
        if self._polled is None:
            raise DeviceNotSetUp(f"ID {self.unit_id} was polled before setup")
        updated: set[str] = set()
        failed: dict[str, ModbusError] = {}
        fresh: list[ComponentGroup | Component] = []
        for name, group in self._polled.items():
            try:
                await group.async_update(notify=False)
            except ModbusConnectionError:
                raise
            except ModbusError as err:
                _LOGGER.debug("I%s: %s did not refresh: %s", self.unit_id, name, err)
                failed[name] = err
            else:
                updated.add(name)
                fresh.append(group)
        for name, block in self._optional.items():
            error = await self._poll_optional(name, block)
            if error is not None:
                failed[name] = error
            elif block.supported:
                updated.add(name)
                fresh.append(block.component)
        for component in fresh:
            component.notify()
        return UpdateReport(updated, failed)

    async def _poll_optional(
        self, name: str, block: OptionalBlock
    ) -> ModbusError | None:
        """Read one optional block, remembering what the inverter said about it.

        Returns the error that stopped it, or ``None``. A block the inverter
        refuses outright is absent rather than failed, so it reports neither.
        """
        if block.supported is False:
            return None
        try:
            if block.timeout is None:
                await block.component.async_update(notify=False)
            else:
                async with asyncio.timeout(block.timeout):
                    await block.component.async_update(notify=False)
        except ModbusConnectionError:
            raise
        except (IllegalDataAddressError, IllegalFunctionError):
            block.supported = False
            _LOGGER.debug("I%s: %s NOT available", self.unit_id, name)
            return None
        except TimeoutError as err:
            # Left unsettled on purpose: an inverter that is merely slow to
            # answer this block should get another chance next cycle. The
            # budget above is ours, so its plain TimeoutError becomes a
            # modbus one to report under.
            block.timed_out = True
            _LOGGER.debug("I%s: %s timed out", self.unit_id, name)
            if isinstance(err, ModbusTimeoutError):
                return err
            return ModbusTimeoutError(f"I{self.unit_id}: {name} timed out")
        except ModbusError as err:
            _LOGGER.debug("I%s: %s did not refresh: %s", self.unit_id, name, err)
            return err
        else:
            block.supported = True
            block.timed_out = False
            return None

    async def async_read_raw(self) -> dict[str, dict[int, int | bool]]:
        """Return every register this device reads, undecoded, for diagnostics.

        The fields refresh, but nothing notifies: a diagnostics download is not
        a poll, and firing the listeners would write a state for every entity
        off the update cycle.
        """
        if self._polled is None:
            raise DeviceNotSetUp(f"ID {self.unit_id} was read before setup")
        raw: dict[str, dict[int, int | bool]] = {}
        for group in self._polled.values():
            for space, values in (await group.async_read_raw(notify=False)).items():
                raw.setdefault(space, {}).update(values)
        for block in self._optional.values():
            if block.supported is False:
                continue
            try:
                for space, values in (
                    await block.component.async_read_raw(notify=False)
                ).items():
                    raw.setdefault(space, {}).update(values)
            except Exception as err:  # noqa: BLE001 - diagnostics must not fail
                _LOGGER.debug(
                    "I%s: raw read of an optional block failed: %s", self.unit_id, err
                )
        return {space: dict(sorted(values.items())) for space, values in raw.items()}

    # -- optional block access -------------------------------------------------

    def block(self, name: str) -> Component | None:
        """Return an optional block's component, or ``None`` if it is absent."""
        block = self._optional.get(name)
        if block is None or block.supported is False:
            return None
        return block.component

    def has_block(self, name: str) -> bool | None:
        """Whether an optional block is present: ``None`` while still untried."""
        block = self._optional.get(name)
        return None if block is None else block.supported

    def timed_out_blocks(self) -> list[str]:
        """Names of the optional blocks whose last read timed out."""
        return [name for name, block in self._optional.items() if block.timed_out]

    @property
    def storage_control(self) -> StorageControl | None:
        """The storage control block, if this inverter serves one."""
        return self.block("storage_control")  # type: ignore[return-value]

    @property
    def site_limit(self) -> SiteLimit | None:
        """The site limit block, if this inverter serves one."""
        return self.block("site_limit")  # type: ignore[return-value]

    @property
    def ext_prod_max(self) -> ExternalProductionMax | None:
        """The external production maximum block, if this inverter serves one."""
        return self.block("ext_prod_max")  # type: ignore[return-value]

    @property
    def global_power_control(self) -> GlobalPowerControl | None:
        """The global dynamic power control block, if this inverter serves one."""
        return self.block("global_power_control")  # type: ignore[return-value]

    @property
    def advanced_power_control(self) -> AdvancedPowerControl | None:
        """The first advanced power control block, if this inverter serves one."""
        return self.block("advanced_power_control")  # type: ignore[return-value]

    @property
    def advanced_power_control_2(self) -> AdvancedPowerControl2 | None:
        """The second advanced power control block, if this inverter serves one."""
        return self.block("advanced_power_control_2")  # type: ignore[return-value]

    @property
    def grid_status(self) -> GridStatus | None:
        """The grid on/off block, if this inverter serves one."""
        return self.block("grid_status")  # type: ignore[return-value]

    @property
    def status_vendor4(self) -> StatusVendor4 | None:
        """The extended vendor status block, if this firmware serves one."""
        return self.block("status_vendor4")  # type: ignore[return-value]

    @property
    def is_mmppt(self) -> bool:
        """Whether this inverter publishes the multiple MPPT model."""
        return self.mppt is not None
