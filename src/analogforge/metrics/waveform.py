"""Generic waveform utilities (pure numpy, simulator independent)."""

from __future__ import annotations

import math

import numpy as np

NAN = float("nan")


def crossings(t: np.ndarray, v: np.ndarray, level: float = 0.0, direction: str = "rise") -> np.ndarray:
    """Linearly interpolated times where ``v`` crosses ``level`` (``rise``, ``fall`` or ``both``)."""
    t = np.asarray(t, float)
    v = np.asarray(v, float) - level
    if len(v) < 2:
        return np.zeros(0)
    a, b = v[:-1], v[1:]
    if direction == "rise":
        idx = np.where((a < 0) & (b >= 0))[0]
    elif direction == "fall":
        idx = np.where((a > 0) & (b <= 0))[0]
    else:
        idx = np.where(((a < 0) & (b >= 0)) | ((a > 0) & (b <= 0)))[0]
    denom = b[idx] - a[idx]
    denom[denom == 0] = 1e-300
    return t[idx] - a[idx] * (t[idx + 1] - t[idx]) / denom


def steady_mask(t: np.ndarray, fraction: float) -> np.ndarray:
    t = np.asarray(t, float)
    return t >= t[0] + (t[-1] - t[0]) * (1.0 - fraction)


def uniform_resample(t: np.ndarray, v: np.ndarray, n: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    n = n or max(256, len(t))
    tu = np.linspace(t[0], t[-1], n)
    return tu, np.interp(tu, t, v)


def spectrum(t: np.ndarray, v: np.ndarray, n: int | None = None, window: str = "hann") -> tuple[np.ndarray, np.ndarray]:
    """Single-sided amplitude spectrum of a (non-uniformly sampled) waveform."""
    tu, vu = uniform_resample(t, v, n)
    vu = vu - np.mean(vu)
    win = np.hanning(len(vu)) if window == "hann" else np.ones(len(vu))
    spec = np.fft.rfft(vu * win)
    amp = 2.0 * np.abs(spec) / np.sum(win)
    return np.fft.rfftfreq(len(vu), tu[1] - tu[0]), amp


def harmonics(t: np.ndarray, v: np.ndarray, f0: float, n_harm: int = 5, periods: int | None = None) -> np.ndarray:
    """Amplitudes of harmonics 1..n_harm using an integer number of periods (exact bins)."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    T = 1.0 / f0
    k = int((t[-1] - t[0]) / T)
    if periods is not None:
        k = min(k, periods)
    if k < 1:
        return np.full(n_harm, NAN)
    n = 64 * k * max(8, n_harm + 1)
    tu = np.linspace(t[-1] - k * T, t[-1], n, endpoint=False)
    vu = np.interp(tu, t, v)
    spec = np.abs(np.fft.rfft(vu - np.mean(vu))) * 2 / n
    return np.array([spec[h * k] if h * k < len(spec) else NAN for h in range(1, n_harm + 1)])


def thd(t: np.ndarray, v: np.ndarray, f0: float, n_harm: int = 5, periods: int | None = None) -> float:
    """Total harmonic distortion as a ratio (multiply by 100 for %)."""
    h = harmonics(t, v, f0, n_harm, periods)
    if not np.isfinite(h[0]) or h[0] <= 0:
        return NAN
    return float(math.sqrt(np.nansum(h[1:] ** 2)) / h[0])


def rel_error(expected: float, measured: float) -> float:
    if not (np.isfinite(expected) and np.isfinite(measured)) or expected == 0:
        return NAN
    return float((measured - expected) / abs(expected))
