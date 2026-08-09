"""SunSpec models as SolarEdge actually publishes them.

The field addresses and types come from the official SunSpec definitions, via

    python -m modbus_connection.model.sunspec.generate 1 103 160 203

Models 101/102/103 share one register layout, as do 201/202/203/204, so one
class covers each family and the model ID selects which points mean anything.
Three deliberate departures from the generated output:

* Identity strings use :class:`SolarEdgeStringField`, because SolarEdge's are
  not the clean ASCII the spec describes.
* The inverter model stops at ``st_vnd`` (offset 39). The six event registers
  behind it are read as separate components, because SolarEdge repurposes two
  of them and firmware that doesn't serve them answers with an exception —
  which would otherwise fail the whole block read.
* Each scale-factor register is declared as its own ``sunssf`` point as well as
  being referenced by the points it scales, so entities can report the device's
  own precision.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag

from modbus_connection.model import Component, repeating_group, uint32
from modbus_connection.model.sunspec import (
    SunSpecComponent,
    acc32,
    bitfield32,
    enum16,
    int16,
    sunssf,
    uint16,
)

from .fields import se_string


class OperatingState(IntEnum):
    """SunSpec inverter operating state (point ``St``)."""

    OFF = 1
    SLEEPING = 2
    STARTING = 3
    MPPT = 4
    THROTTLED = 5
    SHUTTING_DOWN = 6
    FAULT = 7
    STANDBY = 8


class MpptModuleOperatingState(IntEnum):
    """SunSpec MPPT module operating state (point ``DCSt``)."""

    OFF = 1
    SLEEPING = 2
    STARTING = 3
    MPPT = 4
    THROTTLED = 5
    SHUTTING_DOWN = 6
    FAULT = 7
    STANDBY = 8
    TEST = 9
    RESERVED_10 = 10


class MeterEvents(IntFlag):
    """SunSpec meter event flags (point ``Evt``)."""

    POWER_FAILURE = 1 << 2
    UNDER_VOLTAGE = 1 << 3
    LOW_PF = 1 << 4
    OVER_CURRENT = 1 << 5
    OVER_VOLTAGE = 1 << 6
    MISSING_SENSOR = 1 << 7


class Common(SunSpecComponent):
    """SunSpec model 1: the identity block every device starts with."""

    mn = se_string(2, 16)
    """Manufacturer."""

    md = se_string(18, 16)
    """Model."""

    opt = se_string(34, 8)
    """Options."""

    vr = se_string(42, 8)
    """Version. The one point here that changes over a device's life."""

    sn = se_string(50, 16)
    """Serial number."""

    da = uint16(66)
    """Modbus device address."""


class Inverter(SunSpecComponent):
    """SunSpec models 101/102/103: the inverter's measurements."""

    a = uint16(2, scale_register=6, unit="A")
    """AC current."""

    aph_a = uint16(3, scale_register=6, unit="A")
    """Phase A current."""

    aph_b = uint16(4, scale_register=6, unit="A")
    """Phase B current."""

    aph_c = uint16(5, scale_register=6, unit="A")
    """Phase C current."""

    a_sf = sunssf(6)
    """Current scale factor."""

    pp_vph_ab = uint16(7, scale_register=13, unit="V")
    """Phase voltage AB."""

    pp_vph_bc = uint16(8, scale_register=13, unit="V")
    """Phase voltage BC."""

    pp_vph_ca = uint16(9, scale_register=13, unit="V")
    """Phase voltage CA."""

    ph_vph_a = uint16(10, scale_register=13, unit="V")
    """Phase voltage AN."""

    ph_vph_b = uint16(11, scale_register=13, unit="V")
    """Phase voltage BN."""

    ph_vph_c = uint16(12, scale_register=13, unit="V")
    """Phase voltage CN."""

    v_sf = sunssf(13)
    """Voltage scale factor."""

    w = int16(14, scale_register=15, unit="W")
    """AC power."""

    w_sf = sunssf(15)
    """AC power scale factor."""

    hz = uint16(16, scale_register=17, unit="Hz")
    """Line frequency."""

    hz_sf = sunssf(17)
    """Line frequency scale factor."""

    va = int16(18, scale_register=19, unit="VA")
    """AC apparent power."""

    va_sf = sunssf(19)
    """Apparent power scale factor."""

    v_ar = int16(20, scale_register=21, unit="var")
    """AC reactive power."""

    var_sf = sunssf(21)
    """Reactive power scale factor."""

    pf = int16(22, scale_register=23, unit="Pct")
    """AC power factor."""

    pf_sf = sunssf(23)
    """Power factor scale factor."""

    wh = acc32(24, scale_register=26, unit="Wh")
    """AC lifetime energy."""

    wh_sf = sunssf(26)
    """Energy scale factor."""

    dca = uint16(27, scale_register=28, unit="A")
    """DC current."""

    dca_sf = sunssf(28)
    """DC current scale factor."""

    dcv = uint16(29, scale_register=30, unit="V")
    """DC voltage."""

    dcv_sf = sunssf(30)
    """DC voltage scale factor."""

    dcw = int16(31, scale_register=32, unit="W")
    """DC power."""

    dcw_sf = sunssf(32)
    """DC power scale factor."""

    tmp_cab = int16(33, scale_register=37, unit="C")
    """Cabinet temperature."""

    tmp_snk = int16(34, scale_register=37, unit="C")
    """Heat sink temperature."""

    tmp_trns = int16(35, scale_register=37, unit="C")
    """Transformer temperature."""

    tmp_ot = int16(36, scale_register=37, unit="C")
    """Other temperature."""

    tmp_sf = sunssf(37)
    """Temperature scale factor."""

    st = enum16(38, OperatingState)
    """Operating state."""

    st_vnd = enum16(39)
    """Vendor operating state; SolarEdge's own error code."""


class GridStatus(Component):
    """SolarEdge's grid on/off flag, in the inverter model's ``Evt_Vnd1``.

    Placed with ``base_offset`` at the discovered inverter model, and read on
    its own because firmware without it answers the address with an exception.
    A CDAB word order inside an otherwise big-endian SunSpec block, which is
    why this cannot simply be a ``bitfield32`` on :class:`Inverter`.
    """

    grid_status = uint32(44, word_order="little")
    """Non-zero while the inverter is disconnected from the grid."""


class StatusVendor4(Component):
    """SolarEdge's extended error code, in the inverter model's ``Evt_Vnd4``.

    Only firmware 3.20.0 and newer publishes it, so it is read separately for
    the same reason as :class:`GridStatus`.
    """

    status_vendor4 = uint32(50)
    """Extended vendor status code."""


class MpptModule(Component):
    """One module block of SunSpec model 160.

    The scale factors it references live in model 160's own fixed block, not in
    the repeated block, so ``scale_in_block`` stays off and each instance reads
    the same four scale registers.
    """

    id = uint16(10)
    """Input ID."""

    id_str = se_string(11, 8)
    """Input ID string."""

    dca = uint16(19, scale_register=2, unit="A")
    """DC current."""

    dcv = uint16(20, scale_register=3, unit="V")
    """DC voltage."""

    dcw = uint16(21, scale_register=4, unit="W")
    """DC power."""

    dcwh = acc32(22, scale_register=5, unit="Wh")
    """Lifetime energy."""

    tms = uint32(24, unit="Secs")
    """Timestamp."""

    tmp = int16(26, unit="C")
    """Temperature."""

    dc_st = enum16(27, MpptModuleOperatingState)
    """Operating state."""

    dc_evt = bitfield32(28)
    """Module events."""


class Mppt(SunSpecComponent):
    """SunSpec model 160: the multiple MPPT inverter extension.

    ``n`` is read every poll and sizes ``module``; SolarEdge publishes 2 or 3.
    """

    dca_sf = sunssf(2)
    """Module DC current scale factor."""

    dcv_sf = sunssf(3)
    """Module DC voltage scale factor."""

    dcw_sf = sunssf(4)
    """Module DC power scale factor."""

    dcwh_sf = sunssf(5)
    """Module lifetime energy scale factor."""

    evt = bitfield32(6)
    """Global events."""

    n = uint16(8)
    """Number of modules."""

    tms_per = uint16(9)
    """Timestamp period."""

    module = repeating_group(uint16(8), MpptModule, stride=20)
    """One :class:`MpptModule` per module the inverter reports."""


class Meter(SunSpecComponent):
    """SunSpec models 201/202/203/204: a revenue-grade meter."""

    a = int16(2, scale_register=6, unit="A")
    """Total AC current."""

    aph_a = int16(3, scale_register=6, unit="A")
    """Phase A current."""

    aph_b = int16(4, scale_register=6, unit="A")
    """Phase B current."""

    aph_c = int16(5, scale_register=6, unit="A")
    """Phase C current."""

    a_sf = sunssf(6)
    """Current scale factor."""

    ph_v = int16(7, scale_register=15, unit="V")
    """Line-to-neutral voltage."""

    ph_vph_a = int16(8, scale_register=15, unit="V")
    """Phase voltage AN."""

    ph_vph_b = int16(9, scale_register=15, unit="V")
    """Phase voltage BN."""

    ph_vph_c = int16(10, scale_register=15, unit="V")
    """Phase voltage CN."""

    ppv = int16(11, scale_register=15, unit="V")
    """Line-to-line voltage."""

    ph_vph_ab = int16(12, scale_register=15, unit="V")
    """Phase voltage AB."""

    ph_vph_bc = int16(13, scale_register=15, unit="V")
    """Phase voltage BC."""

    ph_vph_ca = int16(14, scale_register=15, unit="V")
    """Phase voltage CA."""

    v_sf = sunssf(15)
    """Voltage scale factor."""

    hz = int16(16, scale_register=17, unit="Hz")
    """Line frequency."""

    hz_sf = sunssf(17)
    """Line frequency scale factor."""

    w = int16(18, scale_register=22, unit="W")
    """Total real power."""

    wph_a = int16(19, scale_register=22, unit="W")
    """Phase A real power."""

    wph_b = int16(20, scale_register=22, unit="W")
    """Phase B real power."""

    wph_c = int16(21, scale_register=22, unit="W")
    """Phase C real power."""

    w_sf = sunssf(22)
    """Real power scale factor."""

    va = int16(23, scale_register=27, unit="VA")
    """Total apparent power."""

    v_aph_a = int16(24, scale_register=27, unit="VA")
    """Phase A apparent power."""

    v_aph_b = int16(25, scale_register=27, unit="VA")
    """Phase B apparent power."""

    v_aph_c = int16(26, scale_register=27, unit="VA")
    """Phase C apparent power."""

    va_sf = sunssf(27)
    """Apparent power scale factor."""

    var = int16(28, scale_register=32, unit="var")
    """Total reactive power."""

    va_rph_a = int16(29, scale_register=32, unit="var")
    """Phase A reactive power."""

    va_rph_b = int16(30, scale_register=32, unit="var")
    """Phase B reactive power."""

    va_rph_c = int16(31, scale_register=32, unit="var")
    """Phase C reactive power."""

    var_sf = sunssf(32)
    """Reactive power scale factor."""

    pf = int16(33, scale_register=37, unit="Pct")
    """Average power factor."""

    p_fph_a = int16(34, scale_register=37, unit="Pct")
    """Phase A power factor."""

    p_fph_b = int16(35, scale_register=37, unit="Pct")
    """Phase B power factor."""

    p_fph_c = int16(36, scale_register=37, unit="Pct")
    """Phase C power factor."""

    pf_sf = sunssf(37)
    """Power factor scale factor."""

    tot_wh_exp = acc32(38, scale_register=54, unit="Wh")
    """Total real energy exported."""

    tot_wh_exp_ph_a = acc32(40, scale_register=54, unit="Wh")
    """Phase A real energy exported."""

    tot_wh_exp_ph_b = acc32(42, scale_register=54, unit="Wh")
    """Phase B real energy exported."""

    tot_wh_exp_ph_c = acc32(44, scale_register=54, unit="Wh")
    """Phase C real energy exported."""

    tot_wh_imp = acc32(46, scale_register=54, unit="Wh")
    """Total real energy imported."""

    tot_wh_imp_ph_a = acc32(48, scale_register=54, unit="Wh")
    """Phase A real energy imported."""

    tot_wh_imp_ph_b = acc32(50, scale_register=54, unit="Wh")
    """Phase B real energy imported."""

    tot_wh_imp_ph_c = acc32(52, scale_register=54, unit="Wh")
    """Phase C real energy imported."""

    tot_wh_sf = sunssf(54)
    """Real energy scale factor."""

    tot_v_ah_exp = acc32(55, scale_register=71, unit="VAh")
    """Total apparent energy exported."""

    tot_v_ah_exp_ph_a = acc32(57, scale_register=71, unit="VAh")
    """Phase A apparent energy exported."""

    tot_v_ah_exp_ph_b = acc32(59, scale_register=71, unit="VAh")
    """Phase B apparent energy exported."""

    tot_v_ah_exp_ph_c = acc32(61, scale_register=71, unit="VAh")
    """Phase C apparent energy exported."""

    tot_v_ah_imp = acc32(63, scale_register=71, unit="VAh")
    """Total apparent energy imported."""

    tot_v_ah_imp_ph_a = acc32(65, scale_register=71, unit="VAh")
    """Phase A apparent energy imported."""

    tot_v_ah_imp_ph_b = acc32(67, scale_register=71, unit="VAh")
    """Phase B apparent energy imported."""

    tot_v_ah_imp_ph_c = acc32(69, scale_register=71, unit="VAh")
    """Phase C apparent energy imported."""

    tot_v_ah_sf = sunssf(71)
    """Apparent energy scale factor."""

    tot_v_arh_imp_q1 = acc32(72, scale_register=104, unit="varh")
    """Total reactive energy imported, quadrant 1."""

    tot_v_arh_imp_q1_ph_a = acc32(74, scale_register=104, unit="varh")
    """Phase A reactive energy imported, quadrant 1."""

    tot_v_arh_imp_q1_ph_b = acc32(76, scale_register=104, unit="varh")
    """Phase B reactive energy imported, quadrant 1."""

    tot_v_arh_imp_q1_ph_c = acc32(78, scale_register=104, unit="varh")
    """Phase C reactive energy imported, quadrant 1."""

    tot_v_arh_imp_q2 = acc32(80, scale_register=104, unit="varh")
    """Total reactive energy imported, quadrant 2."""

    tot_v_arh_imp_q2_ph_a = acc32(82, scale_register=104, unit="varh")
    """Phase A reactive energy imported, quadrant 2."""

    tot_v_arh_imp_q2_ph_b = acc32(84, scale_register=104, unit="varh")
    """Phase B reactive energy imported, quadrant 2."""

    tot_v_arh_imp_q2_ph_c = acc32(86, scale_register=104, unit="varh")
    """Phase C reactive energy imported, quadrant 2."""

    tot_v_arh_exp_q3 = acc32(88, scale_register=104, unit="varh")
    """Total reactive energy exported, quadrant 3."""

    tot_v_arh_exp_q3_ph_a = acc32(90, scale_register=104, unit="varh")
    """Phase A reactive energy exported, quadrant 3."""

    tot_v_arh_exp_q3_ph_b = acc32(92, scale_register=104, unit="varh")
    """Phase B reactive energy exported, quadrant 3."""

    tot_v_arh_exp_q3_ph_c = acc32(94, scale_register=104, unit="varh")
    """Phase C reactive energy exported, quadrant 3."""

    tot_v_arh_exp_q4 = acc32(96, scale_register=104, unit="varh")
    """Total reactive energy exported, quadrant 4."""

    tot_v_arh_exp_q4_ph_a = acc32(98, scale_register=104, unit="varh")
    """Phase A reactive energy exported, quadrant 4."""

    tot_v_arh_exp_q4_ph_b = acc32(100, scale_register=104, unit="varh")
    """Phase B reactive energy exported, quadrant 4."""

    tot_v_arh_exp_q4_ph_c = acc32(102, scale_register=104, unit="varh")
    """Phase C reactive energy exported, quadrant 4."""

    tot_v_arh_sf = sunssf(104)
    """Reactive energy scale factor."""

    evt = bitfield32(105, MeterEvents)
    """Meter event flags."""
