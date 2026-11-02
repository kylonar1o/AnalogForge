"""Simulation-based phase noise of the LC VCO (impulse sensitivity function) and plots."""
from pathlib import Path

from analogforge.core.design import evaluate
from analogforge.plots import plot_curves

r = evaluate("lc_vco", options={"phase_noise": "isf", "isf_points": 16, "tuning_points": 7, "pss": True})
m, ex = r.metrics, r.extra
print(f"f_osc = {m['f_osc'] / 1e9:.4f} GHz  (PSS: {m['f_pss'] / 1e9:.4f} GHz)")
print(f"L(1 MHz) = {m['pn_1M']:.1f} dBc/Hz   FoM = {m['fom']:.1f} dB   method: {ex['phase_noise_method']}")
print("Gamma_rms:", ex["isf_gamma_rms"], " noise split:", ex["phase_noise"]["contributions"])
for p in plot_curves(r, Path("out_vco_isf")):
    print("wrote", p)
