"""Evaluate the built-in LC VCO and two-stage op-amp with ngspice + SKY130 and print the metrics."""
from analogforge.core.design import evaluate

vco = evaluate("lc_vco", options={"tuning_points": 5, "phase_noise": "leeson"})
print("VCO errors:", vco.errors)
for k in ("f_osc", "amplitude_diff", "power", "tuning_range_pct", "pn_1M", "fom"):
    print(f"  {k:<18} {vco.metrics[k]:.5g}")

amp = evaluate("two_stage_opamp")
for k in ("gain_db", "ugbw", "phase_margin", "power", "slew_rate"):
    print(f"  {k:<18} {amp.metrics[k]:.5g}")
print("hand estimates:", amp.extra["hand_estimates"])
