"""Render neutral testbenches as Keysight ADS netlists.

All ADS-specific syntax is concentrated in this module so it can be adjusted
quickly to the installed ADS version.  It follows the netlist format written
by ADS itself (``Component:Instance node1 node2 Param=value``) for the basic
components and controllers.  **Status: not validated against a real ADS
installation** (none available in the development environment) - run
``scripts/ads_selftest.py`` on an ADS machine first (docs/keysight_ads.md).
The recommended production path is to let ADS write the netlist from your
schematic and have AnalogForge only substitute variable values
(:func:`substitute_variables`).
"""

from __future__ import annotations

import re

from ...core import analyses as A
from ...core import circuit as C
from ...core.pdk import PDK
from ...core.testbench import Testbench
from ...units import spice_num as n


def ads_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return s if s[:1].isalpha() else "X" + s


def _node(node: str) -> str:
    return "0" if node in ("0", "gnd", "gnd!") else node


def render_element(e: C.Element, pdk: PDK) -> str:
    nm = ads_name(e.name)
    if isinstance(e, C.Resistor):
        return f"R:{nm} {_node(e.n1)} {_node(e.n2)} R={n(e.r)}"
    if isinstance(e, C.Capacitor):
        return f"C:{nm} {_node(e.n1)} {_node(e.n2)} C={n(e.c)}"
    if isinstance(e, C.Inductor):
        return f"L:{nm} {_node(e.n1)} {_node(e.n2)} L={n(e.l)} R=0"
    if isinstance(e, C.MutualInductance):
        return f'Mutual:{nm} Inductor1="{ads_name(e.l1)}" Inductor2="{ads_name(e.l2)}" K={n(e.k)}'
    if isinstance(e, C.TLine):
        raise A.UnsupportedAnalysis("TLine: use a PDK/ADS transmission-line component via a Raw element")
    if isinstance(e, (C.VSource, C.ISource)):
        return _source(e)
    if isinstance(e, C.VCVS):
        # ADS VCVS pin order: in+, out+, out-, in-
        return f"VCVS:{nm} {_node(e.cp)} {_node(e.p)} {_node(e.n)} {_node(e.cn)} G={n(e.gain)}"
    if isinstance(e, C.VCCS):
        return f"VCCS:{nm} {_node(e.cp)} {_node(e.p)} {_node(e.n)} {_node(e.cn)} G={n(e.gm)}"
    if isinstance(e, C.Device):
        return pdk.render_device("ads", e)
    if isinstance(e, C.Raw):
        if "ads" not in e.text:
            raise ValueError(f"raw element {e.name} has no ads text")
        return e.text["ads"]
    raise TypeError(f"cannot render element {e!r} for ADS")


def _source(e) -> str:
    v = isinstance(e, C.VSource)
    nm = ads_name(e.name)
    p, m = _node(e.p), _node(e.n)
    w = e.waveform
    if isinstance(w, C.Pulse):
        comp, lo, hi = ("VtPulse", "Vlow", "Vhigh") if v else ("ItPulse", "Ilow", "Ihigh")
        return (f"{comp}:{nm} {p} {m} {lo}={n(w.v1)} {hi}={n(w.v2)} Delay={n(w.delay)} Edge=\"linear\" "
                f"Rise={n(w.rise)} Fall={n(w.fall)} Width={n(w.width)} Period={n(w.period)}")
    if isinstance(w, C.Sine):
        comp, dc, amp = ("VtSine", "Vdc", "Amplitude") if v else ("ItSine", "Idc", "Amplitude")
        return f"{comp}:{nm} {p} {m} {dc}={n(w.offset)} {amp}={n(w.amplitude)} Freq={n(w.freq)} Delay={n(w.delay)}"
    if isinstance(w, C.PWL):
        comp = "VtPWL" if v else "ItPWL"
        pts = ", ".join(f"{n(t)}, {n(val)}" for t, val in w.points)
        return f"{comp}:{nm} {p} {m} Wave=list({pts})"
    if v:
        s = f'V_Source:{nm} {p} {m} Type="V_DC" Vdc={n(e.dc)}'
        if e.ac_mag:
            s = f'V_Source:{nm} {p} {m} Type="V_AC" Vdc={n(e.dc)} Vac=polar({n(e.ac_mag)},{n(e.ac_phase)})'
        return s + " SaveCurrent=1"
    s = f'I_Source:{nm} {p} {m} Type="I_DC" Idc={n(e.dc)}'
    if e.ac_mag:
        s = f'I_Source:{nm} {p} {m} Type="I_AC" Idc={n(e.dc)} Iac=polar({n(e.ac_mag)},{n(e.ac_phase)})'
    return s


def controller_lines(an: A.Analysis) -> list[str]:
    nm = ads_name(f"AF_{an.key()}")
    if isinstance(an, A.Op):
        return [f"DC:{nm}"]
    if isinstance(an, A.DCSweep):
        return [f'DC:{nm} SweepVar="{ads_name(an.source)}.Vdc" Start={n(an.start)} Stop={n(an.stop)} '
                f"Step={n(an.step)}"]
    if isinstance(an, A.AC):
        return [f'AC:{nm} SweepVar="freq" Start={n(an.fstart)} Stop={n(an.fstop)} Dec={int(an.points_per_decade)}']
    if isinstance(an, A.Tran):
        s = f"Tran:{nm} StartTime={n(an.tstart)} StopTime={n(an.tstop)} MaxTimeStep={n(an.tmax or an.tstep)}"
        if an.uic:
            s += " UseInitCond=yes"
        return [s]
    if isinstance(an, A.Noise):
        return [f'AC:{nm} SweepVar="freq" Start={n(an.fstart)} Stop={n(an.fstop)} '
                f'Dec={int(an.points_per_decade)} CalcNoise=yes NoiseNode[1]="{an.out_p}"']
    if isinstance(an, A.PSS):
        # oscillator harmonic balance: an OscPort must be inserted in the tank (see testbench_netlist)
        return [f"HB:{nm} Freq[1]={n(an.fund)} Order[1]={int(an.harmonics)}"]
    if isinstance(an, A.PNoise):
        return [f"HB:{nm} NLNoiseMode=yes NLNoiseStart={n(an.fstart)} NLNoiseStop={n(an.fstop)} "
                f"NLNoiseDec={int(an.points_per_decade)} PhaseNoise=1"]
    raise A.UnsupportedAnalysis(f"ADS netlister cannot render {an.kind!r}")


def render_netlist(tb: Testbench, pdk: PDK) -> str:
    circ = tb.circuit
    corner = pdk.check_corner(tb.corner)
    out = [f"; AnalogForge testbench: {circ.name} / {tb.name}",
           f"; PDK {pdk.name}, corner {corner}, T = {tb.temperature} C",
           "; UNVALIDATED ADS syntax - see docs/keysight_ads.md",
           f"Options ResourceUsage=yes UseNutmegFormat=no Temp={n(tb.temperature)} Tnom=27"]
    for st in pdk.model_statements("ads", corner, circ.device_kinds()):
        out.append(f'#include "{st.path}"')
    if tb.variables:  # informational only: element values below are already numbers
        out.append("; design point: " + ", ".join(f"{k}={n(v)}" for k, v in tb.variables.items()))
    out.append("")
    for e in circ.elements:
        out.append(render_element(e, pdk))
    for an in tb.analyses:
        if an.kind == "pss" and getattr(an, "osc_p", None):
            out.append(f"OscPort:AF_OSC {an.osc_p} {an.osc_n or '0'} Z=1.1 Ohm NumOctaves=2 Steps=10")
        out.extend(controller_lines(an))
    return "\n".join(out) + "\n"


_VAR_LINE = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")


def substitute_variables(netlist: str, values: dict[str, float]) -> str:
    """Replace top-level ``NAME=value`` variable assignments of an ADS-generated netlist."""
    wanted = {k.lower(): (k, v) for k, v in values.items()}
    done = set()
    lines = []
    for line in netlist.splitlines():
        m = _VAR_LINE.match(line)
        if m and ":" not in line.split("=")[0] and m.group(2).lower() in wanted:
            name, val = wanted[m.group(2).lower()]
            lines.append(f"{m.group(1)}{m.group(2)}={n(val)}")
            done.add(name.lower())
        else:
            lines.append(line)
    missing = [k for k in wanted if k not in done]
    if missing:
        lines.insert(1, "; variables added by AnalogForge")
        for k in missing:
            name, val = wanted[k]
            lines.insert(2, f"{name}={n(val)}")
    return "\n".join(lines) + "\n"
