"""Register map constants for SolarEdge devices."""

from __future__ import annotations

from typing import Final

# The SunSpec "SunS" marker, and the start of the model chain behind it.
SUNSPEC_BASE_ADDRESS: Final = 40000

# SunSpec model IDs SolarEdge publishes, in the order we prefer them.
INVERTER_MODEL_IDS: Final = (103, 102, 101)
METER_MODEL_IDS: Final = (201, 202, 203, 204)
MMPPT_MODEL_ID: Final = 160
COMMON_MODEL_ID: Final = 1

# Meter ID and the address of its SunSpec common block on a non-MMPPT inverter.
# An inverter that publishes model 160 pushes every meter block along by the
# length of that model, which is read from the chain rather than assumed.
METER_REG_BASE: Final = {
    1: 40121,
    2: 40295,
    3: 40469,
}

# A meter's model block follows its common block, which is 65 words plus header.
METER_MODEL_OFFSET: Final = 67

# Battery ID and the base address of its (proprietary) block.
BATTERY_REG_BASE: Final = {
    1: 57600,
    2: 57856,
    3: 58368,
}

# The battery block's dynamic half, relative to the base address above.
BATTERY_DATA_OFFSET: Final = 68

# Proprietary SolarEdge blocks, all CDAB word order.
SITE_LIMIT_ADDRESS: Final = 57344  # 0xE000
EXT_PROD_MAX_ADDRESS: Final = 57362  # 0xE012
STORAGE_CONTROL_ADDRESS: Final = 57348  # 0xE004
GLOBAL_POWER_CONTROL_ADDRESS: Final = 61440  # 0xF000
ADVANCED_POWER_CONTROL_ADDRESS: Final = 61696  # 0xF100
ADVANCED_POWER_CONTROL_2_ADDRESS: Final = 61782  # 0xF156

# Offsets inside the SunSpec inverter model that SolarEdge repurposes for its
# own values. Both live in the vendor event fields of models 101/102/103.
GRID_STATUS_OFFSET: Final = 44  # Evt_Vnd1
STATUS_VENDOR4_OFFSET: Final = 50  # Evt_Vnd4
