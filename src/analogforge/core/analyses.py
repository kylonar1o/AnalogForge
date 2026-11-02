"""Simulator-neutral analysis descriptions.

Backends translate these into their own syntax (or raise
:class:`UnsupportedAnalysis`).  Every analysis has a ``name`` that becomes the
key of its :class:`~analogforge.core.results.Dataset` in the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class UnsupportedAnalysis(NotImplementedError):
    pass


@dataclass
class Analysis:
    name: str = ""
    kind = "base"

    def key(self) -> str:
        return self.name or self.kind


@dataclass
class Op(Analysis):
    kind = "op"


@dataclass
class DCSweep(Analysis):
    source: str = ""
    start: float = 0.0
    stop: float = 1.0
    step: float = 0.01
    kind = "dc"


@dataclass
class AC(Analysis):
    fstart: float = 1.0
    fstop: float = 1e9
    points_per_decade: int = 20
    kind = "ac"


@dataclass
class Tran(Analysis):
    tstop: float = 1e-6
    tstep: float = 1e-9
    tstart: float = 0.0
    tmax: float | None = None
    uic: bool = False
    #: device parameters recorded versus time, neutral names like "M1:gm"
    save: list[str] = field(default_factory=list)
    kind = "tran"


@dataclass
class Noise(Analysis):
    """Small-signal noise: output between ``out_p`` and ``out_n``, input-referred to ``input_source``."""

    out_p: str = "out"
    out_n: str = "0"
    input_source: str = "Vin"
    fstart: float = 1.0
    fstop: float = 1e9
    points_per_decade: int = 10
    kind = "noise"


@dataclass
class PSS(Analysis):
    """Periodic steady state.  For autonomous oscillators give ``osc_p``/``osc_n``
    and an estimate of the fundamental ``fund``."""

    fund: float = 1e9
    harmonics: int = 10
    tstab: float = 10e-9
    osc_p: str | None = None
    osc_n: str | None = "0"
    points: int = 256
    kind = "pss"


@dataclass
class PNoise(Analysis):
    """Phase noise of an oscillator (requires a preceding PSS in the same testbench)."""

    fstart: float = 1e3
    fstop: float = 100e6
    points_per_decade: int = 10
    osc_p: str = "outp"
    osc_n: str = "outn"
    max_sideband: int = 10
    offsets: list[float] = field(default_factory=lambda: [100e3, 1e6, 10e6])
    kind = "pnoise"


ANALYSIS_KINDS = ("op", "dc", "ac", "tran", "noise", "pss", "pnoise")
