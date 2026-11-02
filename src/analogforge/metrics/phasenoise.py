"""Phase noise from the impulse sensitivity function (Hajimiri & Lee, JSSC 1998).

The ISF is measured with transient simulations (works with any time-domain
simulator, in particular ngspice which has no ``pnoise`` analysis):

1. Simulate the oscillator into steady state; take a reference rising zero
   crossing ``t_ref`` of the output and the period ``T0``.
2. For K phases ``x_k = 2*pi*k/K`` inject a short current pulse of charge
   ``+dq`` and, separately, ``-dq`` across the terminals of a noise source at
   ``t_ref + k*T0/K``.
3. After the perturbation has settled (several periods), compare zero
   crossing times with the run of opposite sign:
   ``dphi_k = -2*pi*f0*(t_cross(+dq) - t_cross(-dq)) / 2``.
   The symmetric (+/-) difference cancels the numerical phase error of the
   time-step control and the second-order (non-linear) response.
4. The sensitivity ``h(x_k) = dphi_k/dq`` [rad/C] is the ISF divided by
   ``q_max`` (``Gamma = h*q_max``); ``q_max`` cancels in the result.

For white noise sources with (single-sided) current PSD ``S_i(x)`` [A^2/Hz]
(possibly cyclostationary, i.e. a function of the oscillation phase) the
1/f^2 phase noise is

    L(df) = 10 log10( sum_sources mean_x( h(x)^2 * S_i(x) ) / (2 * (2*pi*df)^2) )

which reduces to Hajimiri's ``Gamma_rms^2 * (i_n^2/df) / (2 q_max^2 dw^2)``
for a stationary source.  Flicker-noise up-conversion (1/f^3 region) and
tail-current noise are *not* included - see docs/phase_noise.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .waveform import crossings

K_BOLTZMANN = 1.380649e-23
NAN = float("nan")


def phase_shift(t_a, v_a, t_b, v_b, f0: float, t_from: float, n_cross: int = 5) -> float:
    """Phase difference (rad) of waveform a relative to b, from rising crossings after ``t_from``.

    Positive = a leads b (crosses earlier)."""
    ca = crossings(t_a, v_a, 0.0, "rise")
    cb = crossings(t_b, v_b, 0.0, "rise")
    ca = ca[ca > t_from]
    cb = cb[cb > t_from]
    n = min(len(ca), len(cb), n_cross)
    if n < 1:
        return NAN
    # pair each crossing of a with the nearest crossing of b
    T = 1.0 / f0
    diffs = []
    for x in ca[:n]:
        j = int(np.argmin(np.abs(cb - x)))
        d = x - cb[j]
        if abs(d) < T / 2:
            diffs.append(d)
    if not diffs:
        return NAN
    return float(-2 * math.pi * f0 * np.mean(diffs))


@dataclass
class NoiseSourceISF:
    """ISF samples of one noise-source location plus its noise PSD (scalar or per phase)."""

    name: str
    phases: np.ndarray              # x_k in rad, 0..2pi
    sensitivity: np.ndarray         # h(x_k) = dphi/dq  [rad/C]
    psd: np.ndarray | float         # single-sided current PSD [A^2/Hz], scalar or per x_k
    multiplicity: int = 1           # identical sources by symmetry (e.g. both tank halves)

    def weighted_mean(self) -> float:
        s = np.broadcast_to(np.asarray(self.psd, float), self.sensitivity.shape)
        return float(self.multiplicity * np.mean(self.sensitivity ** 2 * s))

    def gamma_rms(self, q_max: float) -> float:
        return float(np.sqrt(np.mean((self.sensitivity * q_max) ** 2)))

    def gamma_dc(self, q_max: float) -> float:
        return float(np.mean(self.sensitivity * q_max))


@dataclass
class PhaseNoiseResult:
    offsets: np.ndarray
    l_dbc_hz: np.ndarray
    contributions: dict[str, float] = field(default_factory=dict)  # share of total, 0..1
    method: str = "isf"

    def at(self, f_offset: float) -> float:
        return float(np.interp(math.log10(f_offset), np.log10(self.offsets), self.l_dbc_hz))

    def to_dict(self) -> dict:
        return {"method": self.method, "offsets": self.offsets.tolist(),
                "l_dbc_hz": self.l_dbc_hz.tolist(), "contributions": self.contributions}


def phase_noise_from_isf(sources: list[NoiseSourceISF], offsets) -> PhaseNoiseResult:
    offsets = np.asarray(offsets, float)
    parts = {s.name: s.weighted_mean() for s in sources}
    total = sum(parts.values())
    if not total > 0:
        return PhaseNoiseResult(offsets, np.full_like(offsets, NAN), {}, "isf")
    l = total / (2 * (2 * math.pi * offsets) ** 2)
    return PhaseNoiseResult(offsets, 10 * np.log10(l), {k: v / total for k, v in parts.items()}, "isf")


def thermal_current_psd(resistance: float, temperature_c: float = 27.0) -> float:
    """4kT/R  [A^2/Hz]."""
    return 4 * K_BOLTZMANN * (temperature_c + 273.15) / resistance


def channel_noise_psd(gm, gamma: float = 1.0, temperature_c: float = 27.0):
    """4 k T gamma g_m  [A^2/Hz] (long-channel style estimate; gamma is a user parameter)."""
    return 4 * K_BOLTZMANN * (temperature_c + 273.15) * gamma * np.maximum(np.asarray(gm, float), 0.0)
