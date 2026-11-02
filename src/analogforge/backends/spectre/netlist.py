"""Render neutral testbenches as Cadence Spectre netlists (``simulator lang=spectre``).

Syntax follows the Spectre Circuit Simulator Reference.  The generated text
has been checked structurally by unit tests; it has **not** been run through
Spectre in the development environment (see docs/cadence_spectre.md for the
self-test to run on a Cadence machine).
"""

from __future__ import annotations

import re

from ...core import analyses as A
from ...core import circuit as C
from ...core.pdk import PDK
from ...core.testbench import Testbench
from ...units import spice_num as n

#: neutral option -> Spectre ``options`` parameter
OPTION_MAP = {"reltol": "reltol", "abstol": "iabstol", "vntol": "vabstol", "gmin": "gmin",
              "maxstep": "maxstep", "method": None}


def sanitize(name: str) -> str:
    """Spectre instance/analysis names: letters, digits, underscore."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return s if s[:1].isalpha() else "x" + s


def _wave_params(w) -> str:
    if isinstance(w, C.Sine):
        return (f"type=sine sinedc={n(w.offset)} ampl={n(w.amplitude)} freq={n(w.freq)} "
                f"delay={n(w.delay)} sinephase={n(w.phase_deg)}")
    if isinstance(w, C.Pulse):
        return (f"type=pulse val0={n(w.v1)} val1={n(w.v2)} delay={n(w.delay)} rise={n(w.rise)} "
                f"fall={n(w.fall)} width={n(w.width)} period={n(w.period)}")
    if isinstance(w, C.PWL):
        return "type=pwl wave=[" + " ".join(f"{n(t)} {n(v)}" for t, v in w.points) + "]"
    raise TypeError(f"unknown waveform {w!r}")


def _source(e, kind: str) -> str:
    parts = [f"{sanitize(e.name)} ({e.p} {e.n}) {kind}"]
    if e.waveform is not None:
        parts.append(_wave_params(e.waveform))
        parts.append(f"dc={n(e.dc)}")  # value used by DC/AC analyses
    else:
        parts.append(f"type=dc dc={n(e.dc)}")
    if e.ac_mag:
        parts.append(f"mag={n(e.ac_mag)}")
        if e.ac_phase:
            parts.append(f"phase={n(e.ac_phase)}")
    return " ".join(parts)


def render_element(e: C.Element, pdk: PDK) -> str:
    nm = sanitize(e.name)
    if isinstance(e, C.Resistor):
        return f"{nm} ({e.n1} {e.n2}) resistor r={n(e.r)}"
    if isinstance(e, C.Capacitor):
        return f"{nm} ({e.n1} {e.n2}) capacitor c={n(e.c)}"
    if isinstance(e, C.Inductor):
        return f"{nm} ({e.n1} {e.n2}) inductor l={n(e.l)}"
    if isinstance(e, C.MutualInductance):
        return f"{nm} mutual_inductor coupling={n(e.k)} ind1={sanitize(e.l1)} ind2={sanitize(e.l2)}"
    if isinstance(e, C.TLine):
        return f"{nm} ({e.p1} {e.n1} {e.p2} {e.n2}) tline z0={n(e.z0)} td={n(e.td)}"
    if isinstance(e, C.VSource):
        return _source(e, "vsource")
    if isinstance(e, C.ISource):
        return _source(e, "isource")
    if isinstance(e, C.VCVS):
        return f"{nm} ({e.p} {e.n} {e.cp} {e.cn}) vcvs gain={n(e.gain)}"
    if isinstance(e, C.VCCS):
        return f"{nm} ({e.p} {e.n} {e.cp} {e.cn}) vccs gm={n(e.gm)}"
    if isinstance(e, C.Device):
        return pdk.render_device("spectre", e)
    if isinstance(e, C.Raw):
        if "spectre" not in e.text:
            raise ValueError(f"raw element {e.name} has no spectre text")
        return e.text["spectre"]
    raise TypeError(f"cannot render element {e!r} for Spectre")


def analysis_lines(an: A.Analysis, circ: C.Circuit) -> list[str]:
    key = sanitize(an.key())
    if isinstance(an, A.Op):
        return [f"{key} dc oppoint=rawfile maxiters=150 maxsteps=10000"]
    if isinstance(an, A.DCSweep):
        return [f"{key} dc dev={sanitize(an.source)} param=dc start={n(an.start)} stop={n(an.stop)} "
                f"step={n(an.step)}"]
    if isinstance(an, A.AC):
        return [f"{key} ac start={n(an.fstart)} stop={n(an.fstop)} dec={int(an.points_per_decade)}"]
    if isinstance(an, A.Tran):
        s = f"{key} tran stop={n(an.tstop)} step={n(an.tstep)}"
        if an.tstart:
            s += f" outputstart={n(an.tstart)}"
        if an.tmax:
            s += f" maxstep={n(an.tmax)}"
        s += " errpreset=conservative"
        if an.uic:
            s += " skipdc=yes"
        return [s]
    if isinstance(an, A.Noise):
        neg = an.out_n if an.out_n not in ("", None) else "0"
        return [f"{key} ({an.out_p} {neg}) noise start={n(an.fstart)} stop={n(an.fstop)} "
                f"dec={int(an.points_per_decade)} iprobe={sanitize(an.input_source)}"]
    if isinstance(an, A.PSS):
        if an.osc_p:  # autonomous: oscillator node pair in the instance terminals
            return [f"{key} ({an.osc_p} {an.osc_n or '0'}) pss fund={n(an.fund)} harms={int(an.harmonics)} "
                    f"tstab={n(an.tstab)} errpreset=conservative"]
        return [f"{key} pss fund={n(an.fund)} harms={int(an.harmonics)} tstab={n(an.tstab)} "
                f"errpreset=conservative"]
    if isinstance(an, A.PNoise):
        return [f"{key} ({an.osc_p} {an.osc_n}) pnoise start={n(an.fstart)} stop={n(an.fstop)} "
                f"dec={int(an.points_per_decade)} maxsideband={int(an.max_sideband)} "
                f"noisetype=timeaverage noiseout=[pm]"]
    raise A.UnsupportedAnalysis(f"Spectre netlister cannot render {an.kind!r}")


def render_netlist(tb: Testbench, pdk: PDK, saves: list[str] | None = None,
                   include_analyses: bool = True, include_models: bool = True) -> str:
    circ = tb.circuit
    corner = pdk.check_corner(tb.corner)
    out = [f"// AnalogForge testbench: {circ.name} / {tb.name}",
           f"// PDK {pdk.name}, corner {corner}, T = {tb.temperature} C"]
    out += [f"// {c}" for c in circ.comments]
    out += ["simulator lang=spectre", "global 0"]
    if include_models:
        for st in pdk.model_statements("spectre", corner, circ.device_kinds()):
            if st.section:
                out.append(f'include "{st.path}" section={st.section}')
            else:
                out.append(f'include "{st.path}"')
    out.append("")
    for e in circ.elements:
        out.append(render_element(e, pdk))
    if circ.ics:
        out.append("ic " + " ".join(f"{k}={n(v)}" for k, v in circ.ics.items()))
    if circ.nodesets:
        out.append("nodeset " + " ".join(f"{k}={n(v)}" for k, v in circ.nodesets.items()))
    opts = {"temp": tb.temperature, "tnom": 27}
    for k, v in tb.options.items():
        mapped = OPTION_MAP.get(k)
        if mapped:
            opts[mapped] = v
    out.append("")
    out.append("simulatorOptions options " + " ".join(f"{k}={n(v) if not isinstance(v, str) else v}"
                                                    for k, v in opts.items()))
    if saves:
        out.append("save " + " ".join(saves))
    if include_analyses:
        out.append("")
        for an in tb.analyses:
            out.extend(analysis_lines(an, circ))
    return "\n".join(out) + "\n"
