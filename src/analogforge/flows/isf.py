"""Measure impulse sensitivity functions (ISF) of an oscillator with transient simulations.

Generic over the oscillator topology: the caller supplies a function that
builds the circuit with an optional injection current source, the output
signal expression and the injection locations.  Used by the LC-VCO design and
reusable for class-F VCOs, ring oscillators or RTWOs.  See
:mod:`analogforge.metrics.phasenoise` for the theory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np

from ..core.analyses import Tran
from ..core.circuit import Circuit, Pulse
from ..core.design import DesignContext
from ..core.testbench import Testbench
from ..metrics.phasenoise import phase_shift


@dataclass
class InjectionSite:
    """Charge is pushed out of ``node_from`` and into ``node_to`` (like the noise source it models)."""

    name: str
    node_from: str
    node_to: str


@dataclass
class ISFData:
    site: InjectionSite
    phases: np.ndarray       # rad
    sensitivity: np.ndarray  # dphi/dq [rad/C]
    dq: float


def measure_isf(ctx: DesignContext, build: Callable[[], Circuit], output: str, sites: list[InjectionSite],
                f0: float, t_ref: float, dq: float, tmax: float, points: int = 12,
                settle_periods: int = 8, observe_periods: int = 4, options: dict | None = None) -> list[ISFData]:
    """Return the ISF samples (as dphi/dq) for each injection site.

    ``t_ref`` must be a rising zero crossing of ``output`` in steady state;
    injections happen at ``t_ref + k/points * T0``.  For each phase two runs
    with +dq and -dq are made and half their phase difference is used.
    """
    if ctx.simulate is None:
        raise RuntimeError("ISF measurement needs a DesignContext with a simulate() callback")
    T0 = 1.0 / f0
    width = T0 / 50.0
    edge = T0 / 200.0
    amp = dq / (width + edge)  # trapezoid area = amp * (width + edge) for equal rise/fall
    tbs: list[Testbench] = []
    index = []
    for site in sites:
        for k in range(points):
            t_k = t_ref + k * T0 / points
            tstop = t_k + (settle_periods + observe_periods + 1) * T0
            for sign in (+1, -1):
                circ = build()
                circ.isource("IINJ", site.node_from, site.node_to,
                             waveform=Pulse(0.0, sign * amp, t_k, edge, edge, width, 1.0))
                name = f"isf_{site.name}_{k}_{'p' if sign > 0 else 'm'}"
                tbs.append(Testbench(name, circ, [Tran("tran", tstop=tstop, tstep=tmax, tmax=tmax)],
                                     options=dict(options or {})))
                index.append((site.name, k, sign, name, t_k))
    results = ctx.simulate(tbs)
    out = []
    for site in sites:
        phases = 2 * math.pi * np.arange(points) / points
        h = np.full(points, np.nan)
        for k in range(points):
            rp = results[f"isf_{site.name}_{k}_p"]
            rm = results[f"isf_{site.name}_{k}_m"]
            if not (rp.ok and rm.ok):
                continue
            tp, tm = rp["tran"], rm["tran"]
            t_k = t_ref + k * T0 / points
            dphi = phase_shift(tp.sweep, np.real(tp.eval(output)), tm.sweep, np.real(tm.eval(output)), f0,
                               t_from=t_k + settle_periods * T0, n_cross=observe_periods)
            h[k] = 0.5 * dphi / dq
        out.append(ISFData(site, phases, h, dq))
    return out
