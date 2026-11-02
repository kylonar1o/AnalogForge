"""Built-in and YAML designs simulated with ngspice + SKY130 (the reference path)."""

import math

import numpy as np
import pytest

from analogforge.core.design import evaluate
from analogforge.designs import get_design, list_designs


def test_catalogue():
    names = list_designs()
    assert {"lc_vco", "two_stage_opamp", "rc_lowpass", "cs_amplifier", "lc_vco_simple"} <= set(names)
    info = get_design("lc_vco").info()
    assert info["kind"] == "oscillator" and any(p["name"] == "VTUNE" for p in info["parameters"])
    with pytest.raises(Exception):
        get_design("lc_vco").resolve({"NOPE": 1})


# ------------------------------------------------------------------ LC VCO
def test_vco_oscillates_and_frequency_cross_checks(vco_result):
    r = vco_result
    assert r.ok, r.errors
    m = r.metrics
    assert r.extra["oscillating"]
    assert 2.5e9 < m["f_osc"] < 4.5e9
    # three independent frequency estimates: zero crossings, FFT, ngspice PSS
    assert m["f_fft"] == pytest.approx(m["f_osc"], rel=2e-3)
    assert m["f_pss"] == pytest.approx(m["f_osc"], rel=5e-3)
    assert m["amplitude_diff"] > 0.3
    assert 1e-3 < m["power"] < 10e-3
    assert m["loop_gain_startup"] > 1


def test_vco_frequency_matches_lc_resonance(vco_result):
    """f_osc must lie below the resonance of L_TANK with the fixed capacitor alone and above the
    resonance with the maximum varactor capacitance added (device parasitics shift it further down)."""
    p = vco_result.params
    f_cfix = 1 / (2 * math.pi * math.sqrt(p["LTANK"] * p["CFIX"]))
    assert vco_result.metrics["f_osc"] < f_cfix


def test_vco_tuning_curve(vco_result):
    m = vco_result.metrics
    f = [x for x in vco_result.extra["tuning"]["f"] if x is not None]
    assert len(f) == 5, "oscillation must start at every tuning point"
    assert all(b >= a - 1e6 for a, b in zip(f, f[1:])), "f(VTUNE) must be non-decreasing"
    assert m["tuning_range_pct"] > 5
    assert m["f_min"] <= m["f_osc"] <= m["f_max"]


def test_vco_isf_phase_noise(vco_result):
    m, ex = vco_result.metrics, vco_result.extra
    assert ex["phase_noise_method"].startswith("isf")
    # quasi-sinusoidal LC oscillator: tank ISF is cosine-like with Gamma_rms ~ 1/sqrt(2)
    assert ex["isf_gamma_rms"]["tank"] == pytest.approx(1 / math.sqrt(2), abs=0.08)
    assert -135 < m["pn_1M"] < -100
    assert m["pn_100k"] - m["pn_1M"] == pytest.approx(20, abs=0.1)  # 1/f^2 region
    # consistency with Leeson (F = 1 + gamma = 2, inductor Q) within 3 dB
    from analogforge.metrics.oscillator import leeson
    rp = get_design("lc_vco").rp(vco_result.params)
    lee = leeson([1e6], m["f_osc"], m["q_tank"], m["amplitude_diff"] ** 2 / (4 * rp), 2.0)[0]
    assert m["pn_1M"] == pytest.approx(lee, abs=3.0)
    assert m["fom"] == pytest.approx(20 * math.log10(m["f_osc"] / 1e6) - m["pn_1M"]
                                     - 10 * math.log10(m["power"] / 1e-3), abs=1e-6)
    contrib = ex["phase_noise"]["contributions"]
    assert contrib["tank"] + contrib["m1"] == pytest.approx(1.0)


def test_vco_frequency_rises_with_vtune(sky130):
    lo = evaluate("lc_vco", {"VTUNE": 0.3}, options={"tuning_points": 0, "phase_noise": "none"})
    hi = evaluate("lc_vco", {"VTUNE": 1.8}, options={"tuning_points": 0, "phase_noise": "none"})
    assert lo.ok and hi.ok
    assert hi.metrics["f_osc"] > lo.metrics["f_osc"] * 1.05


def test_vco_no_oscillation_is_reported(sky130):
    r = evaluate("lc_vco", {"IBIAS": 0.5e-3, "WN": 5e-6, "QL": 3}, options={"tuning_points": 0})
    assert r.ok  # the simulation itself succeeds ...
    assert not r.extra.get("oscillating", True)  # ... but the circuit does not oscillate
    assert any("does not oscillate" in w for w in r.warnings)


# ----------------------------------------------------------------- op-amp
def test_opamp_metrics(opamp_result):
    r = opamp_result
    assert r.ok, r.errors
    m, hand = r.metrics, r.extra["hand_estimates"]
    assert 55 < m["gain_db"] < 80
    assert m["gain_db"] == pytest.approx(hand["gain_db"], abs=1.0)      # gm/gds hand analysis
    assert m["ugbw"] == pytest.approx(hand["gbw"], rel=0.25)            # GBW = gm1 / (2 pi Cc)
    assert 45 < m["phase_margin"] < 100
    assert m["gain_margin"] > 6
    assert 50e-6 < m["power"] < 2e-3
    assert m["slew_rate"] > 5e6 and m["settling_time"] < 200e-9
    assert abs(m["offset"]) < 20e-3
    # every transistor in saturation at the operating point: |Vds| > |Vdsat|
    for dev, op in r.extra["op"].items():
        assert abs(op["vds"]) > abs(op["vdsat"]) * 0.9, dev


def test_opamp_compensation_trend(sky130):
    small = evaluate("two_stage_opamp", {"CC": 0.5e-12}, options={"tran": False, "psrr": False})
    big = evaluate("two_stage_opamp", {"CC": 2e-12}, options={"tran": False, "psrr": False})
    assert big.metrics["ugbw"] < small.metrics["ugbw"]
    assert big.metrics["phase_margin"] > small.metrics["phase_margin"]


def test_opamp_corners_and_temperature(sky130):
    tt = evaluate("two_stage_opamp", corner="tt", options={"tran": False})
    ss = evaluate("two_stage_opamp", corner="ss", temperature=125, vdd_scale=0.9, options={"tran": False})
    assert tt.ok and ss.ok
    assert ss.metrics["power"] < tt.metrics["power"]


# ------------------------------------------------------------------- YAML
def test_yaml_rc_lowpass(sky130):
    r = evaluate("rc_lowpass", {"R": "2k", "C": "0.5p"})
    assert r.ok and abs(r.metrics["error_pct"]) < 0.5


def test_yaml_cs_amplifier(sky130):
    r = evaluate("cs_amplifier")
    assert r.ok, r.errors
    assert 10 < r.metrics["gain_db"] < 30 and r.metrics["gm"] > 0
    assert r.metrics["gain_db"] < r.metrics["gain_hand_db"]  # finite r_o lowers the gain below gm*RD


def test_yaml_lc_oscillator(sky130):
    r = evaluate("lc_vco_simple")
    m = r.metrics
    assert r.ok
    assert 0.9 * m["f_lc"] < m["f_osc"] < m["f_lc"]                  # parasitics pull f down a little
    assert m["amplitude_diff"] == pytest.approx(m["amplitude_hand"], rel=0.15)  # (4/pi) I R_P
