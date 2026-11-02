"""A testbench = circuit + analyses + simulation conditions (simulator neutral)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .analyses import Analysis
from .circuit import Circuit


@dataclass
class Testbench:
    __test__ = False  # not a pytest test class

    name: str
    circuit: Circuit
    analyses: list[Analysis]
    corner: str | None = None
    temperature: float = 27.0
    #: devices whose operating-point parameters are saved with the ``op`` analysis
    save_op: list[str] = field(default_factory=list)
    #: neutral simulator options: reltol, abstol, vntol, gmin, maxstep, method ('gear'/'trap')
    options: dict = field(default_factory=dict)
    description: str = ""
    #: design-variable map, used by backends that keep variables symbolic (OCEAN cellview mode)
    variables: dict[str, float] = field(default_factory=dict)
    #: signals a measurement needs; backends that cannot save "everything" export only these
    probes: list[str] = field(default_factory=list)

    def analysis_kinds(self) -> set[str]:
        return {a.kind for a in self.analyses}
