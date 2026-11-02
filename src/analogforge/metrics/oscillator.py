"""Oscillator metrics: frequency, amplitude, start-up, tuning range, K_VCO, FoM, Leeson."""

from __future__ import annotations

import math

import numpy as np

from .waveform import crossings, spectrum, steady_mask

NAN = float("nan")
K_BOLTZMANN = 1.380649e-23


def osc_frequency(t, v, fraction: float = 0.5) -> float:
    """Mean frequency from interpolated rising crossings of ``v`` through its mean value,
    using the last ``fraction`` of the record (steady state)."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    m = steady_mask(t, fraction)
    if m.sum() < 10:
        return NAN
    tt, vv = t[m], v[m]
    tc = crossings(tt, vv, float(np.mean(vv)), "rise")
    if len(tc) < 3:
        return NAN
    return float((len(tc) - 1) / (tc[-1] - tc[0]))


def period_jitter(t, v, fraction: float = 0.5) -> float:
    """Standard deviation of the period (s) in the steady-state window (numerical noise floor
    for a noiseless transient - useful to judge simulation accuracy)."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    m = steady_mask(t, fraction)
    tc = crossings(t[m], v[m], float(np.mean(v[m])), "rise")
    return float(np.std(np.diff(tc))) if len(tc) > 3 else NAN


def fft_frequency(t, v, fraction: float = 0.5) -> float:
    """Frequency of the largest spectral line (Hann window, parabolic peak interpolation)."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    m = steady_mask(t, fraction)
    if m.sum() < 16:
        return NAN
    f, a = spectrum(t[m], v[m], n=max(4096, 4 * int(m.sum())))
    k = int(np.argmax(a[1:]) + 1)
    if 1 <= k < len(a) - 1:
        y0, y1, y2 = np.log(a[k - 1] + 1e-300), np.log(a[k] + 1e-300), np.log(a[k + 1] + 1e-300)
        den = y0 - 2 * y1 + y2
        delta = 0.5 * (y0 - y2) / den if den != 0 else 0.0
        return float(f[k] + delta * (f[1] - f[0]))
    return float(f[k])


def amplitude(t, v, fraction: float = 0.3) -> float:
    """Half the peak-to-peak value in the last ``fraction`` of the record."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    m = steady_mask(t, fraction)
    if m.sum() < 3:
        return NAN
    return float((np.max(v[m]) - np.min(v[m])) / 2)


def startup_time(t, v, level: float = 0.9, fraction: float = 0.3) -> float:
    """First time the signal reaches ``level`` times its steady-state amplitude."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    amp = amplitude(t, v, fraction)
    if not np.isfinite(amp) or amp <= 0:
        return NAN
    vc = v - np.mean(v[steady_mask(t, fraction)])
    idx = np.where(np.abs(vc) >= level * amp)[0]
    return float(t[idx[0]]) if len(idx) else NAN


def is_oscillating(t, v, min_amplitude: float = 1e-3, fraction: float = 0.3) -> bool:
    amp = amplitude(t, v, fraction)
    f = osc_frequency(t, v, max(fraction, 0.3))
    return bool(np.isfinite(amp) and amp >= min_amplitude and np.isfinite(f))


def tuning_metrics(vtune, freq) -> dict[str, float]:
    """Tuning range and K_VCO from a frequency-vs-control-voltage curve."""
    vt = np.asarray(vtune, float)
    f = np.asarray(freq, float)
    ok = np.isfinite(f)
    out = {"f_min": NAN, "f_max": NAN, "tuning_range_pct": NAN, "kvco_max": NAN,
           "kvco_mean": NAN, "monotonic": 0.0}
    if ok.sum() < 2:
        return out
    vt, f = vt[ok], f[ok]
    order = np.argsort(vt)
    vt, f = vt[order], f[order]
    fmin, fmax = float(np.min(f)), float(np.max(f))
    k = np.diff(f) / np.diff(vt)
    out.update({
        "f_min": fmin, "f_max": fmax,
        "tuning_range_pct": 200.0 * (fmax - fmin) / (fmax + fmin),
        "kvco_max": float(np.max(np.abs(k))),
        "kvco_mean": float(abs(f[-1] - f[0]) / (vt[-1] - vt[0])),
        "monotonic": float(np.all(k > 0) or np.all(k < 0)),
    })
    return out


def fom(pn_dbc_hz: float, f0: float, f_offset: float, p_dc: float) -> float:
    """Oscillator figure of merit (dB, higher is better):
    FoM = 20 log10(f0/df) - L(df) - 10 log10(P_DC / 1 mW)."""
    if not all(np.isfinite([pn_dbc_hz, f0, f_offset, p_dc])) or p_dc <= 0:
        return NAN
    return float(20 * math.log10(f0 / f_offset) - pn_dbc_hz - 10 * math.log10(p_dc / 1e-3))


def fom_t(fom_db: float, tuning_range_pct: float) -> float:
    """FoM including tuning range: FoM_T = FoM + 20 log10(TR[%] / 10)."""
    if not (np.isfinite(fom_db) and np.isfinite(tuning_range_pct)) or tuning_range_pct <= 0:
        return NAN
    return float(fom_db + 20 * math.log10(tuning_range_pct / 10.0))


def leeson(f_offset, f0: float, q_loaded: float, p_signal: float, noise_factor: float = 2.0,
           temperature_k: float = 300.15, f_flicker: float = 0.0):
    """Leeson's phase-noise model in dBc/Hz:

    L(df) = 10 log10[ (2 F k T / P_sig) * (1 + (f0 / (2 Q df))^2) * (1 + f_1/f^3 / df) ]
    """
    df = np.asarray(f_offset, float)
    if p_signal <= 0 or q_loaded <= 0:
        return np.full_like(df, NAN)
    core = 2 * noise_factor * K_BOLTZMANN * temperature_k / p_signal
    l = core * (1 + (f0 / (2 * q_loaded * df)) ** 2) * (1 + f_flicker / df)
    return 10 * np.log10(l)
