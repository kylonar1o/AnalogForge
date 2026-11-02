"""Render neutral testbenches as ngspice decks."""

from __future__ import annotations

from ...core import analyses as A
from ...core import circuit as C
from ...core.pdk import PDK
from ...core.testbench import Testbench
from ...units import spice_num as n

#: neutral option name -> ngspice option name
OPTION_MAP = {"reltol": "reltol", "abstol": "abstol", "vntol": "vntol", "gmin": "gmin",
              "method": "method", "maxord": "maxord", "itl1": "itl1", "itl2": "itl2", "itl4": "itl4",
              "rshunt": "rshunt", "gminsteps": "gminsteps", "srcsteps": "srcsteps", "trtol": "trtol",
              "chgtol": "chgtol"}

#: plot name prefixes ngspice writes into raw files, used to validate outputs
PLOTNAMES = {"op": "operating point", "dc": "dc transfer", "ac": "ac analysis",
             "tran": "transient analysis", "noise": "noise spectral", "pss": "frequency domain periodic",
             "noise_integrated": "integrated noise", "pss_td": "time domain periodic"}


def _waveform(w) -> str:
    if isinstance(w, C.Sine):
        return (f"SIN({n(w.offset)} {n(w.amplitude)} {n(w.freq)} {n(w.delay)} 0 {n(w.phase_deg)})")
    if isinstance(w, C.Pulse):
        return (f"PULSE({n(w.v1)} {n(w.v2)} {n(w.delay)} {n(w.rise)} {n(w.fall)} "
                f"{n(w.width)} {n(w.period)})")
    if isinstance(w, C.PWL):
        return "PWL(" + " ".join(f"{n(t)} {n(v)}" for t, v in w.points) + ")"
    raise TypeError(f"unknown waveform {w!r}")


def _src(prefix: str, e) -> str:
    name = e.name if e.name.upper().startswith(prefix) else prefix + e.name
    parts = [f"{name} {e.p} {e.n} DC {n(e.dc)}"]
    if e.ac_mag:
        parts.append(f"AC {n(e.ac_mag)} {n(e.ac_phase)}")
    if e.waveform is not None:
        parts.append(_waveform(e.waveform))
    return " ".join(parts)


def element_name(prefix: str, name: str) -> str:
    return name if name.upper().startswith(prefix) else prefix + name


def render_element(e: C.Element, pdk: PDK) -> str:
    if isinstance(e, C.Resistor):
        return f"{element_name('R', e.name)} {e.n1} {e.n2} {n(e.r)}"
    if isinstance(e, C.Capacitor):
        ic = f" IC={n(e.ic)}" if e.ic is not None else ""
        return f"{element_name('C', e.name)} {e.n1} {e.n2} {n(e.c)}{ic}"
    if isinstance(e, C.Inductor):
        ic = f" IC={n(e.ic)}" if e.ic is not None else ""
        return f"{element_name('L', e.name)} {e.n1} {e.n2} {n(e.l)}{ic}"
    if isinstance(e, C.MutualInductance):
        return f"{element_name('K', e.name)} {element_name('L', e.l1)} {element_name('L', e.l2)} {n(e.k)}"
    if isinstance(e, C.TLine):
        return f"{element_name('T', e.name)} {e.p1} {e.n1} {e.p2} {e.n2} Z0={n(e.z0)} TD={n(e.td)}"
    if isinstance(e, C.VSource):
        return _src("V", e)
    if isinstance(e, C.ISource):
        return _src("I", e)
    if isinstance(e, C.VCVS):
        return f"{element_name('E', e.name)} {e.p} {e.n} {e.cp} {e.cn} {n(e.gain)}"
    if isinstance(e, C.VCCS):
        return f"{element_name('G', e.name)} {e.p} {e.n} {e.cp} {e.cn} {n(e.gm)}"
    if isinstance(e, C.Device):
        return pdk.render_device("ngspice", e)
    if isinstance(e, C.Raw):
        if "ngspice" not in e.text:
            raise ValueError(f"raw element {e.name} has no ngspice text")
        return e.text["ngspice"]
    raise TypeError(f"cannot render element {e!r} for ngspice")


def source_name(circuit: C.Circuit, name: str) -> str:
    """ngspice instance name of an independent source given its IR name."""
    try:
        e = circuit[name]
    except KeyError:
        return name
    if isinstance(e, C.VSource):
        return element_name("V", e.name)
    if isinstance(e, C.ISource):
        return element_name("I", e.name)
    return name


def analysis_commands(an: A.Analysis, circuit: C.Circuit) -> list[str]:
    key = an.key()
    if isinstance(an, A.Op):
        return ["op"]
    if isinstance(an, A.DCSweep):
        return [f"dc {source_name(circuit, an.source)} {n(an.start)} {n(an.stop)} {n(an.step)}"]
    if isinstance(an, A.AC):
        return [f"ac dec {int(an.points_per_decade)} {n(an.fstart)} {n(an.fstop)}"]
    if isinstance(an, A.Tran):
        cmd = f"tran {n(an.tstep)} {n(an.tstop)}"
        if an.tstart or an.tmax:
            cmd += f" {n(an.tstart)}"
        if an.tmax:
            cmd += f" {n(an.tmax)}"
        if an.uic:
            cmd += " uic"
        return [cmd]
    if isinstance(an, A.Noise):
        out = f"v({an.out_p})" if an.out_n in ("0", "", None) else f"v({an.out_p},{an.out_n})"
        return [f"noise {out} {source_name(circuit, an.input_source)} dec {int(an.points_per_decade)} "
                f"{n(an.fstart)} {n(an.fstop)}"]
    if isinstance(an, A.PSS):
        if not an.osc_p:
            raise A.UnsupportedAnalysis("ngspice PSS is used for autonomous oscillators: set osc_p")
        # .pss gfreq tstab oscnob psspoints harms sciter steadycoeff [uic]
        return [f"pss {n(an.fund)} {n(an.tstab)} {an.osc_p} {int(an.points)} {int(an.harmonics)} 50 5e-3 uic"]
    raise A.UnsupportedAnalysis(f"ngspice backend cannot run {an.kind!r} analyses ({key})")


def render_deck(tb: Testbench, pdk: PDK, op_vectors: list[str], extra_options: dict | None = None,
                analysis_saves: dict[str, list[str]] | None = None) -> str:
    """Complete ngspice input deck including the ``.control`` section."""
    circ = tb.circuit
    lines = [f"* AnalogForge testbench: {circ.name} / {tb.name}"]
    for c in circ.comments:
        lines.append(f"* {c}")
    corner = pdk.check_corner(tb.corner)
    lines.append(f"* PDK {pdk.name}, corner {corner}, T = {tb.temperature} C")
    for st in pdk.model_statements("ngspice", corner, circ.device_kinds()):
        if st.kind == "lib":
            lines.append(f'.lib "{st.path}" {st.section}')
        else:
            lines.append(f'.include "{st.path}"')
    lines.append(f".temp {n(tb.temperature)}")
    opts = {OPTION_MAP.get(k, k): v for k, v in tb.options.items() if k in OPTION_MAP}
    opts.update(extra_options or {})
    if opts:
        lines.append(".options " + " ".join(k if v is True else f"{k}={v if isinstance(v, str) else n(v)}"
                                            for k, v in opts.items()))
    lines.append("")
    for e in circ.elements:
        lines.append(render_element(e, pdk))
    if circ.ics:
        lines.append(".ic " + " ".join(f"v({k})={n(v)}" for k, v in circ.ics.items()))
    if circ.nodesets:
        lines.append(".nodeset " + " ".join(f"v({k})={n(v)}" for k, v in circ.nodesets.items()))
    lines.append("")
    lines.append(".control")
    lines.append("set filetype=binary")
    lines.append("set noaskquit")
    for an in tb.analyses:
        key = an.key()
        saves = (analysis_saves or {}).get(key)
        if saves:
            lines.append("save all " + " ".join(saves))
        lines.extend(analysis_commands(an, circ))
        if an.kind == "op" and op_vectors:
            # one write per vector chunk would be messy; ngspice accepts long lines
            lines.append(f"write {key}.raw all " + " ".join(op_vectors))
        elif an.kind == "noise":
            lines.append(f"write {key}_integrated.raw all")
            lines.append("setplot previous")
            lines.append(f"write {key}.raw all")
        elif an.kind == "pss":
            lines.append(f"write {key}.raw all")
            lines.append("setplot previous")
            lines.append(f"write {key}_td.raw all")
        else:
            lines.append(f"write {key}.raw all")
    lines.append(".endc")
    lines.append(".end")
    return "\n".join(lines) + "\n"
