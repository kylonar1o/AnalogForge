"""Cross-coupled NMOS LC VCO with MOS varactors and an NMOS tail current mirror.

Topology (per side, outp shown; outn is the mirror image)::

          VDD ──┬──────────┬──────────┐
                L_TANK     R_P        (tank loss, R_P = Q * 2*pi*F_Q * L_TANK)
                │          │
    outp ───────┴──────────┴──┬─────── C_FIX ── gnd
                              ├─────── varactor (gate = outp, n-well = VTUNE)
                              └─── drain M1 (gate = outn), source = tail
    tail ── MT (NMOS mirror, ratio N_MIRROR : 1 from MB fed by IBIAS/N_MIRROR) ── gnd

Measured: oscillation frequency (zero crossings of v(outp)-v(outn), FFT and
optionally ngspice PSS as cross-checks), differential amplitude, DC power,
start-up time, small-signal start-up loop gain gm*R_P, tuning curve over
VTUNE (tuning range, K_VCO), phase noise (Leeson estimate or the
simulation-based ISF method), FoM and FoM_T.
"""

from __future__ import annotations

import math

import numpy as np

from ..core.analyses import PSS, Op, Tran
from ..core.circuit import Circuit, Pulse
from ..core.design import Curve, Design, DesignContext, Measurement, MetricSpec, ParamSpec
from ..core.testbench import Testbench
from ..flows.isf import InjectionSite, measure_isf
from ..metrics import oscillator as osc
from ..metrics import time_average
from ..metrics.phasenoise import NoiseSourceISF, channel_noise_psd, phase_noise_from_isf, thermal_current_psd
from ..metrics.waveform import crossings, spectrum

NAN = float("nan")


class LCVCO(Design):
    name = "lc_vco"
    title = "Cross-coupled NMOS LC VCO"
    kind = "oscillator"
    description = __doc__
    parameters = [
        ParamSpec("VDD", 1.8, 1.6, 2.0, "V", description="supply voltage", tunable=False),
        ParamSpec("IBIAS", 2e-3, 0.5e-3, 5e-3, "A", log=True, description="tail current"),
        ParamSpec("WN", 20e-6, 5e-6, 60e-6, "m", log=True, description="cross-coupled NMOS width"),
        ParamSpec("LN", 0.15e-6, 0.15e-6, 0.5e-6, "m", description="cross-coupled NMOS length"),
        ParamSpec("LTANK", 2e-9, 0.5e-9, 5e-9, "H", log=True, description="tank inductance per side"),
        ParamSpec("QL", 10.0, 3.0, 30.0, "", description="inductor quality factor at F_Q", tunable=False),
        ParamSpec("FQ", 3e9, 0.5e9, 20e9, "Hz", log=True, description="frequency at which QL is specified",
                  tunable=False),
        ParamSpec("CFIX", 0.6e-12, 0.0, 3e-12, "F", description="fixed (MIM) capacitance per side"),
        ParamSpec("WVAR", 2e-6, 1e-6, 10e-6, "m", description="varactor finger width (narrow = higher Q)"),
        ParamSpec("LVAR", 0.18e-6, 0.18e-6, 1e-6, "m", description="varactor length (short = higher Q)"),
        ParamSpec("MVAR", 120, 10, 400, "", description="varactor fingers per side", integer=True),
        ParamSpec("VTUNE", 0.9, 0.0, 1.8, "V", description="nominal tuning voltage"),
        ParamSpec("WT", 40e-6, 5e-6, 200e-6, "m", log=True, description="tail transistor width"),
        ParamSpec("LT", 0.5e-6, 0.15e-6, 2e-6, "m", description="tail transistor length"),
        ParamSpec("NMIRROR", 10, 1, 20, "", description="tail mirror ratio", integer=True, tunable=False),
    ]
    metrics = [
        MetricSpec("f_osc", "Hz", "oscillation frequency at VTUNE (zero crossings)"),
        MetricSpec("f_fft", "Hz", "oscillation frequency from the FFT peak (cross-check)"),
        MetricSpec("f_pss", "Hz", "oscillation frequency from ngspice PSS (option pss=true)"),
        MetricSpec("amplitude_diff", "V", "differential output amplitude (peak)", "max"),
        MetricSpec("power", "W", "average DC power from VDD", "min"),
        MetricSpec("startup_time", "s", "time to reach 90 % of the final amplitude", "min"),
        MetricSpec("loop_gain_startup", "", "gm(M1)*R_P: start-up loop gain counting inductor loss only "
                   "(varactor and device losses lower the real margin)"),
        MetricSpec("f_min", "Hz", "lowest frequency over the VTUNE sweep"),
        MetricSpec("f_max", "Hz", "highest frequency over the VTUNE sweep"),
        MetricSpec("tuning_range_pct", "%", "tuning range 2(fmax-fmin)/(fmax+fmin)", "max"),
        MetricSpec("kvco_max", "Hz/V", "maximum |df/dVTUNE|"),
        MetricSpec("kvco_nom", "Hz/V", "df/dVTUNE at the nominal VTUNE"),
        MetricSpec("pn_100k", "dBc/Hz", "phase noise at 100 kHz offset", "min"),
        MetricSpec("pn_1M", "dBc/Hz", "phase noise at 1 MHz offset", "min"),
        MetricSpec("pn_10M", "dBc/Hz", "phase noise at 10 MHz offset", "min"),
        MetricSpec("fom", "dB", "FoM = 20log(f0/df) - L(df) - 10log(P/1mW) at 1 MHz", "max"),
        MetricSpec("fom_t", "dB", "FoM_T = FoM + 20log(TR%/10)", "max"),
        MetricSpec("q_tank", "", "inductor Q at the oscillation frequency"),
    ]
    options = {
        "tstop": 40e-9,          # main transient length
        "tuning_points": 5,      # VTUNE sweep points over [0, VDD]; 0 disables
        "phase_noise": "leeson",  # "leeson", "isf", "pnoise" (simulator-native, Spectre/ADS) or "none"
        "isf_points": 12,        # ISF phase samples per injection site
        "isf_dq_fraction": 0.02,  # injected charge relative to q_max
        "gamma": 1.0,            # channel-noise coefficient for 4kT*gamma*gm
        "noise_factor": None,    # Leeson F (default 1 + gamma)
        "pss": False,            # also run ngspice PSS as a frequency cross-check (slow, ~40 s)
    }
    default_objective = "max:fom"
    default_constraints = ["f_osc >= 2.0e9", "amplitude_diff >= 0.4", "power <= 6e-3"]

    # --------------------------------------------------------------- helpers
    @staticmethod
    def rp(p) -> float:
        return p["QL"] * 2 * math.pi * p["FQ"] * p["LTANK"]

    @staticmethod
    def f_estimate(p) -> float:
        c_var = p["MVAR"] * (0.8e-15 + 6e-3 * p["WVAR"] * p["LVAR"])  # mid-range, fringe + area
        c_dev = p["WN"] * 2.5e-9                              # ~2.5 fF/um of device width
        c = p["CFIX"] + c_var + c_dev
        return 1.0 / (2 * math.pi * math.sqrt(p["LTANK"] * c))

    def circuit(self, p, ctx: DesignContext, vtune: float | None = None) -> Circuit:
        vdd = p["VDD"] * ctx.vdd_scale
        c = Circuit(self.name)
        c.comments.append(f"{self.title}; R_P = {self.rp(p):.4g} Ohm per side (Q={p['QL']} @ {p['FQ']:.4g} Hz)")
        c.vsource("VDD", "vdd", dc=vdd)
        c.vsource("VTUNE", "vtune", dc=p["VTUNE"] if vtune is None else vtune)
        rp = self.rp(p)
        for side, other in (("p", "n"), ("n", "p")):
            node = f"out{side}"
            c.inductor(f"L{side.upper()}", "vdd", node, p["LTANK"])
            c.resistor(f"RP{side.upper()}", "vdd", node, rp)
            if p["CFIX"] > 0:
                c.capacitor(f"CF{side.upper()}", node, "0", p["CFIX"])
            c.varactor(f"V{side.upper()}", node, "vtune", p["WVAR"], p["LVAR"], int(p["MVAR"]))
        c.nmos("M1", "outp", "outn", "tail", "0", p["WN"], p["LN"])
        c.nmos("M2", "outn", "outp", "tail", "0", p["WN"], p["LN"])
        n = int(p["NMIRROR"])
        c.isource("IREF", "vdd", "nbias", dc=p["IBIAS"] / n)
        c.nmos("MB", "nbias", "nbias", "0", "0", p["WT"] / n, p["LT"])
        c.nmos("MT", "tail", "nbias", "0", "0", p["WT"], p["LT"])
        # start-up kick: 100 uA x 100 ps into outp
        c.isource("IKICK", "0", "outp", waveform=Pulse(0.0, 100e-6, 0.1e-9, 10e-12, 10e-12, 100e-12, 1.0))
        return c

    def _tmax(self, p) -> float:
        return 1.0 / (self.f_estimate(p) * 80.0)

    # ------------------------------------------------------------ testbenches
    def testbenches(self, p, ctx):
        tmax = self._tmax(p)
        tstop = float(ctx.option("tstop", 40e-9))
        tbs = [Testbench("tran", self.circuit(p, ctx),
                         [Op("op"), Tran("tran", tstop=tstop, tstep=tmax, tmax=tmax, save=["M1:gm", "M1:id"])],
                         save_op=["M1", "MT"], description="main transient at nominal VTUNE",
                         variables=dict(p), probes=["v(outp)", "v(outn)", "i(VDD)"])]
        npts = int(ctx.option("tuning_points", 5) or 0)
        if npts >= 2:
            vdd = p["VDD"] * ctx.vdd_scale
            for i, vt in enumerate(np.linspace(0.0, vdd, npts)):
                tbs.append(Testbench(f"tune_{i}", self.circuit(p, ctx, float(vt)),
                                     [Tran("tran", tstop=min(tstop, 30e-9), tstep=tmax, tmax=tmax)],
                                     description=f"tuning point VTUNE={vt:.3f} V",
                                     variables={**p, "VTUNE": float(vt)}, probes=["v(outp)", "v(outn)"]))
        return tbs

    # ---------------------------------------------------------------- measure
    def measure(self, p, results, ctx):
        meas = Measurement()
        m = meas.metrics
        for spec in self.metrics:
            m[spec.name] = NAN
        vdd = p["VDD"] * ctx.vdd_scale
        rp = self.rp(p)
        main = results.get("tran")
        tr = main.get("tran") if main else None
        if tr is None:
            meas.warnings.append("main transient failed - no oscillator metrics")
            return meas
        t = tr.sweep
        vd = np.real(tr.v("outp") - tr.v("outn"))
        f0 = osc.osc_frequency(t, vd, 0.3)
        m["f_osc"] = f0
        m["f_fft"] = osc.fft_frequency(t, vd, 0.5)
        m["amplitude_diff"] = osc.amplitude(t, vd, 0.2)
        m["startup_time"] = osc.startup_time(t, vd)
        m["power"] = abs(vdd * time_average(t, tr.i("VDD"), 0.3))
        oscillating = osc.is_oscillating(t, vd, 0.01)
        meas.extra["oscillating"] = oscillating
        op = main.get("op")
        if op is not None and op.has("m1:gm"):
            m["loop_gain_startup"] = op.op("m1", "gm") * rp
            meas.extra["op"] = {k: float(np.real(v.reshape(-1)[0])) for k, v in op.signals.items()
                                if ":" in k}
        if np.isfinite(f0):
            m["q_tank"] = rp / (2 * math.pi * f0 * p["LTANK"])
        # waveforms for plotting: last ~4 periods and the start-up envelope
        T = 1.0 / f0 if np.isfinite(f0) else (t[-1] - t[0]) / 20
        last = t >= t[-1] - 4 * T
        meas.curves.append(Curve("waveform", "Steady-state output", t[last] * 1e9,
                                 {"v(outp)": tr.v("outp")[last], "v(outn)": tr.v("outn")[last],
                                  "v(outp)-v(outn)": vd[last]}, "Time (ns)", "Voltage (V)"))
        meas.curves.append(Curve("startup", "Start-up transient", t * 1e9, {"v(outp)-v(outn)": vd},
                                 "Time (ns)", "Voltage (V)"))
        fs, amp = spectrum(t[t >= t[-1] * 0.5], vd[t >= t[-1] * 0.5])
        sel = fs <= 6 * (f0 if np.isfinite(f0) else fs[-1])
        meas.curves.append(Curve("spectrum", "Output spectrum (Hann)", fs[sel] / 1e9,
                                 {"|V| (dBV)": 20 * np.log10(np.maximum(amp[sel], 1e-12))},
                                 "Frequency (GHz)", "Amplitude (dBV)"))
        if not oscillating:
            meas.warnings.append("circuit does not oscillate (check loop_gain_startup > 1)")
            return meas
        # ------------------------------------------------------------- tuning
        tune = sorted((k for k in results if k.startswith("tune_")), key=lambda s: int(s.split("_")[1]))
        if tune:
            npts = len(tune)
            vts = np.linspace(0.0, vdd, npts)
            freqs = []
            for k in tune:
                ds = results[k].get("tran")
                if ds is None:
                    freqs.append(NAN)
                    continue
                v = np.real(ds.v("outp") - ds.v("outn"))
                freqs.append(osc.osc_frequency(ds.sweep, v, 0.3) if osc.is_oscillating(ds.sweep, v, 0.01) else NAN)
            freqs = np.array(freqs)
            tm = osc.tuning_metrics(vts, freqs)
            for key in ("f_min", "f_max", "tuning_range_pct", "kvco_max"):
                m[key] = tm[key]
            ok = np.isfinite(freqs)
            if ok.sum() >= 2:
                k_curve = np.gradient(freqs[ok], vts[ok])
                m["kvco_nom"] = float(np.interp(p["VTUNE"], vts[ok], k_curve))
            meas.extra["tuning"] = {"vtune": vts.tolist(), "f": [None if not np.isfinite(x) else float(x)
                                                                 for x in freqs]}
            meas.curves.append(Curve("tuning", "Tuning curve", vts, {"f (GHz)": freqs / 1e9},
                                     "VTUNE (V)", "Frequency (GHz)"))
        # -------------------------------------------------------------- PSS
        if ctx.option("pss") and ctx.backend.supports("pss"):
            tbp = Testbench("pss", self.circuit(p, ctx),
                            [PSS("pss", fund=f0, harmonics=10, tstab=min(float(ctx.option("tstop", 40e-9)) / 2,
                                                                          20e-9), osc_p="outp", points=128)])
            rpss = ctx.simulate([tbp])["pss"]
            ds = rpss.get("pss")
            if ds is not None and len(ds.sweep) > 1:
                m["f_pss"] = float(ds.sweep[1])
                meas.extra["pss_vs_tran_pct"] = 100 * (m["f_pss"] - f0) / f0
            else:
                meas.warnings.append(f"PSS failed: {rpss.error}")
        # ------------------------------------------------------- phase noise
        offsets = np.array([1e5, 1e6, 1e7])
        method = str(ctx.option("phase_noise", "leeson")).lower()
        gamma = float(ctx.option("gamma", 1.0))
        pn = None
        if method == "isf":
            pn = self._isf_phase_noise(p, ctx, tr, f0, offsets, gamma, meas)
        elif method == "pnoise":
            pn = self._native_phase_noise(p, ctx, f0, offsets, meas)
        if pn is None and method in ("leeson", "isf", "pnoise"):
            F = ctx.option("noise_factor") or (1.0 + gamma)
            p_sig = m["amplitude_diff"] ** 2 / (4 * rp)
            pn = osc.leeson(offsets, f0, m["q_tank"], p_sig, float(F), ctx.temperature + 273.15)
            meas.extra["phase_noise_method"] = f"leeson(F={F:.3g}, Q={m['q_tank']:.3g})"
            meas.extra["phase_noise"] = {"offsets": offsets.tolist(), "l_dbc_hz": pn.tolist()}
        if pn is not None:
            m["pn_100k"], m["pn_1M"], m["pn_10M"] = (float(x) for x in pn)
            m["fom"] = osc.fom(m["pn_1M"], f0, 1e6, m["power"])
            m["fom_t"] = osc.fom_t(m["fom"], m["tuning_range_pct"])
            # both models are 1/f^2 laws in the region they describe: extend from the 1 MHz value
            dense = np.logspace(4, 8, 41)
            if method == "isf" and "isf" in meas.extra:
                curve = m["pn_1M"] - 20 * np.log10(dense / 1e6)
            else:
                curve = osc.leeson(dense, f0, m["q_tank"], m["amplitude_diff"] ** 2 / (4 * rp),
                                   float(ctx.option("noise_factor") or (1.0 + gamma)), ctx.temperature + 273.15)
            meas.curves.append(Curve("phase_noise", f"Phase noise ({meas.extra.get('phase_noise_method')})",
                                     dense, {"L(f) dBc/Hz": curve}, "Offset frequency (Hz)", "dBc/Hz", xlog=True))
        self._drop_unrequested(m, ctx)
        return meas

    @staticmethod
    def _drop_unrequested(m: dict, ctx) -> None:
        """Remove metrics whose measurement was not requested (avoids misleading NaN warnings)."""
        if not ctx.option("pss"):
            m.pop("f_pss", None)
        if int(ctx.option("tuning_points", 5) or 0) < 2:
            for k in ("f_min", "f_max", "tuning_range_pct", "kvco_max", "kvco_nom", "fom_t"):
                m.pop(k, None)
        if str(ctx.option("phase_noise", "leeson")).lower() == "none":
            for k in ("pn_100k", "pn_1M", "pn_10M", "fom", "fom_t"):
                m.pop(k, None)

    def _native_phase_noise(self, p, ctx, f0, offsets, meas):
        """PSS + PNoise in the simulator (Spectre ``pnoise`` / ADS harmonic-balance phase noise).

        Experimental: result naming differs between simulators/versions; values are taken from
        OCEAN measurements (``pn_<offset>``) or from a pnoise dataset signal in dBc/Hz."""
        if not ctx.backend.supports("pnoise"):
            meas.warnings.append(f"backend {ctx.backend.name} has no native phase-noise analysis; "
                                 f"falling back to Leeson (use phase_noise=isf on ngspice)")
            return None
        from ..core.analyses import PNoise
        tb = Testbench("pnoise", self.circuit(p, ctx),
                       [PSS("pss", fund=f0, harmonics=10, tstab=20e-9, osc_p="outp", osc_n="outn"),
                        PNoise("pnoise", 1e3, 1e8, 10, "outp", "outn", offsets=list(offsets))],
                       variables=dict(p), probes=["v(outp)", "v(outn)"])
        r = ctx.simulate([tb])["pnoise"]
        vals = []
        ocean = r.get("ocean_measurements")
        ds = r.get("pnoise")
        for off in offsets:
            v = float("nan")
            if ocean is not None:
                for key in (f"pn_{off:.10g}", f"pn_{int(off)}"):
                    if ocean.has(key):
                        v = ocean.scalar(key)
            elif ds is not None and ds.signals:
                sig = next(iter(ds.signals.values()))
                y = np.real(np.asarray(sig))
                y = y if np.nanmax(y) < 0 else 20 * np.log10(np.maximum(np.abs(y), 1e-30))
                v = float(np.interp(np.log10(off), np.log10(ds.sweep), y))
            vals.append(v)
        if not np.all(np.isfinite(vals)):
            meas.warnings.append(f"native phase noise not available ({r.error or 'unrecognised result naming'})")
            return None
        meas.extra["phase_noise_method"] = f"{ctx.backend.name} pnoise"
        meas.extra["phase_noise"] = {"offsets": list(offsets), "l_dbc_hz": vals}
        return np.array(vals)

    def _isf_phase_noise(self, p, ctx, tr, f0, offsets, gamma, meas):
        t = tr.sweep
        vd = np.real(tr.v("outp") - tr.v("outn"))
        T0 = 1.0 / f0
        tc = crossings(t, vd, 0.0, "rise")
        tc = tc[tc >= t[-1] * 0.6]
        if len(tc) < 2:
            meas.warnings.append("ISF: no steady-state reference crossing")
            return None
        t_ref = float(tc[0])
        c_node = 1.0 / ((2 * math.pi * f0) ** 2 * p["LTANK"])
        q_max = c_node * meas.metrics["amplitude_diff"]
        dq = float(ctx.option("isf_dq_fraction", 0.02)) * q_max
        sites = [InjectionSite("tank", "0", "outp"), InjectionSite("m1", "tail", "outp")]
        npts = int(ctx.option("isf_points", 12))
        data = measure_isf(ctx, lambda: self.circuit(p, ctx), "v(outp)-v(outn)", sites, f0, t_ref, dq,
                           self._tmax(p), points=npts)
        rp = self.rp(p)
        sources = []
        for d in data:
            if not np.all(np.isfinite(d.sensitivity)):
                meas.warnings.append(f"ISF site {d.site.name}: some runs failed")
                return None
            if d.site.name == "tank":
                psd = thermal_current_psd(rp, ctx.temperature)
            else:
                if not tr.has("m1:gm"):
                    meas.warnings.append("ISF: gm(t) of M1 not available")
                    return None
                gm_t = np.interp(t_ref + d.phases / (2 * math.pi) * T0, t, np.real(tr.get("m1:gm")))
                psd = channel_noise_psd(gm_t, gamma, ctx.temperature)
            sources.append(NoiseSourceISF(d.site.name, d.phases, d.sensitivity, psd, multiplicity=2))
        res = phase_noise_from_isf(sources, offsets)
        meas.extra["phase_noise_method"] = f"isf(K={npts}, gamma={gamma})"
        meas.extra["phase_noise"] = res.to_dict()
        meas.extra["isf"] = {d.site.name: {"phase_rad": d.phases.tolist(),
                                           "gamma": (d.sensitivity * q_max).tolist()} for d in data}
        meas.extra["isf_gamma_rms"] = {s.name: s.gamma_rms(q_max) for s in sources}
        meas.curves.append(Curve("isf", "Impulse sensitivity function (normalized to q_max)",
                                 data[0].phases, {f"Gamma_{d.site.name}": d.sensitivity * q_max for d in data},
                                 "Phase (rad)", "Gamma"))
        return res.l_dbc_hz
