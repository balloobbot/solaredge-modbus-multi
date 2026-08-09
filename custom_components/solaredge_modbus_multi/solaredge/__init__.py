"""A SolarEdge device library built on modbus-connection.

Nothing here imports Home Assistant. It takes a ``ModbusUnit``, models what a
SolarEdge inverter serves on it, and exposes the values as typed attributes —
so the register map can be tested against the mock backend with no hardware and
no Home Assistant in the loop.
"""

from __future__ import annotations

from .const import BATTERY_REG_BASE, METER_REG_BASE, SUNSPEC_BASE_ADDRESS
from .device import BatteryDevice, MeterDevice, SolarEdgeDevice, SolarEdgeOptions
from .errors import DeviceInvalid, DeviceNotSetUp, SolarEdgeError
from .models import (
    Common,
    GridStatus,
    Inverter,
    Meter,
    MeterEvents,
    Mppt,
    MpptModule,
    MpptModuleOperatingState,
    OperatingState,
    StatusVendor4,
)
from .proprietary import (
    AdvancedPowerControl,
    AdvancedPowerControl2,
    Battery,
    ExternalProductionMax,
    GlobalPowerControl,
    SiteLimit,
    StorageControl,
)

__all__ = [
    "AdvancedPowerControl",
    "AdvancedPowerControl2",
    "BATTERY_REG_BASE",
    "Battery",
    "BatteryDevice",
    "Common",
    "DeviceInvalid",
    "DeviceNotSetUp",
    "ExternalProductionMax",
    "GlobalPowerControl",
    "GridStatus",
    "Inverter",
    "METER_REG_BASE",
    "Meter",
    "MeterDevice",
    "MeterEvents",
    "Mppt",
    "MpptModule",
    "MpptModuleOperatingState",
    "OperatingState",
    "SUNSPEC_BASE_ADDRESS",
    "SiteLimit",
    "SolarEdgeDevice",
    "SolarEdgeError",
    "SolarEdgeOptions",
    "StatusVendor4",
    "StorageControl",
]
