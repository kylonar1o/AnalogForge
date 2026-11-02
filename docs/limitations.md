# Limitations and verification status

This page lists honestly what is verified, what is not, and what was found
during development.  Numbers quoted elsewhere come from `docs/validation.md`,
which `scripts/validate.py` regenerates from real simulations.

## Verification status

| Component | Status |
|---|---|
| ngspice backend (netlisting, runner, raw parser, diagnostics, auto-retry) | implemented; tested with real ngspice-42 |
| SKY130 integration (nfet/pfet_01v8, cap_var_lvt; corners tt/ff/ss/sf/fs) | implemented; tested; models obtained from the PyPI wheel `sky130==1.0.0` (Apache-2.0 SkyWater files, unmodified) |
| LC VCO / two-stage op-amp / YAML designs | tested with ngspice + SKY130 (see validation.md) |
| Sweeps, PVT, optimization, SQLite, reports, job manager, CLI, web UI | tested (pytest + a headless-browser walk-through of the UI) |
| Spectre netlists, OCEAN scripts | generated and checked structurally by unit tests; **never executed** on Spectre |
| PSF-ASCII / nutmeg parsing for Spectre | tested on synthetic files written to the documented format and through a fake `spectre` executable |
| ADS netlists and Python-API adapter | tested against a fake `keysight` package only; **never executed** in ADS |
| open_pdks/volare SKY130 installations (`PDK_ROOT`) | supported by the locator (uses `.lib <corner>`), not tested here (GitHub was not reachable from the development environment) |

## Known limitations

* **Phase noise on ngspice is an estimate** (ISF or Leeson), 1/f² region only;
  flicker up-conversion, tail-current, varactor and bias-network noise are not
  included.  See `phase_noise.md`.
* **ngspice PSS** works for the LC VCO (agrees with the transient frequency to
  0.05 %) but is slow (~40 s per run with the SKY130 varactor); it is therefore
  opt-in (`-o pss=true`).
* **SKY130 has no official Spectre/ADS models.**  The `sky130` profile's
  Spectre/ADS entries only demonstrate syntax; on the school machines use an
  institute PDK profile.
* **Tank inductors are ideal L with a parallel loss resistor** derived from a
  user-given Q at a reference frequency; SKY130's open models contain no
  validated RF inductor models.
* **SKY130 varactor Q is low at GHz frequencies in the model**: `cap_var_lvt`
  with W=10 µm/L=0.5 µm fingers has Q≈1.5–3 at 4 GHz in accumulation (the first
  VCO sizing failed to start up for that reason); short, narrow fingers
  (2 µm × 0.18 µm, the default now) give Q≈25.  Tank Q in the VCO is therefore
  dominated by the inductor + varactor model, not by an RF-characterised device.
* **Tuning curve shape**: the varactor gate sits at the tank DC level (VDD), so
  most tuning happens for VTUNE between ~1.0 V and 1.8 V (K_VCO is non-uniform);
  AC-coupled varactors with a separate bias are a possible extension.
* **Optimization** is single-objective with penalty constraints; budgets of a
  few dozen evaluations find improvements but not guaranteed optima.  The
  current design is always included as a starting point.
* **Monte-Carlo mismatch** is not implemented (the SKY130 files support it via
  `mc_mm_switch`; the wrapper sets it to 0).
* **Hierarchical netlists / subcircuits** are not part of the neutral IR yet
  (designs are flat); a `Raw` element allows backend-specific lines.
* **Class-F VCO and RTWO** are not provided as designs yet.  The IR already has
  `MutualInductance` and `TLine` elements and the ISF/oscillator metrics are
  topology-independent, see the roadmap in `architecture.md`.
* The web server has no authentication; run it on localhost or behind an SSH tunnel.

## Performance notes found during development

* ngspice is built with OpenMP; with the default libgomp *active* wait policy,
  several ngspice processes running in parallel slowed each other down by
  ~17× (4 parallel VCO transients: 40 s instead of 2.3 s).  AnalogForge
  starts ngspice with `OMP_NUM_THREADS=1` and `OMP_WAIT_POLICY=PASSIVE`
  (override with `ANALOGFORGE_OMP_THREADS`).
* Loading the full SKY130 library takes ~17 s per ngspice run; the per-corner
  minimal wrapper takes 0.1 s (MOSFETs only) or ~1.2 s (with varactor).
