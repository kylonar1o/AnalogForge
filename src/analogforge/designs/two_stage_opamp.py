"""Two-stage Miller-compensated CMOS operational amplifier.

Topology::

    stage 1: NMOS differential pair M1 (gate = inn) / M2 (gate = inp), PMOS current-mirror load
             M3 (diode, n1) / M4 (output n2), NMOS tail M5 mirrored from M8 (IBIAS)
    stage 2: PMOS common source M6 (gate = n2) with NMOS current sink M7 (mirror of M8)
    compensation: R_Z + C_C from n2 to out, load C_L

Testbenches:

* ``ol``   - open loop: DC unity feedback through a 1 GH inductor (inn AC-grounded by 1 F),
             so the output sits at the input common mode while AC sees the open-loop gain.
* ``psrr`` - same, AC source on VDD.
* ``tran`` - unity-gain follower driven by a pulse: slew rate and 1 % settling time.

Hand estimates (``extra.hand_estimates``) use the simulated gm/gds:
A1 = gm1/(gds2+gds4), A2 = gm6/(gds6+gds7), GBW = gm1/(2 pi C_C).
"""

from __future__ import annotations

import math

import numpy as np

from ..core.analyses import AC, Op, Tran
from ..core.circuit import Circuit, Pulse
from ..core.design import Curve, Design, DesignContext, Measurement, MetricSpec, ParamSpec
from ..core.testbench import Testbench
from ..metrics import amplifier as amp

NAN = float("nan")


class TwoStageOpamp(Design):
    name = "two_stage_opamp"
    title = "Two-stage Miller-compensated op-amp"
    kind = "amplifier"
    description = __doc__
    parameters = [
        ParamSpec("VDD", 1.8, 1.6, 2.0, "V", description="supply voltage", tunable=False),
        ParamSpec("VCM", 1.0, 0.7, 1.3, "V", description="input common-mode voltage", tunable=False),
        ParamSpec("IBIAS", 20e-6, 5e-6, 100e-6, "A", log=True, description="reference current into M8"),
        ParamSpec("W1", 4e-6, 1e-6, 40e-6, "m", log=True, description="input pair width (M1, M2)"),
        ParamSpec("L1", 0.5e-6, 0.15e-6, 2e-6, "m", log=True, description="input pair length"),
        ParamSpec("W3", 4e-6, 1e-6, 40e-6, "m", log=True, description="PMOS load width (M3, M4)"),
        ParamSpec("L3", 0.5e-6, 0.15e-6, 2e-6, "m", log=True, description="PMOS load / M6 length"),
        ParamSpec("W5", 8e-6, 1e-6, 80e-6, "m", log=True, description="tail current source width"),
        ParamSpec("W6", 20e-6, 2e-6, 100e-6, "m", log=True, description="second-stage PMOS width"),
        ParamSpec("W7", 20e-6, 2e-6, 200e-6, "m", log=True, description="second-stage NMOS sink width"),
        ParamSpec("W8", 4e-6, 1e-6, 20e-6, "m", description="bias diode width", tunable=False),
        ParamSpec("LB", 1e-6, 0.5e-6, 4e-6, "m", description="length of bias devices M5, M7, M8"),
        ParamSpec("CC", 1e-12, 0.1e-12, 5e-12, "F", log=True, description="Miller capacitor"),
        ParamSpec("RZ", 2e3, 0.0, 20e3, "Ohm", description="nulling resistor (0 = none)"),
        ParamSpec("CL", 1e-12, 0.1e-12, 10e-12, "F", description="load capacitance", tunable=False),
        ParamSpec("VSTEP", 0.4, 0.05, 0.8, "V", description="follower step amplitude", tunable=False),
    ]
    metrics = [
        MetricSpec("gain_db", "dB", "open-loop DC gain", "max"),
        MetricSpec("gain", "V/V", "open-loop DC gain"),
        MetricSpec("f3db", "Hz", "open-loop -3 dB bandwidth"),
        MetricSpec("ugbw", "Hz", "unity-gain bandwidth", "max"),
        MetricSpec("phase_margin", "deg", "phase margin", "max"),
        MetricSpec("gain_margin", "dB", "gain margin", "max"),
        MetricSpec("power", "W", "static power", "min"),
        MetricSpec("slew_rate", "V/s", "follower slew rate (min of rise/fall)", "max"),
        MetricSpec("settling_time", "s", "follower 1 % settling time after the rising step", "min"),
        MetricSpec("psrr_db", "dB", "PSRR+ at low frequency (gain / supply gain)", "max"),
        MetricSpec("offset", "V", "systematic output offset V(out)-VCM in unity feedback"),
        MetricSpec("i_total", "A", "total supply current"),
    ]
    options = {"tran": True, "psrr": True}
    default_objective = "max:ugbw"
    default_constraints = ["phase_margin >= 60", "gain_db >= 60", "power <= 0.5e-3"]

    # ---------------------------------------------------------------- circuit
    def circuit(self, p, ctx: DesignContext, mode: str) -> Circuit:
        vdd = p["VDD"] * ctx.vdd_scale
        c = Circuit(self.name)
        c.vsource("VDD", "vdd", dc=vdd, ac=1.0 if mode == "psrr" else 0.0)
        c.vsource("VCM", "cm", dc=p["VCM"])
        if mode == "tran":
            lo, hi = p["VCM"] - p["VSTEP"] / 2, p["VCM"] + p["VSTEP"] / 2
            c.vsource("VIN", "inp", "0", dc=lo, waveform=Pulse(lo, hi, 20e-9, 0.5e-9, 0.5e-9, 230e-9, 500e-9))
            inn = "out"  # unity-gain follower
        else:
            c.vsource("VIN", "inp", "cm", dc=0.0, ac=1.0 if mode == "ol" else 0.0)
            inn = "inn"
            c.inductor("LFB", "out", "inn", 1e9)     # DC unity feedback
            c.capacitor("CFB", "inn", "0", 1.0)      # AC ground of inn
        c.isource("IBIAS", "vdd", "nb", dc=p["IBIAS"])
        c.nmos("M8", "nb", "nb", "0", "0", p["W8"], p["LB"])
        c.nmos("M5", "tail", "nb", "0", "0", p["W5"], p["LB"])
        c.nmos("M1", "n1", inn, "tail", "0", p["W1"], p["L1"])
        c.nmos("M2", "n2", "inp", "tail", "0", p["W1"], p["L1"])
        c.pmos("M3", "n1", "n1", "vdd", "vdd", p["W3"], p["L3"])
        c.pmos("M4", "n2", "n1", "vdd", "vdd", p["W3"], p["L3"])
        c.pmos("M6", "out", "n2", "vdd", "vdd", p["W6"], p["L3"])
        c.nmos("M7", "out", "nb", "0", "0", p["W7"], p["LB"])
        if p["RZ"] > 0:
            c.resistor("RZ", "n2", "nz", p["RZ"])
            c.capacitor("CC", "nz", "out", p["CC"])
        else:
            c.capacitor("CC", "n2", "out", p["CC"])
        c.capacitor("CL", "out", "0", p["CL"])
        return c

    def testbenches(self, p, ctx):
        tbs = [Testbench("ol", self.circuit(p, ctx, "ol"),
                         [Op("op"), AC("ac", fstart=1.0, fstop=10e9, points_per_decade=20)],
                         save_op=["M1", "M2", "M4", "M5", "M6", "M7"], description="open-loop gain",
                         variables=dict(p), probes=["v(out)", "v(inp)", "v(inn)", "i(VDD)"])]
        if ctx.option("psrr", True):
            tbs.append(Testbench("psrr", self.circuit(p, ctx, "psrr"),
                                 [AC("ac", fstart=1.0, fstop=10e9, points_per_decade=10)],
                                 description="supply rejection", variables=dict(p), probes=["v(out)"]))
        if ctx.option("tran", True):
            tbs.append(Testbench("tran", self.circuit(p, ctx, "tran"),
                                 [Tran("tran", tstop=500e-9, tstep=0.2e-9, tmax=0.2e-9)],
                                 description="unity-gain follower step response", variables=dict(p),
                                 probes=["v(out)", "v(inp)"]))
        return tbs

    # ---------------------------------------------------------------- measure
    def measure(self, p, results, ctx):
        meas = Measurement()
        m = meas.metrics
        for spec in self.metrics:
            m[spec.name] = NAN
        vdd = p["VDD"] * ctx.vdd_scale
        ol = results.get("ol")
        h = None
        if ol is not None and ol.get("op") is not None:
            op = ol["op"]
            m["i_total"] = abs(op.scalar("i(vdd)"))
            m["power"] = vdd * m["i_total"]
            m["offset"] = op.scalar("v(out)") - p["VCM"]
            devs = {}
            for d in ("m1", "m2", "m4", "m5", "m6", "m7"):
                devs[d] = {k.split(":")[1]: float(np.real(v.reshape(-1)[0]))
                           for k, v in op.signals.items() if k.startswith(d + ":")}
            meas.extra["op"] = devs
            try:
                a1 = devs["m2"]["gm"] / (devs["m2"]["gds"] + devs["m4"]["gds"])
                a2 = devs["m6"]["gm"] / (devs["m6"]["gds"] + devs["m7"]["gds"])
                meas.extra["hand_estimates"] = {
                    "a1": a1, "a2": a2, "gain_db": 20 * math.log10(a1 * a2),
                    "gbw": devs["m2"]["gm"] / (2 * math.pi * p["CC"]),
                    "p2": devs["m6"]["gm"] / (2 * math.pi * p["CL"]),
                    "slew_rate_internal": devs["m5"]["id"] / p["CC"],
                }
            except (KeyError, ZeroDivisionError):
                pass
        if ol is not None and ol.get("ac") is not None:
            ds = ol["ac"]
            f = ds.sweep
            h = ds.v("out") / (ds.v("inp") - ds.v("inn"))
            m["gain"] = amp.dc_gain(f, h)
            m["gain_db"] = float(amp.db20(m["gain"]))
            m["f3db"] = amp.bandwidth_3db(f, h)
            m["ugbw"] = amp.unity_gain_frequency(f, h)
            m["phase_margin"] = amp.phase_margin(f, h)
            m["gain_margin"] = amp.gain_margin(f, h)
            meas.curves.append(Curve("bode_mag", "Open-loop gain", f, {"|A| (dB)": amp.db20(h)},
                                     "Frequency (Hz)", "Gain (dB)", xlog=True))
            meas.curves.append(Curve("bode_phase", "Open-loop phase", f, {"phase (deg)": amp.phase_deg(h)},
                                     "Frequency (Hz)", "Phase (deg)", xlog=True))
        ps = results.get("psrr")
        if ps is not None and ps.get("ac") is not None and h is not None:
            hv = ps["ac"].v("out")
            m["psrr_db"] = float(amp.db20(m["gain"]) - amp.db20(abs(hv[0])))
        tr = results.get("tran")
        if tr is not None and tr.get("tran") is not None:
            ds = tr["tran"]
            t, vout = ds.sweep, np.real(ds.v("out"))
            sr_r, sr_f = amp.slew_rate(t, vout)
            m["slew_rate"] = float(np.nanmin([sr_r, sr_f]))
            m["settling_time"] = amp.settling_time(t, vout, 20e-9, 0.01, t_end=250e-9)
            meas.curves.append(Curve("step", "Follower step response", t * 1e9,
                                     {"v(out)": vout, "v(inp)": np.real(ds.v("inp"))}, "Time (ns)", "Voltage (V)"))
        return meas
