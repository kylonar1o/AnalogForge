"""Amplifier metrics: gain, bandwidth, unity-gain frequency, margins, slew rate, settling."""

from __future__ import annotations

import math

import numpy as np

NAN = float("nan")


def db20(x) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(x), 1e-300))


def phase_deg(h) -> np.ndarray:
    """Unwrapped phase in degrees."""
    return np.degrees(np.unwrap(np.angle(h)))


def _crossing_log(f: np.ndarray, y: np.ndarray, level: float, falling: bool = True) -> float:
    f = np.asarray(f, float)
    for i in range(len(y) - 1):
        a, b = y[i], y[i + 1]
        if (a >= level > b) if falling else (a <= level < b):
            if b == a:
                return float(f[i])
            t = (level - a) / (b - a)
            return float(10 ** (math.log10(f[i]) + t * (math.log10(f[i + 1]) - math.log10(f[i]))))
    return NAN


def _at_log(f: np.ndarray, y: np.ndarray, f0: float) -> float:
    if not np.isfinite(f0):
        return NAN
    return float(np.interp(math.log10(f0), np.log10(np.asarray(f, float)), y))


def dc_gain(freq, h) -> float:
    """|H| at the lowest simulated frequency."""
    return float(np.abs(np.asarray(h)[0]))


def bandwidth_3db(freq, h) -> float:
    """-3 dB frequency relative to the low-frequency gain (NaN if not reached)."""
    mag = db20(h)
    return _crossing_log(freq, mag, mag[0] - 3.0103, falling=True)


def unity_gain_frequency(freq, h) -> float:
    mag = db20(h)
    if mag[0] <= 0:
        return NAN
    return _crossing_log(freq, mag, 0.0, falling=True)


def phase_margin(freq, h) -> float:
    """PM = 180 deg minus the phase lag accumulated between DC and the unity-gain frequency.

    Referencing the phase to its low-frequency value makes the result
    independent of whether the measured transfer function is inverting.
    """
    fu = unity_gain_frequency(freq, h)
    if not np.isfinite(fu):
        return NAN
    ph = phase_deg(h)
    return float(180.0 - (ph[0] - _at_log(freq, ph, fu)))


def gain_margin(freq, h) -> float:
    """-|H| (dB) where the phase lag reaches 180 deg; +inf if it never does in band."""
    ph = phase_deg(h)
    lag = ph[0] - ph
    f180 = _crossing_log(freq, lag, 180.0, falling=False)
    if not np.isfinite(f180):
        return float("inf")
    return float(-_at_log(freq, db20(h), f180))


def slew_rate(t, v, lo: float = 0.2, hi: float = 0.8) -> tuple[float, float]:
    """(rising, falling) slew rates in V/s measured between ``lo`` and ``hi`` of the swing."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    vmin, vmax = float(np.min(v)), float(np.max(v))
    span = vmax - vmin
    if span <= 0:
        return NAN, NAN
    v_lo, v_hi = vmin + lo * span, vmin + hi * span

    def cross(level, rising, start=0):
        for i in range(start, len(v) - 1):
            if (v[i] < level <= v[i + 1]) if rising else (v[i] > level >= v[i + 1]):
                return t[i] + (level - v[i]) * (t[i + 1] - t[i]) / (v[i + 1] - v[i]), i
        return NAN, len(v)

    t1, i1 = cross(v_lo, True)
    t2, i2 = cross(v_hi, True, i1)
    t3, i3 = cross(v_hi, False, i2)
    t4, _ = cross(v_lo, False, i3)
    rise = (v_hi - v_lo) / (t2 - t1) if np.isfinite(t2 - t1) and t2 > t1 else NAN
    fall = (v_hi - v_lo) / (t4 - t3) if np.isfinite(t4 - t3) and t4 > t3 else NAN
    return float(rise), float(fall)


def settling_time(t, v, t_step: float, tol: float = 0.01, t_end: float | None = None) -> float:
    """Time after ``t_step`` until ``v`` stays within ``tol`` x step size of its final value."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    m = t >= t_step
    if t_end is not None:
        m &= t <= t_end
    tt, vv = t[m], v[m]
    if len(tt) < 3:
        return NAN
    final = vv[-1]
    band = tol * max(abs(final - float(np.interp(t_step, t, v))), 1e-12)
    out = np.where(np.abs(vv - final) > band)[0]
    if len(out) == 0:
        return 0.0
    if out[-1] >= len(tt) - 1:
        return NAN
    return float(tt[out[-1] + 1] - t_step)


def output_swing(vin, vout, gain_fraction: float = 0.5) -> tuple[float, float]:
    """Output range where the incremental gain stays above ``gain_fraction`` of its peak."""
    vin = np.asarray(vin, float)
    vout = np.asarray(vout, float)
    g = np.abs(np.gradient(vout, vin))
    if not len(g) or np.max(g) <= 0:
        return NAN, NAN
    ok = g >= gain_fraction * np.max(g)
    return float(np.min(vout[ok])), float(np.max(vout[ok]))


def supply_power(vdd: float, i_source: float) -> float:
    """Power delivered by a supply.  Source currents are positive *into* the + terminal,
    so a supply delivering power has a negative current."""
    return float(abs(vdd * i_source))
