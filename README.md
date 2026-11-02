# AnalogForge

**Simulator-agnostic analog/RF IC design automation in Python** — describe a
circuit once (Python or YAML), simulate it with **ngspice + the open SkyWater
SKY130 PDK** today and with **Cadence Spectre (OCEAN)** or **Keysight ADS**
on the institute machines, extract oscillator and amplifier figures of merit,
run sweeps, PVT corners and optimizations in parallel, keep every result in
SQLite and inspect/compare everything in a web UI.

Built with LC-VCO work in mind (class-F VCOs and rotary travelling-wave
oscillators are on the roadmap): oscillation frequency (three independent
estimates), tuning range and K_VCO, phase noise (ISF method or Leeson; native
`pnoise`/HB on the commercial simulators), power, FoM/FoM_T — and gain,
bandwidth, UGBW, phase/gain margin, slew rate, PSRR for amplifiers.

| | |
|---|---|
| Reference backend | ngspice (tested with ngspice-42) + SKY130 (Apache-2.0 models, installed from PyPI) — **fully implemented and tested** |
| Cadence backend | Spectre netlists, OCEAN scripts (netlist or Virtuoso-cellview mode), PSF-ASCII/nutmeg parsing — **adapter, not yet run on a real Spectre** |
| ADS backend | ADS netlists + Keysight ADS Python API (`keysight.edatoolbox`, `keysight.ads.dataset`) — **adapter, not yet run in a real ADS** |
| Interfaces | CLI, FastAPI REST API + web UI, Python API, YAML jobs |

See [`docs/limitations.md`](docs/limitations.md) for exactly what is and is not verified.

## Contents

* [Installation](#installation) · [Quick start](#quick-start) · [Web UI](#web-ui)
* [Describing circuits](#describing-circuits) · [Flows](#sweeps-pvt-and-optimization) · [Results](#results-database-and-reports)
* [Cadence Spectre](#cadence-spectre--virtuoso) · [Keysight ADS](#keysight-ads) · [Architecture](#architecture) · [Testing](#testing)

## Installation

Requirements: Python ≥ 3.10, ngspice, ~25 MB for the SKY130 model subset.

```bash
# 1. ngspice
sudo apt install ngspice            # Debian/Ubuntu;  macOS: brew install ngspice;  conda: conda install -c conda-forge ngspice

# 2. AnalogForge (from the repository directory)
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# 3. SKY130 device models (downloads the PyPI wheel `sky130`, extracts the
#    unmodified Apache-2.0 SkyWater sky130_fd_pr files into ~/.cache/analogforge)
analogforge pdk install sky130
#    offline:  analogforge pdk install sky130 --wheel sky130-1.0.0-py3-none-any.whl
#    existing open_pdks/volare install: export PDK_ROOT=...  (or ANALOGFORGE_SKY130_PATH)

# 4. Check everything
analogforge doctor --full
```

`doctor` checks Python packages, runs an RC smoke test in ngspice (f-3dB
against 1/2πRC), finds the SKY130 models, simulates the LC VCO, and reports
whether Spectre/ADS are reachable.

## Quick start

```bash
analogforge designs                                   # catalogue (built-in + YAML)
analogforge designs lc_vco                            # parameters, metrics, options

# LC VCO: frequency, amplitude, power, 5-point tuning curve, Leeson phase noise, FoM
analogforge simulate lc_vco

# simulation-based phase noise (ISF, 48 extra transients) + ngspice PSS cross-check
analogforge simulate lc_vco -o phase_noise=isf -o pss=true --report out/vco

# two-stage op-amp: gain, UGBW, PM, GM, slew rate, settling, PSRR, power
analogforge simulate two_stage_opamp -p CC=1.5p

# netlists for any backend (Spectre also writes an OCEAN script)
analogforge netlist lc_vco --backend spectre --out spectre_in/
```

Example output (default VCO, from [`docs/validation.md`](docs/validation.md)):

```
f_osc 3.370 GHz (FFT 3.371 GHz, ngspice PSS 3.372 GHz)   amplitude_diff 0.890 V   power 4.36 mW
tuning 3.364 - 3.803 GHz (12.2 %)   L(1 MHz) = -118.9 dBc/Hz (ISF) / -119.5 dBc/Hz (Leeson)   FoM 183 dB
```

## Web UI

```bash
analogforge serve            # http://127.0.0.1:8000
```

* **New job** — pick a design, backend, PDK, corner, temperature; edit
  parameters; choose *simulate*, *sweep* (sweep spec per parameter, e.g.
  `1m:4m:7`), *PVT corners* or *optimize* (tick variables, objective,
  constraints, method, budget).
* **Jobs** — live progress bars; per job: charts (metric vs. sweep variable,
  PVT bar charts, optimization convergence), worst-case table, run table.
  Per run: metrics, parameters, waveforms, spectrum, tuning curve, phase noise,
  ISF, Bode plots and the exact netlists.
* **Compare** — tick runs anywhere; side-by-side metrics with % differences and
  overlaid curves.
* **Netlist export** — ngspice / Spectre (+OCEAN) / ADS decks for download.

REST endpoints: `GET /api/info`, `GET /api/designs[/{name}]`, `POST /api/netlist`,
`POST /api/jobs`, `GET /api/jobs[/{id}]`, `POST /api/jobs/{id}/cancel`,
`DELETE /api/jobs/{id}`, `GET /api/runs/{id}`, `GET /api/compare?ids=1,2`,
`GET /api/report/{job}`; interactive docs at `/docs`.  Plotly is served from the
installed Python package — no internet needed.

## Describing circuits

**Python** (see `src/analogforge/designs/lc_vco.py`):

```python
from analogforge.core.circuit import Circuit
c = Circuit("tank")
c.vsource("VDD", "vdd", dc=1.8)
c.inductor("LP", "vdd", "outp", 2e-9)
c.varactor("VP", "outp", "vtune", w=2e-6, l=0.18e-6, m=120)   # PDK device kinds: nmos, pmos, varactor
c.nmos("M1", "outp", "outn", "tail", "0", w=20e-6, l=0.15e-6)
```

Designs subclass `analogforge.core.design.Design` (`parameters`, `metrics`,
`testbenches()`, `measure()`); `measure()` may launch follow-up simulations
(the ISF procedure and the tuning sweep do).

**YAML** (see `examples/designs/` and [`docs/yaml_format.md`](docs/yaml_format.md)):

```yaml
name: rc_lowpass
parameters: {R: {default: 1k, min: 100, max: 100k, log: true}, C: 1p}
circuit:
  - {type: vsource, name: VIN, p: in, n: 0, ac: 1}
  - {type: resistor, name: R1, nodes: [in, out], value: R}
  - {type: capacitor, name: C1, nodes: [out, 0], value: C}
testbenches: [{name: main, analyses: [{type: ac, name: ac, fstart: 1k, fstop: 100g}]}]
metrics:
  - {name: f3db, extractor: bandwidth_3db, analysis: ac, signal: "v(out)"}
  - {name: f3db_expected, expr: "1/(2*pi*R*C)"}
```

**Python API**:

```python
from analogforge.core.design import evaluate
r = evaluate("lc_vco", {"IBIAS": "3m"}, backend="ngspice", pdk="sky130", corner="ss", temperature=125,
             options={"phase_noise": "isf"})
print(r.metrics["f_osc"], r.metrics["pn_1M"], r.extra["phase_noise_method"])
```

More in `examples/python/`.

## Sweeps, PVT and optimization

```bash
analogforge sweep lc_vco --var VTUNE=0:1.8:7 --var corner=tt,ss -o tuning_points=0
analogforge corners lc_vco --process tt,ff,ss,sf,fs --voltage 0.9,1,1.1 --temperature -40,27,125
analogforge optimize two_stage_opamp --objective max:ugbw --var W1 --var W6 --var CC --var IBIAS \
    --constraint "phase_margin >= 60" --constraint "gain_db >= 60" --constraint "power <= 0.4m" \
    --method bayes --budget 30
analogforge run examples/jobs/vco_optimize.yaml          # any job as a YAML file
```

* points run in parallel (thread pool; ngspice processes limited to the CPU count);
* optimizers: `bayes` (GP with Matérn-5/2 ARD kernel + expected improvement,
  batched, implemented with numpy/scipy), `de` (scipy differential evolution),
  `nelder-mead`, `random`; constraints via an exact penalty; the current design
  is always evaluated first;
* PVT summaries report min/max and the worst corner per metric.

## Results database and reports

Every run is stored in SQLite (`~/.analogforge/analogforge.db`, override with
`ANALOGFORGE_DATA` or `--data`): job config, parameters, corner/T/VDD, metrics,
errors/warnings, timing; curves, netlists and simulator logs go to compressed
artifacts next to it.

```bash
analogforge jobs                 # list
analogforge runs <job-id>        # runs of a job
analogforge report <job-id>      # Markdown + matplotlib PNG report
```

## Cadence Spectre / Virtuoso

Full guide: [`docs/cadence_spectre.md`](docs/cadence_spectre.md).  In short:

1. Source the Cadence environment, install AnalogForge (`pip install -e .`), run
   `python scripts/spectre_selftest.py [--ocean]`.
2. Copy `src/analogforge/pdks/template_institute.yaml` to
   `~/.analogforge/pdks/mypdk.yaml` and fill in model file/sections and device
   instance templates of your PDK.
3. `analogforge simulate lc_vco --backend spectre --pdk mypdk` (direct Spectre,
   PSF-ASCII results), or drive an **existing ADE testbench** through OCEAN:
   `--backend-opt mode=ocean --backend-opt cellview=MyLib/vco_tb/schematic`
   with a YAML file listing its design variables
   (`examples/designs/virtuoso_vco_tb.yaml`).
4. `-o phase_noise=pnoise` uses Spectre `pss`+`pnoise` instead of the ngspice estimate.

## Keysight ADS

Full guide: [`docs/keysight_ads.md`](docs/keysight_ads.md).  In short: install
AnalogForge into the Python that ships with ADS 2024+
(`$HPEESOF_DIR/tools/python/bin/python3 -m pip install --user -e .`), run
`scripts/ads_selftest.py`, then either let ADS netlist your schematic and have
AnalogForge substitute the `VAR` values
(`get_backend("ads", workspace=..., cellview=(lib, cell, "schematic"))`), or
simulate AnalogForge-generated netlists with an ADS section in your PDK profile.

## Architecture

Design document: [`docs/architecture.md`](docs/architecture.md).

```
CLI · FastAPI/web UI · Python API
   └─ jobs (threads, progress, cancel) · SQLite store · plots/reports
       └─ flows: evaluate · sweep · PVT · optimize · ISF phase noise · parallel executor
           └─ designs (Python / YAML) · metric library (waveform, amplifier, oscillator, phase noise)
               └─ neutral core: circuit IR · analyses · results · PDK profiles
                   ├─ backends.ngspice   (reference, tested)
                   ├─ backends.spectre   (netlist, OCEAN, PSF parser)
                   └─ backends.ads       (netlist, Keysight Python API)
```

Repository layout:

```
docs/                architecture, YAML format, phase noise, Cadence, ADS, limitations, validation
src/analogforge/     core/ backends/ pdks/ designs/ metrics/ flows/ web/ store.py plots.py jobs.py cli.py
examples/            designs/*.yaml  jobs/*.yaml  python/*.py
scripts/             validate.py  spectre_selftest.py  ads_selftest.py  package.sh
tests/               pytest suite (simulation tests use real ngspice + SKY130)
```

## Testing

```bash
pytest -q                     # ~100 tests, ~2 min on 4 cores; installs SKY130 on first use if missing
python scripts/validate.py    # regenerates docs/validation.md from real simulations
```

The suite covers units/expressions, IR and PDK profiles, the raw-file reader,
every metric on analytic waveforms with known answers (single/two/three-pole
margins, THD, ISF = Leeson identity, ...), the ngspice backend (RC against
analytic results incl. thermal noise, diagnostics, automatic retry), the
Spectre adapter (netlist/OCEAN syntax, PSF parsing, a fake `spectre`
executable), the ADS adapter (fake Keysight API), the designs (VCO oscillation,
FFT/PSS agreement, monotonic tuning, Γ_rms ≈ 1/√2, ISF vs Leeson, op-amp vs
small-signal hand analysis, saturation of all devices), flows, store, job
manager (incl. cancellation), REST API and CLI.

## License

AnalogForge is MIT-licensed (`LICENSE`).  The SkyWater SKY130 device models
are © SkyWater Technology / Google and licensed under Apache-2.0; they are not
part of this repository — `analogforge pdk install sky130` downloads them
unmodified (a provenance file with the wheel's SHA-256 is written next to them).
