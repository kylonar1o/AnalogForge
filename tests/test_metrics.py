"""Metric extraction on analytic waveforms with known answers."""

import math

import numpy as np
import pytest

from analogforge.metrics import amplifier as amp
from analogforge.metrics import oscillator as osc
from analogforge.metrics import phasenoise as pnm
from analogforge.metrics import time_average, waveform

f = np.logspace(0, 10, 2001)
s = 2j * np.pi * f


def test_single_pole():
    a0, fp = 1000.0, 1e4
    h = a0 / (1 + s / (2 * np.pi * fp))
    assert amp.dc_gain(f, h) == pytest.approx(a0, rel=1e-6)
    assert amp.bandwidth_3db(f, h) == pytest.approx(fp, rel=2e-3)
    assert amp.unity_gain_frequency(f, h) == pytest.approx(a0 * fp, rel=5e-3)
    assert amp.phase_margin(f, h) == pytest.approx(90.0, abs=0.2)
    assert amp.gain_margin(f, h) == float("inf")


def test_two_pole_phase_margin():
    a0, p1, p2 = 1e4, 1e3, 2e7
    h = a0 / ((1 + s / (2 * np.pi * p1)) * (1 + s / (2 * np.pi * p2)))
    fu = amp.unity_gain_frequency(f, h)
    expected = 180 - math.degrees(math.atan(fu / p1)) - math.degrees(math.atan(fu / p2))
    assert amp.phase_margin(f, h) == pytest.approx(expected, abs=0.3)


def test_three_pole_gain_margin():
    a0, p = 100.0, 1e6
    h = a0 / (1 + s / (2 * np.pi * p)) ** 3
    # phase -180 at w = sqrt(3) p -> |H| = a0/8 -> GM = -20log10(12.5)
    assert amp.gain_margin(f, h) == pytest.approx(-20 * math.log10(a0 / 8), abs=0.1)


def test_inverting_reference():
    h = -1000 / (1 + s / (2 * np.pi * 1e4))
    assert amp.phase_margin(f, h) == pytest.approx(90.0, abs=0.2)


def test_slew_and_settling():
    t = np.linspace(0, 1e-6, 10001)
    v = np.clip((t - 1e-7) * 1e7, 0, 1.0)              # 10 V/us ramp from 0 to 1 V
    v = np.where(t > 6e-7, np.clip(1 - (t - 6e-7) * 2e7, 0, 1), v)  # fall at 20 V/us
    rise, fall = amp.slew_rate(t, v)
    assert rise == pytest.approx(1e7, rel=1e-3) and fall == pytest.approx(2e7, rel=1e-3)
    tau = 10e-9
    v2 = 1 - np.exp(-np.maximum(t - 1e-7, 0) / tau)
    assert amp.settling_time(t, v2, 1e-7, 0.01) == pytest.approx(tau * math.log(100), rel=0.02)


def test_oscillator_frequency_amplitude():
    t = np.linspace(0, 20e-9, 40001)
    f0 = 2.4e9
    v = 0.8 * np.sin(2 * np.pi * f0 * t) * (1 - np.exp(-t / 2e-9))
    assert osc.osc_frequency(t, v, 0.5) == pytest.approx(f0, rel=1e-5)
    assert osc.fft_frequency(t, v, 0.5) == pytest.approx(f0, rel=2e-3)
    assert osc.amplitude(t, v, 0.2) == pytest.approx(0.8, rel=2e-3)
    assert osc.startup_time(t, v) == pytest.approx(-2e-9 * math.log(0.1), rel=0.1)
    assert osc.is_oscillating(t, v)
    assert not osc.is_oscillating(t, 1e-6 * v)


def test_thd_known_harmonics():
    f0 = 1e6
    t = np.linspace(0, 20e-6, 20001)
    v = np.sin(2 * np.pi * f0 * t) + 0.1 * np.sin(2 * np.pi * 2 * f0 * t) + 0.05 * np.sin(2 * np.pi * 3 * f0 * t)
    assert waveform.thd(t, v, f0) == pytest.approx(math.sqrt(0.1 ** 2 + 0.05 ** 2), rel=1e-3)


def test_tuning_fom_leeson():
    tm = osc.tuning_metrics([0, 0.9, 1.8], [3.0e9, 3.2e9, 3.6e9])
    assert tm["tuning_range_pct"] == pytest.approx(200 * 0.6 / 6.6)
    assert tm["kvco_max"] == pytest.approx(0.4e9 / 0.9)
    assert tm["monotonic"] == 1.0
    assert osc.fom(-120.0, 3e9, 1e6, 1e-3) == pytest.approx(20 * math.log10(3e3) + 120)
    assert osc.fom_t(180.0, 10.0) == pytest.approx(180.0)
    # Leeson slope: -20 dB/decade in the 1/f^2 region
    l1, l2 = osc.leeson([1e6, 1e7], 3e9, 10, 1e-3)
    assert l1 - l2 == pytest.approx(20.0, abs=0.05)


def test_isf_matches_leeson_for_ideal_tank():
    """For a sinusoidal ISF and tank noise only, the Hajimiri-Lee result equals Leeson with F=1."""
    C, L, Rp, A, T = 1e-12, 2e-9, 300.0, 0.8, 27.0
    w0 = 1 / math.sqrt(L * C)
    q_max = C * A
    x = np.linspace(0, 2 * np.pi, 64, endpoint=False)
    src = pnm.NoiseSourceISF("tank", x, np.cos(x) / q_max, pnm.thermal_current_psd(Rp, T))
    res = pnm.phase_noise_from_isf([src], [1e6])
    q = Rp / (w0 * L)
    lee = osc.leeson([1e6], w0 / (2 * math.pi), q, A ** 2 / (2 * Rp), 1.0, T + 273.15)
    assert res.l_dbc_hz[0] == pytest.approx(lee[0], abs=0.01)
    assert src.gamma_rms(q_max) == pytest.approx(1 / math.sqrt(2), rel=1e-6)


def test_phase_shift_measurement():
    t = np.linspace(0, 10e-9, 100001)
    f0 = 1e9
    a = np.sin(2 * np.pi * f0 * t + 0.01)
    b = np.sin(2 * np.pi * f0 * t)
    assert pnm.phase_shift(t, a, t, b, f0, 2e-9) == pytest.approx(0.01, rel=1e-3)


def test_time_average():
    t = np.linspace(0, 1, 1001)
    assert time_average(t, np.sin(2 * np.pi * 5 * t) + 2.0) == pytest.approx(2.0, abs=1e-3)
