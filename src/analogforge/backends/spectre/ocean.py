"""OCEAN (SKILL) script generation for Cadence Virtuoso/ADE, and parsers for its outputs.

Two modes:

* ``netlist``  - simulate a circuit-only Spectre netlist written by AnalogForge
                 (analyses, model files and temperature are set by OCEAN commands);
* ``cellview`` - simulate an existing Virtuoso schematic (``lib``/``cell``/``view``)
                 whose design variables are set with ``desVar`` - lets AnalogForge's
                 sweeps, PVT and optimizers drive an ADE testbench you already have.

The script writes

* ``analogforge_measurements.txt`` - ``name value`` lines (OCEAN calculator results),
* ``wave_<analysis>.txt``          - ``ocnPrint`` tables of the requested probes.

OCEAN function names follow the OCEAN Reference (``VT``, ``VF``, ``IT``,
``frequency``, ``clip``, ``ymax``, ``average``, ``bandwidth``, ``phaseMargin``,
``gainMargin``, ``phaseNoise``).  Generated scripts are unit-tested for
structure only - validate once on your IC version (docs/cadence_spectre.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ...core import analyses as A
from ...core.testbench import Testbench
from ...units import spice_num as n


def _q(x) -> str:
    return f'"{n(x)}"' if not isinstance(x, str) else f'"{x}"'


@dataclass
class Measure:
    """A scalar measured inside OCEAN: ``name`` and a SKILL expression."""

    name: str
    expr: str
    analysis: str = ""


@dataclass
class OceanSpec:
    mode: str = "netlist"                       # "netlist" | "cellview"
    corner: str = ""
    netlist_path: str = ""                      # netlist mode
    lib: str = ""                               # cellview mode
    cell: str = ""
    view: str = "schematic"
    results_dir: str = "./af_ocean_results"
    model_files: list[tuple[str, str | None]] = field(default_factory=list)
    design_vars: dict[str, float] = field(default_factory=dict)
    temperature: float = 27.0
    options: dict = field(default_factory=dict)
    probes: dict[str, list[str]] = field(default_factory=dict)   # analysis kind -> signals
    measures: list[Measure] = field(default_factory=list)


def _probe_expr(sig: str, kind: str) -> str:
    """Neutral signal name -> OCEAN waveform access for the given analysis kind."""
    s = sig.strip()
    m = re.fullmatch(r"([vi])\((.+)\)", s, re.IGNORECASE)
    if not m:
        return f'getData("{s}")'
    typ, node = m.group(1).lower(), m.group(2)
    if typ == "v":
        fn = {"tran": "VT", "ac": "VF", "dc": "VS", "op": "VDC", "pss": "v"}.get(kind, "VT")
        if fn == "v":
            return f'v("/{node}" ?result "pss_td")'
        return f'{fn}("/{node}")'
    fn = {"tran": "IT", "ac": "IF", "dc": "IS", "op": "IDC"}.get(kind, "IT")
    return f'{fn}("/{node}/PLUS")'


def analysis_command(an: A.Analysis) -> str:
    if isinstance(an, A.Op):
        return "analysis( 'dc ?saveOppoint t )"
    if isinstance(an, A.DCSweep):
        return (f"analysis( 'dc ?dev \"/{an.source}\" ?param \"dc\" ?start {_q(an.start)} "
                f"?stop {_q(an.stop)} ?step {_q(an.step)} )")
    if isinstance(an, A.AC):
        return f"analysis( 'ac ?start {_q(an.fstart)} ?stop {_q(an.fstop)} ?dec {_q(an.points_per_decade)} )"
    if isinstance(an, A.Tran):
        s = f"analysis( 'tran ?stop {_q(an.tstop)} ?errpreset \"conservative\""
        if an.tmax:
            s += f" ?maxstep {_q(an.tmax)}"
        if an.uic:
            s += ' ?skipdc "yes"'
        return s + " )"
    if isinstance(an, A.Noise):
        return (f"analysis( 'noise ?start {_q(an.fstart)} ?stop {_q(an.fstop)} ?dec {_q(an.points_per_decade)} "
                f"?p \"/{an.out_p}\" ?n \"/{an.out_n or 'gnd!'}\" ?iprobe \"/{an.input_source}\" )")
    if isinstance(an, A.PSS):
        s = (f"analysis( 'pss ?fund {_q(an.fund)} ?harms {_q(an.harmonics)} ?tstab {_q(an.tstab)} "
             f"?errpreset \"conservative\"")
        if an.osc_p:
            s += f" ?p \"/{an.osc_p}\" ?n \"/{an.osc_n or 'gnd!'}\" ?oscana t"
        return s + " )"
    if isinstance(an, A.PNoise):
        return (f"analysis( 'pnoise ?start {_q(an.fstart)} ?stop {_q(an.fstop)} ?dec {_q(an.points_per_decade)} "
                f"?maxsideband {_q(an.max_sideband)} ?p \"/{an.osc_p}\" ?n \"/{an.osc_n}\" "
                f"?noisetype \"timeaverage\" ?noiseout list(\"pm\") )")
    raise A.UnsupportedAnalysis(f"OCEAN generator cannot express {an.kind!r}")


def default_measures(tb: Testbench) -> list[Measure]:
    """Generic measurements derived from the analyses of a testbench."""
    out = []
    kinds = tb.analysis_kinds()
    if "pnoise" in kinds:
        an = next(a for a in tb.analyses if a.kind == "pnoise")
        for off in an.offsets:
            out.append(Measure(f"pn_{n(off)}", f'value( phaseNoise( 1 "pss_fd" ?result "pnoise" ) {n(off)} )',
                               "pnoise"))
    return out


def generate_ocean(tb: Testbench, spec: OceanSpec) -> str:
    rd = spec.results_dir
    lines = [
        f"; AnalogForge OCEAN script - testbench {tb.circuit.name}/{tb.name}",
        f"; mode: {spec.mode}   corner/T: {spec.corner or tb.corner} / {spec.temperature} C",
        "; run with:  ocean -nograph -restore <this file>",
        "simulator( 'spectre )",
    ]
    if spec.mode == "cellview":
        lines.append(f'design( "{spec.lib}" "{spec.cell}" "{spec.view}" )')
    else:
        lines.append(f'design( "{spec.netlist_path}" )')
    lines.append(f'resultsDir( "{rd}" )')
    if spec.model_files:
        items = " ".join(f'("{p}" "{s}")' if s else f'("{p}")' for p, s in spec.model_files)
        lines.append(f"modelFile( {items} )")
    if spec.mode == "cellview":  # netlist mode: values are already numbers in the netlist
        for k, v in spec.design_vars.items():
            lines.append(f'desVar( "{k}" {n(v)} )')
    lines.append(f"temp( {n(spec.temperature)} )")
    for k, v in spec.options.items():
        lines.append(f"option( '{k} {_q(v)} )")
    for an in tb.analyses:
        lines.append(analysis_command(an))
    lines += ["run()", "", f'af_out = outfile( "{rd}/analogforge_measurements.txt" "w" )']
    measures = list(spec.measures) or default_measures(tb)
    for m in measures:
        if m.analysis:
            lines.append(f"selectResult( '{_result_name(m.analysis)} )")
        lines.append(f'fprintf( af_out "{m.name} %.12e\\n" {m.expr} )')
    lines.append("close( af_out )")
    for kind, sigs in spec.probes.items():
        if not sigs:
            continue
        lines.append(f"selectResult( '{_result_name(kind)} )")
        exprs = " ".join(_probe_expr(s, kind) for s in sigs)
        lines.append(f'ocnPrint( ?output "{rd}/wave_{kind}.txt" ?numberNotation \'scientific '
                     f"?precision 12 {exprs} )")
    lines.append("exit()")
    return "\n".join(lines) + "\n"


def _result_name(kind: str) -> str:
    return {"op": "dcOp", "dc": "dc", "ac": "ac", "tran": "tran", "noise": "noise",
            "pss": "pss_fd", "pnoise": "pnoise"}.get(kind, kind)


# -------------------------------------------------------------------- parsers
def parse_measurements(path: str | Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                out[parts[0]] = float("nan")
    return out


_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def parse_ocnprint(path: str | Path) -> tuple[list[str], np.ndarray]:
    """Parse an ``ocnPrint`` table: returns (column labels, data[rows, cols])."""
    labels: list[str] = []
    rows: list[list[float]] = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        if all(_NUM.match(p) for p in parts):
            rows.append([float(p) for p in parts])
        elif not rows:
            labels = parts
    if not rows:
        return labels, np.zeros((0, 0))
    width = min(len(r) for r in rows)
    return labels, np.array([r[:width] for r in rows])
