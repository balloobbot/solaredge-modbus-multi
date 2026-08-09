"""Fixtures that build a fake SolarEdge inverter in the mock backend.

``mock_modbus_unit`` comes from modbus-connection's pytest plugin, which is
registered through an entry point — there is no wiring to do beyond seeding the
registers.

The device library is imported as the top-level package ``solaredge`` rather
than as ``solaredge_modbus_multi.solaredge``: importing it through the
component package would execute the integration's ``__init__``, which pulls in
Home Assistant. Importing it standalone is also the check that keeps the
library free of Home Assistant imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components"
sys.path.insert(0, str(_COMPONENT / "solaredge_modbus_multi"))

from .fixtures import (  # noqa: E402
    seed_battery,
    seed_inverter,
    seed_meter,
    seed_mppt,
)

__all__ = ["seed_battery", "seed_inverter", "seed_meter", "seed_mppt"]


@pytest.fixture
def inverter_unit(mock_modbus_unit):
    """A mock unit seeded with a single-meter, no-battery three-phase inverter."""
    seed_inverter(mock_modbus_unit)
    seed_meter(mock_modbus_unit, meter_id=1)
    return mock_modbus_unit
