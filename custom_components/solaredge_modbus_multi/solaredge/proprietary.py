"""SolarEdge's proprietary register blocks.

Everything here sits outside the SunSpec map, in the 0xE000 and 0xF000 ranges,
and is documented only in SolarEdge's application notes. Two things are true of
all of it and of none of the SunSpec blocks:

* the word order is **CDAB** — multi-register values arrive with their words
  swapped, so every field wider than one register declares
  ``word_order="little"``;
* a block is present only on some firmware and some hardware, so each one is
  its own :class:`~modbus_connection.model.Component` and is polled on its own.
  Pooling them would mean one absent block failing every other block's read.

Unimplemented 32-bit floats read back as the SunSpec ``0x7FC00000`` NaN, which
``nan=`` turns into ``None``.
"""

from __future__ import annotations

from typing import Any, Final

from modbus_connection.model import (
    Component,
    FloatField,
    bit,
    bits,
    float32,
    int32,
    integer,
    uint32,
    uint64,
)

from .fields import se_string

# SunSpec's "not implemented" float, which SolarEdge also uses in its own blocks.
NOT_IMPLEMENTED_FLOAT32: Final = 0x7FC00000


def _f32(address: int, **kwargs: Any) -> FloatField:
    """Declare a CDAB 32-bit float with SolarEdge's unimplemented sentinel."""
    return float32(address, word_order="little", nan=NOT_IMPLEMENTED_FLOAT32, **kwargs)


class SiteLimit(Component):
    """Export/site limitation control (0xE000).

    ``e_lim_ctl_mode`` packs five settings and eleven reserved bits into one
    register, so each setting is declared as the bits it owns. Writing one
    re-reads the register and merges, leaving the rest — including the reserved
    bits, whose contents this model cannot know — alone. SolarEdge does not
    serve FC 0x16 (mask write), so the read-modify-write cannot be made atomic;
    what re-reading buys is a window of one round trip rather than a whole poll
    interval in which another writer's change could be lost.
    """

    e_lim_ctl_mode = integer(0, signed=False)
    """The whole limit control mode word, for diagnostics. Written per setting."""

    limit_mode = bits(0, 0, 3, writable=True)
    """Bits 0-2: which quantity is limited; zero is off.

    One field rather than three, because the device allows only a single
    selection: changing it has to clear two bits and set one in the same write,
    or it passes through a state SolarEdge documents as forbidden.
    """

    external_production = bit(0, 10, writable=True)
    """Bit 10: a non-SolarEdge power source is present on the site."""

    negative_limit = bit(0, 11, writable=True)
    """Bit 11: the limit is a minimum import rather than a maximum export."""

    e_lim_ctl = integer(1, signed=False, writable=True)
    """Limit control: which meter/connection the limit is measured at."""

    e_site_limit = _f32(2, writable=True)
    """The site limit itself, in the unit selected by the mode."""


class ExternalProductionMax(Component):
    """External production maximum power (0xE012).

    A separate component from :class:`SiteLimit` even though it is 18 registers
    further into the same range: inverters that serve the site limit block do
    not all serve this one.
    """

    ext_prod_max = _f32(0, writable=True)
    """Maximum power of external (non-SolarEdge) production."""


class StorageControl(Component):
    """Battery charge/discharge control (0xE004).

    These registers are flash-backed on the inverter. Writing them repeatedly
    wears the flash out, which is why the integration hides them behind an
    explicit opt-in.
    """

    control_mode = integer(0, signed=False, writable=True)
    """Storage control mode."""

    ac_charge_policy = integer(1, signed=False, writable=True)
    """AC charge policy."""

    ac_charge_limit = _f32(2, writable=True)
    """AC charge limit, in kWh or percent depending on the policy."""

    backup_reserve = _f32(4, writable=True)
    """Backup reserve, in percent of battery capacity."""

    default_mode = integer(6, signed=False, writable=True)
    """Default storage charge/discharge mode."""

    command_timeout = uint32(7, word_order="little", writable=True)
    """Seconds the remote command stays in effect before reverting."""

    command_mode = integer(9, signed=False, writable=True)
    """Remote storage charge/discharge command mode."""

    charge_limit = _f32(10, writable=True)
    """Charge power limit, in watts."""

    discharge_limit = _f32(12, writable=True)
    """Discharge power limit, in watts."""


class GlobalPowerControl(Component):
    """Global dynamic power control and status (0xF000)."""

    rrcr = integer(0, signed=False)
    """RRCR state: the ripple-control receiver input bits."""

    power_limit = integer(1, signed=False, writable=True)
    """Active power limit, in percent of nominal."""

    cos_phi = _f32(2, writable=True)
    """Fixed power factor setpoint."""


class AdvancedPowerControl(Component):
    """Advanced power control, first block (0xF100).

    86 registers of grid-support configuration. Most of it is never surfaced as
    an entity; it is read so that a diagnostics download shows the inverter's
    full grid profile.
    """

    commit_pwr_ctl_settings = integer(0, writable=True)
    """Write 1 to commit the power control settings to flash."""

    restore_pwr_ctl_defaults = integer(1, writable=True)
    """Write 1 to restore the factory power control defaults."""

    pwr_frq_derating_config = int32(2, word_order="little")
    """Power/frequency derating configuration."""

    reactive_pwr_config = int32(4, word_order="little", writable=True)
    """Reactive power configuration mode."""

    react_pwr_iter_time = uint32(6, word_order="little")
    """Reactive power iteration time, in milliseconds."""

    active_pwr_grad = int32(8, word_order="little")
    """Active power gradient."""

    fixed_cos_phi_phase = _f32(10)
    """Fixed cos-phi phase."""

    fixed_react_pwr = _f32(12)
    """Fixed reactive power."""

    react_cos_phi_vs_p_x_0 = _f32(14)
    react_cos_phi_vs_p_x_1 = _f32(16)
    react_cos_phi_vs_p_x_2 = _f32(18)
    react_cos_phi_vs_p_x_3 = _f32(20)
    react_cos_phi_vs_p_x_4 = _f32(22)
    react_cos_phi_vs_p_x_5 = _f32(24)
    react_cos_phi_vs_p_y_0 = _f32(26)
    react_cos_phi_vs_p_y_1 = _f32(28)
    react_cos_phi_vs_p_y_2 = _f32(30)
    react_cos_phi_vs_p_y_3 = _f32(32)
    react_cos_phi_vs_p_y_4 = _f32(34)
    react_cos_phi_vs_p_y_5 = _f32(36)
    react_q_vs_vg_x_0 = _f32(38)
    react_q_vs_vg_x_1 = _f32(40)
    react_q_vs_vg_x_2 = _f32(42)
    react_q_vs_vg_x_3 = _f32(44)
    react_q_vs_vg_x_4 = _f32(46)
    react_q_vs_vg_x_5 = _f32(48)
    react_q_vs_vg_y_0 = _f32(50)
    react_q_vs_vg_y_1 = _f32(52)
    react_q_vs_vg_y_2 = _f32(54)
    react_q_vs_vg_y_3 = _f32(56)
    react_q_vs_vg_y_4 = _f32(58)
    react_q_vs_vg_y_5 = _f32(60)

    frt_k_factor = _f32(62)
    """Fault ride-through K factor."""

    power_reduce = _f32(64, writable=True)
    """Power reduction, in percent."""

    adv_pwr_ctrl_en = int32(66, word_order="little", writable=True)
    """Advanced power control master enable."""

    frt_en = int32(68, word_order="little")
    """Fault ride-through enable."""

    max_wakeup_freq = _f32(70)
    min_wakeup_freq = _f32(72)
    max_wakeup_vg = _f32(74)
    min_wakeup_vg = _f32(76)

    vnom = _f32(78)
    """Nominal grid voltage."""

    inom = _f32(80)
    """Nominal grid current."""

    pwr_vs_freq_x_0 = _f32(82)
    pwr_vs_freq_x_1 = _f32(84)


class AdvancedPowerControl2(Component):
    """Advanced power control, second block (0xF156).

    The continuation of :class:`AdvancedPowerControl`; it is a separate read
    because the two together are 170 registers, past the 125-register ceiling
    on a single Modbus request.
    """

    pwr_vs_freq_y_0 = _f32(0)
    pwr_vs_freq_y_1 = _f32(2)

    reset_freq = _f32(4)
    max_freq = _f32(6)

    react_q_vs_p_x_0 = _f32(8)
    react_q_vs_p_x_1 = _f32(10)
    react_q_vs_p_x_2 = _f32(12)
    react_q_vs_p_x_3 = _f32(14)
    react_q_vs_p_x_4 = _f32(16)
    react_q_vs_p_x_5 = _f32(18)
    react_q_vs_p_y_0 = _f32(20)
    react_q_vs_p_y_1 = _f32(22)
    react_q_vs_p_y_2 = _f32(24)
    react_q_vs_p_y_3 = _f32(26)
    react_q_vs_p_y_4 = _f32(28)
    react_q_vs_p_y_5 = _f32(30)

    pwr_frq_derating_reset_time = uint32(32, word_order="little")
    """Power/frequency derating reset time, in milliseconds."""

    pwr_frq_derating_grad_time = uint32(34, word_order="little")
    """Power/frequency derating gradient time, in milliseconds."""

    react_cos_phi_vs_p_vg_lock_in_max = _f32(36)
    react_cos_phi_vs_p_vg_lock_in_min = _f32(38)
    react_cos_phi_vs_p_vg_lock_out_max = _f32(40)
    react_cos_phi_vs_p_vg_lock_out_min = _f32(42)
    react_q_vs_vg_p_lock_in_max = _f32(44)
    react_q_vs_vg_p_lock_in_min = _f32(46)
    react_q_vs_vg_p_lock_out_max = _f32(48)
    react_q_vs_vg_p_lock_out_min = _f32(50)

    react_q_vs_vg_type = uint32(52, word_order="little")
    """Reactive-power-versus-grid-voltage curve type."""

    pwr_soft_start_time = uint32(54, word_order="little")
    """Power soft-start time, in milliseconds."""

    max_current = _f32(56, writable=True)
    """Maximum inverter current."""

    pwr_vs_vg_x_0 = _f32(58)
    pwr_vs_vg_x_1 = _f32(60)
    pwr_vs_vg_x_2 = _f32(62)
    pwr_vs_vg_x_3 = _f32(64)
    pwr_vs_vg_x_4 = _f32(66)
    pwr_vs_vg_x_5 = _f32(68)
    pwr_vs_vg_y_0 = _f32(70)
    pwr_vs_vg_y_1 = _f32(72)
    pwr_vs_vg_y_2 = _f32(74)
    pwr_vs_vg_y_3 = _f32(76)
    pwr_vs_vg_y_4 = _f32(78)
    pwr_vs_vg_y_5 = _f32(80)

    disconnect_at_zero_pwr_lim = _f32(82)
    """Whether the inverter disconnects at a zero power limit."""


class Battery(Component):
    """A StorEdge battery, at one of the three battery base addresses.

    Identity and measurements are one declared block with a 32-register hole in
    the middle that the battery does not map. Stating the two halves as
    readable ranges keeps the planner from bridging the hole, which it would
    otherwise be free to do — and the block is wider than a single Modbus
    request either way.
    """

    register_ranges = ((0, 75), (108, 153))

    manufacturer = se_string(0, 16)
    """Battery manufacturer, with the serial number appended on some models."""

    model = se_string(16, 16)
    """Battery model, likewise."""

    version = se_string(32, 16)
    """Battery firmware version."""

    serial = se_string(48, 16)
    """Battery serial number."""

    device_address = integer(64, signed=False)
    """Modbus device address the battery answers on."""

    rated_energy = _f32(66, unit="Wh")
    """Nameplate energy. A battery reporting zero or NaN here is not usable."""

    max_charge_power = _f32(68, unit="W")
    max_discharge_power = _f32(70, unit="W")
    max_charge_peak_power = _f32(72, unit="W")
    max_discharge_peak_power = _f32(74, unit="W")

    temp_average = _f32(108, unit="C")
    temp_max = _f32(110, unit="C")

    dc_voltage = _f32(112, unit="V")
    dc_current = _f32(114, unit="A")
    dc_power = _f32(116, unit="W")

    export_energy_wh = uint64(118, word_order="little", unit="Wh")
    """Lifetime energy discharged."""

    import_energy_wh = uint64(122, word_order="little", unit="Wh")
    """Lifetime energy charged."""

    energy_max = _f32(126, unit="Wh")
    energy_available = _f32(128, unit="Wh")

    soh = _f32(130, unit="%")
    """State of health."""

    soe = _f32(132, unit="%")
    """State of energy."""

    status = uint32(134, word_order="little")
    status_vendor = uint32(136, word_order="little")

    event_log_1 = integer(138, signed=False)
    event_log_2 = integer(139, signed=False)
    event_log_3 = integer(140, signed=False)
    event_log_4 = integer(141, signed=False)
    event_log_5 = integer(142, signed=False)
    event_log_6 = integer(143, signed=False)
    event_log_7 = integer(144, signed=False)
    event_log_8 = integer(145, signed=False)
    event_log_vendor_1 = integer(146, signed=False)
    event_log_vendor_2 = integer(147, signed=False)
    event_log_vendor_3 = integer(148, signed=False)
    event_log_vendor_4 = integer(149, signed=False)
    event_log_vendor_5 = integer(150, signed=False)
    event_log_vendor_6 = integer(151, signed=False)
    event_log_vendor_7 = integer(152, signed=False)
    event_log_vendor_8 = integer(153, signed=False)
