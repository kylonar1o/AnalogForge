"""Classify ngspice failures and choose automatic remedies.

The goal is to turn cryptic simulator output into an actionable explanation
and, where it is safe, retry with standard convergence aids (gmin/source
stepping, larger iteration limits, a shunt resistance on every node, Gear
integration).  Every applied fix is recorded in the simulation result so the
user can see exactly what was changed - fixes never happen silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Diagnosis:
    category: str
    message: str
    evidence: str = ""
    suggestion: str = ""
    fixable: bool = False

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "message": self.message,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "fixable": self.fixable,
        }


@dataclass
class Fix:
    name: str
    description: str
    options: dict = field(default_factory=dict)
    reduces_accuracy: bool = False


# (category, regex, message, suggestion, fixable)
_PATTERNS: list[tuple[str, str, str, str, bool]] = [
    ("missing_model",
     r"(could not find a valid modelname|unable to find definition of model|unknown model type|"
     r"model\s+\S+\s+not\s+found|no model found|can't find model)",
     "A device references a model that is not defined.",
     "Check the technology selection; for SKY130 run `analogforge pdk install sky130` "
     "or set PDK_ROOT / ANALOGFORGE_SKY130_PATH.",
     False),
    ("missing_subckt",
     r"(unknown subckt|subcircuit\s+\S+\s+not\s+found|no such subcircuit)",
     "An instance references an undefined subcircuit.",
     "The PDK library was probably not found or the device name is misspelled.",
     False),
    ("missing_include",
     r"(could not find include file|can't open|cannot open|no such file)",
     "An included model/library file could not be opened.",
     "Run `analogforge doctor` to check the model paths.",
     False),
    ("undefined_parameter",
     r"(undefined parameter|cannot compute substitute|unknown parameter)",
     "A netlist parameter or model parameter is undefined.",
     "Check parameter names and that all required PDK parameter files are included.",
     False),
    ("netlist_syntax",
     r"(error on line|syntax error|unrecognized|too few nodes|incorrect number of nodes|"
     r"is not a valid .* line, ignored)",
     "The netlist contains a syntax error.",
     "Inspect the generated netlist (`analogforge netlist ...`).",
     False),
    ("singular_matrix",
     r"singular matrix",
     "The circuit matrix is singular (usually a floating node or a loop of ideal voltage sources/inductors).",
     "Every node needs a DC path to ground. A large shunt resistance (rshunt) is tried automatically.",
     True),
    ("dc_convergence",
     r"(gmin stepping failed|source stepping failed|no convergence in dc|iteration limit reached|"
     r"dc solution failed|transient op failed|op: no convergence|dcop failed|"
     r"failed to converge|no convergence)",
     "The DC operating point did not converge.",
     "Automatic retries with more iterations, gmin/source stepping and rshunt are applied; "
     "if they fail, add .nodeset hints or check that bias sources are sensible.",
     True),
    ("tran_timestep",
     r"(timestep too small|time step too small)",
     "The transient simulation could not reduce its timestep enough (convergence failure in time).",
     "Retried automatically with Gear integration and relaxed tolerances; consider a smaller tmax "
     "or smoother source edges.",
     True),
    ("missing_vector",
     r"(no such vector|vector\s+\S+\s+not found)",
     "A requested output vector does not exist.",
     "Check node names used in measurements.",
     False),
    ("fatal",
     r"(fatal error|segmentation fault|simulation\(s\) aborted|run simulation\(s\) aborted)",
     "ngspice aborted.",
     "See the full log for details.",
     True),
]

_COMPILED = [(c, re.compile(p, re.IGNORECASE), m, s, f) for c, p, m, s, f in _PATTERNS]


def diagnose(log: str) -> list[Diagnosis]:
    """Return diagnoses for every recognised problem in an ngspice log."""
    found: dict[str, Diagnosis] = {}
    for line in log.splitlines():
        for cat, rx, msg, sug, fixable in _COMPILED:
            if cat in found:
                continue
            if rx.search(line):
                found[cat] = Diagnosis(cat, msg, line.strip()[:300], sug, fixable)
    return list(found.values())


FIX_RELAXED_DC = Fix(
    "relaxed_dc_iterations",
    "More DC iterations with gmin and source stepping",
    {"itl1": 500, "itl2": 500, "gminsteps": 50, "srcsteps": 50},
)
FIX_RSHUNT = Fix(
    "rshunt",
    "1 TOhm shunt resistor from every node to ground (fixes floating nodes)",
    {"itl1": 500, "itl2": 500, "gminsteps": 50, "srcsteps": 50, "rshunt": 1e12},
)
FIX_GEAR = Fix(
    "gear_integration",
    "Gear integration and more transient iterations",
    {"method": "gear", "itl4": 100, "itl1": 500, "itl2": 500},
)
FIX_LOOSE = Fix(
    "loose_tolerances",
    "Relaxed tolerances (reltol=1e-2): results are less accurate",
    {"method": "gear", "itl1": 1000, "itl2": 1000, "itl4": 200, "reltol": 1e-2,
     "abstol": 1e-10, "vntol": 1e-5, "rshunt": 1e12},
    reduces_accuracy=True,
)


def plan_fixes(diagnoses: list[Diagnosis], missing_analyses: list[str]) -> list[Fix]:
    """Ordered list of fixes worth trying for the given failure."""
    cats = {d.category for d in diagnoses}
    if cats & {"missing_model", "missing_subckt", "missing_include", "undefined_parameter",
               "netlist_syntax", "missing_vector"}:
        return []  # deterministic user/setup errors: retrying cannot help
    plan: list[Fix] = []
    if "singular_matrix" in cats:
        plan += [FIX_RSHUNT]
    if "tran_timestep" in cats or (not cats and any(a.startswith("tran") for a in missing_analyses)):
        plan += [FIX_GEAR]
    if "dc_convergence" in cats or "fatal" in cats or not cats:
        plan += [FIX_RELAXED_DC, FIX_RSHUNT]
    plan.append(FIX_LOOSE)
    # de-duplicate while preserving order
    seen: set[str] = set()
    out = []
    for f in plan:
        if f.name not in seen:
            seen.add(f.name)
            out.append(f)
    return out
