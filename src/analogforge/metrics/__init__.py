"""Metric library and the registry of named extractors used by YAML designs."""

from __future__ import annotations

from typing import Callable

import numpy as np

from . import amplifier, oscillator, phasenoise, waveform

NAN = float("nan")


def _xy(ds, signal):
    return ds.sweep, ds.eval(signal)


def time_average(t, y, fraction: float = 1.0) -> float:
    """Trapezoidal time average of ``y`` over the last ``fraction`` of the record."""
    t = np.asarray(t, float)
    y = np.real(np.asarray(y))
    m = waveform.steady_mask(t, fraction)
    if m.sum() < 2:
        return float(np.mean(y))
    trap = getattr(np, "trapezoid", None) or np.trapz  # numpy < 2.0 compatibility
    return float(trap(y[m], t[m]) / (t[m][-1] - t[m][0]))


def _supply_power(ds, source, vdd, fraction=0.5):
    i = np.real(ds.i(source)).reshape(-1)
    if ds.kind == "tran" and len(i) > 1:
        return abs(vdd * time_average(ds.sweep, i, fraction))
    return abs(vdd * float(i[0]))


#: name -> callable(dataset, signal, **kwargs) -> float.  Used by YAML ``metrics:``.
EXTRACTORS: dict[str, Callable] = {
    # transient / generic
    "osc_frequency": lambda ds, s, fraction=0.5: oscillator.osc_frequency(*_xy(ds, s), fraction),
    "fft_frequency": lambda ds, s, fraction=0.5: oscillator.fft_frequency(*_xy(ds, s), fraction),
    "amplitude": lambda ds, s, fraction=0.3: oscillator.amplitude(*_xy(ds, s), fraction),
    "startup_time": lambda ds, s, level=0.9: oscillator.startup_time(*_xy(ds, s), level),
    "thd": lambda ds, s, f0, n_harm=5: waveform.thd(*_xy(ds, s), f0, n_harm),
    "slew_rate": lambda ds, s: float(np.nanmin(amplifier.slew_rate(*_xy(ds, s)))),
    "settling_time": lambda ds, s, t_step, tol=0.01: amplifier.settling_time(*_xy(ds, s), t_step, tol),
    "max": lambda ds, s: float(np.max(np.real(ds.eval(s)))),
    "min": lambda ds, s: float(np.min(np.real(ds.eval(s)))),
    "mean": lambda ds, s: float(np.mean(np.real(ds.eval(s)))),
    "rms": lambda ds, s: float(np.sqrt(np.mean(np.real(ds.eval(s)) ** 2))),
    "value": lambda ds, s: float(np.real(np.asarray(ds.eval(s)).reshape(-1)[0])),
    "final": lambda ds, s: float(np.real(np.asarray(ds.eval(s)).reshape(-1)[-1])),
    # AC
    "dc_gain": lambda ds, s: amplifier.dc_gain(*_xy(ds, s)),
    "gain_db": lambda ds, s: float(amplifier.db20(amplifier.dc_gain(*_xy(ds, s)))),
    "bandwidth_3db": lambda ds, s: amplifier.bandwidth_3db(*_xy(ds, s)),
    "unity_gain_frequency": lambda ds, s: amplifier.unity_gain_frequency(*_xy(ds, s)),
    "phase_margin": lambda ds, s: amplifier.phase_margin(*_xy(ds, s)),
    "gain_margin": lambda ds, s: amplifier.gain_margin(*_xy(ds, s)),
    # power: ``signal`` is the supply source name, kwarg vdd = supply voltage
    "supply_power": _supply_power,
}

__all__ = ["EXTRACTORS", "amplifier", "oscillator", "phasenoise", "waveform"]
