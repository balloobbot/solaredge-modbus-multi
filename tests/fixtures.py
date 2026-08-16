"""Build a plausible SolarEdge register map in the mock backend.

The addresses here are the ones a real inverter answers on, so a test that
passes against these is testing the same arithmetic production does.
"""

from __future__ import annotations

from typing import Any

SUNSPEC_MARKER = [0x5375, 0x6E53]  # "SunS"
END_OF_CHAIN = 0xFFFF

COMMON_LENGTH = 65
INVERTER_LENGTH = 50
METER_LENGTH = 105
MPPT_LENGTH_2_UNITS = 48


def string_registers(text: str, length: int) -> list[int]:
    """Encode ``text`` as ``length`` null-padded big-endian registers."""
    raw = text.encode("ascii").ljust(length * 2, b"\x00")[: length * 2]
    return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)]


def u32(value: int, *, little: bool = False) -> list[int]:
    """Split an unsigned 32-bit value into two registers."""
    words = [(value >> 16) & 0xFFFF, value & 0xFFFF]
    return list(reversed(words)) if little else words


def u64(value: int, *, little: bool = False) -> list[int]:
    """Split an unsigned 64-bit value into four registers."""
    words = [(value >> shift) & 0xFFFF for shift in (48, 32, 16, 0)]
    return list(reversed(words)) if little else words


def f32(value: float, *, little: bool = True) -> list[int]:
    """Encode a 32-bit float, CDAB by default as SolarEdge sends it."""
    import struct

    raw = struct.unpack(">I", struct.pack(">f", value))[0]
    return u32(raw, little=little)


def i16(value: int) -> int:
    """Two's-complement a signed 16-bit value into a register word."""
    return value & 0xFFFF


def seed_common(
    holding: Any,
    address: int,
    *,
    manufacturer: str = "SolarEdge",
    model: str = "SE10000H-US000BNU4",
    option: str = "",
    version: str = "0004.0019.0033",
    serial: str = "7E1B4C29",
    device_address: int = 1,
) -> int:
    """Seed a SunSpec model 1 block; returns the address after it."""
    holding[address] = [1, COMMON_LENGTH]
    holding[address + 2] = string_registers(manufacturer, 16)
    holding[address + 18] = string_registers(model, 16)
    holding[address + 34] = string_registers(option, 8)
    holding[address + 42] = string_registers(version, 8)
    holding[address + 50] = string_registers(serial, 16)
    holding[address + 66] = device_address
    return address + 2 + COMMON_LENGTH


def seed_inverter_model(holding: Any, address: int, *, model_id: int = 103) -> int:
    """Seed a SunSpec model 101/102/103 block; returns the address after it."""
    holding[address] = [model_id, INVERTER_LENGTH]
    holding[address + 2] = [1234, 411, 412, 411]  # A, AphA, AphB, AphC
    holding[address + 6] = i16(-2)  # A_SF
    holding[address + 7] = [4001, 4002, 4003]  # PPVphAB, BC, CA
    holding[address + 10] = [2301, 2302, 2303]  # PhVphA, B, C
    holding[address + 13] = i16(-1)  # V_SF
    holding[address + 14] = i16(5432)  # W
    holding[address + 15] = 0  # W_SF
    holding[address + 16] = 4999  # Hz
    holding[address + 17] = i16(-2)  # Hz_SF
    holding[address + 18] = i16(5500)  # VA
    holding[address + 19] = 0  # VA_SF
    holding[address + 20] = i16(-120)  # VAr
    holding[address + 21] = 0  # VAr_SF
    holding[address + 22] = i16(9985)  # PF
    holding[address + 23] = i16(-2)  # PF_SF
    holding[address + 24] = u32(12345678)  # WH
    holding[address + 26] = 0  # WH_SF
    holding[address + 27] = 890  # DCA
    holding[address + 28] = i16(-2)  # DCA_SF
    holding[address + 29] = 3805  # DCV
    holding[address + 30] = i16(-1)  # DCV_SF
    holding[address + 31] = i16(5480)  # DCW
    holding[address + 32] = 0  # DCW_SF
    holding[address + 33] = 0x8000  # TmpCab: not implemented
    holding[address + 34] = i16(452)  # TmpSnk
    holding[address + 35] = 0x8000  # TmpTrns: not implemented
    holding[address + 36] = 0x8000  # TmpOt: not implemented
    holding[address + 37] = i16(-1)  # Tmp_SF
    holding[address + 38] = 4  # St: MPPT
    holding[address + 39] = 0  # StVnd: no error
    holding[address + 40] = [0, 0, 0, 0]  # Evt1, Evt2
    holding[address + 44] = u32(0, little=True)  # Evt_Vnd1: grid on, CDAB
    holding[address + 46] = [0, 0, 0, 0]  # Evt_Vnd2, Evt_Vnd3
    holding[address + 50] = u32(0)  # Evt_Vnd4
    return address + 2 + INVERTER_LENGTH


def seed_mppt_model(holding: Any, address: int, *, units: int = 2) -> int:
    """Seed a SunSpec model 160 block with ``units`` modules."""
    length = 8 + units * 20
    holding[address] = [160, length]
    holding[address + 2] = [i16(-2), i16(-1), 0, 0]  # DCA_SF, DCV_SF, DCW_SF, DCWH_SF
    holding[address + 6] = u32(0)  # Evt
    holding[address + 8] = [units, 0]  # N, TmsPer
    for module in range(units):
        base = address + 10 + module * 20
        holding[base] = module + 1  # ID
        holding[base + 1] = string_registers(f"String {module + 1}", 8)
        holding[base + 9] = 445 + module  # DCA
        holding[base + 10] = 3801 + module  # DCV
        holding[base + 11] = 2740 + module  # DCW
        holding[base + 12] = u32(6172839 + module)  # DCWH
        holding[base + 14] = u32(0)  # Tms
        holding[base + 16] = i16(45)  # Tmp
        holding[base + 17] = 4  # DCSt
        holding[base + 18] = u32(0)  # DCEvt
    return address + 2 + length


def seed_meter_model(holding: Any, address: int, *, model_id: int = 203) -> int:
    """Seed a SunSpec model 201-204 block; returns the address after it."""
    holding[address] = [model_id, METER_LENGTH]
    holding[address + 2] = [i16(300), i16(100), i16(100), i16(100)]  # A, AphA/B/C
    holding[address + 6] = i16(-2)  # A_SF
    holding[address + 7] = [i16(2301), i16(2302), i16(2303), i16(2304)]  # PhV, A, B, C
    holding[address + 11] = [
        i16(4001),
        i16(4002),
        i16(4003),
        i16(4004),
    ]  # PPV, AB, BC, CA
    holding[address + 15] = i16(-1)  # V_SF
    holding[address + 16] = i16(4999)  # Hz
    holding[address + 17] = i16(-2)  # Hz_SF
    holding[address + 18] = [i16(-1500), i16(-500), i16(-500), i16(-500)]  # W, phases
    holding[address + 22] = 0  # W_SF
    holding[address + 23] = [i16(1600), i16(540), i16(530), i16(530)]  # VA, phases
    holding[address + 27] = 0  # VA_SF
    holding[address + 28] = [i16(-200), i16(-70), i16(-65), i16(-65)]  # VAr, phases
    holding[address + 32] = 0  # VAr_SF
    holding[address + 33] = [i16(-9800), i16(-9800), i16(-9800), i16(-9800)]  # PF
    holding[address + 37] = i16(-2)  # PF_SF
    for index in range(8):  # TotWhExp/Imp and their phases
        holding[address + 38 + index * 2] = u32(1_000_000 + index * 1000)
    holding[address + 54] = 0  # TotWh_SF
    for index in range(8):  # TotVAhExp/Imp and their phases
        holding[address + 55 + index * 2] = u32(2_000_000 + index * 1000)
    holding[address + 71] = 0  # TotVAh_SF
    for index in range(16):  # the four VArh quadrants and their phases
        holding[address + 72 + index * 2] = u32(3_000_000 + index * 1000)
    holding[address + 104] = 0  # TotVArh_SF
    holding[address + 105] = u32(0)  # Evt
    return address + 2 + METER_LENGTH


def seed_inverter(
    unit: Any,
    *,
    model_id: int = 103,
    mppt_units: int | None = None,
    serial: str = "7E1B4C29",
    model: str = "SE10000H-US000BNU4",
) -> dict[str, int]:
    """Seed the SunSpec chain: marker, common block, inverter model, MPPT.

    Returns the addresses the chain ended up placing things at, so a test can
    assert on the layout rather than restate it.
    """
    holding = unit.holding
    holding[40000] = SUNSPEC_MARKER
    layout = {"common": 40002}
    after_common = seed_common(holding, 40002, serial=serial, model=model)
    layout["inverter"] = after_common
    after_inverter = seed_inverter_model(holding, after_common, model_id=model_id)
    if mppt_units is not None:
        layout["mppt"] = after_inverter
        after_inverter = seed_mppt_model(holding, after_inverter, units=mppt_units)
    layout["chain_end"] = after_inverter
    holding[after_inverter] = END_OF_CHAIN
    return layout


def seed_meter(
    unit: Any,
    *,
    meter_id: int = 1,
    model_id: int = 203,
    mppt_units: int | None = None,
    serial: str | None = None,
) -> int:
    """Seed a meter at the slot SolarEdge puts meter ``meter_id`` in.

    The slot moves when the inverter publishes model 160, exactly as it does on
    real hardware, so pass the same ``mppt_units`` used for the inverter.
    """
    base = {1: 40121, 2: 40295, 3: 40469}[meter_id]
    if mppt_units is not None:
        base += 8 + mppt_units * 20 + 2
    holding = unit.holding
    after_common = seed_common(
        holding,
        base,
        manufacturer="SolarEdge",
        model="SE-RGMTR-1D-240C-A",
        serial=serial or f"3F2A{meter_id:04d}",
        device_address=meter_id,
    )
    after_meter = seed_meter_model(holding, after_common, model_id=model_id)
    holding[after_meter] = END_OF_CHAIN
    return base


def seed_battery(
    unit: Any, *, battery_id: int = 1, rated_energy: float = 9800.0
) -> int:
    """Seed a battery in one of the three proprietary slots. All CDAB."""
    base = {1: 57600, 2: 57856, 3: 58368}[battery_id]
    holding = unit.holding
    holding[base] = string_registers("SolarEdge", 16)
    holding[base + 16] = string_registers("48V-9.8kWh", 16)
    holding[base + 32] = string_registers("1.2.3", 16)
    holding[base + 48] = string_registers(f"BATT{battery_id:04d}", 16)
    holding[base + 64] = battery_id
    holding[base + 66] = f32(rated_energy)
    holding[base + 68] = f32(5000.0)  # MaxChargePower
    holding[base + 70] = f32(5000.0)  # MaxDischargePower
    holding[base + 72] = f32(6000.0)  # MaxChargePeakPower
    holding[base + 74] = f32(6000.0)  # MaxDischargePeakPower
    holding[base + 108] = f32(24.5)  # TempAverage
    holding[base + 110] = f32(26.0)  # TempMax
    holding[base + 112] = f32(52.4)  # DCVoltage
    holding[base + 114] = f32(-12.5)  # DCCurrent
    holding[base + 116] = f32(-655.0)  # DCPower
    holding[base + 118] = u64(4_000_000, little=True)  # ExportEnergyWh
    holding[base + 122] = u64(4_500_000, little=True)  # ImportEnergyWh
    holding[base + 126] = f32(rated_energy)  # EnergyMax
    holding[base + 128] = f32(7350.0)  # EnergyAvailable
    holding[base + 130] = f32(99.0)  # SOH
    holding[base + 132] = f32(75.0)  # SOE
    holding[base + 134] = u32(4, little=True)  # Status
    holding[base + 136] = u32(0, little=True)  # StatusVendor
    holding[base + 138] = [0] * 16  # event logs
    return base


def seed_mppt(unit: Any, *, units: int = 2) -> dict[str, int]:
    """Seed an inverter that publishes the multiple MPPT model."""
    return seed_inverter(unit, mppt_units=units)


def reads_at(unit: Any, address: int, *, since: int = 0) -> int:
    """How many reads covered an address, counting from a point in the log."""
    return sum(
        1
        for event in unit.read_events[since:]
        if event.address <= address < event.address + event.count
    )
