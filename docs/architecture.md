# AnalogForge — Architecture Design Document

Status: design baseline for v0.1 · Audience: developers and users of the platform
(primarily RF/analog IC designers working on oscillators: LC VCOs now, class-F
VCOs and rotary travelling-wave oscillators (RTWO) later).

---

## 1. Goals and constraints

AnalogForge is an open-source Python platform that automates the repetitive
part of analog/RF IC design work:

* describe a circuit and its design variables once (Python or YAML),
* generate netlists for **several simulators**,
* run single simulations, parameter sweeps, PVT corner sweeps and
  optimizations, in parallel,
* extract figures of merit automatically (oscillator: frequency, tuning range,
  K_VCO, phase noise, power, FoM; amplifier: gain, bandwidth, UGBW, phase/gain
  margin, slew rate, power),
* store every run in SQLite, plot results, and expose everything through a CLI
  and a FastAPI web application.

Hard constraints that shape the design:

| Constraint | Consequence |
|---|---|
| The production simulators (Cadence Spectre/Virtuoso, Keysight ADS) are only available on university machines, not in the development/cloud environment. | The core must be **simulator-agnostic**. Spectre and ADS support is built as adapters that can be unit-tested without the tools (synthetic result files, fake API modules) and are activated on the target machine. |
| Only public, legally usable device models may be used in the open-source repository. | The reference flow uses the **SkyWater SKY130 open PDK (Apache-2.0)** with ngspice. No foundry PDK data is shipped or invented. Institute PDKs are plugged in through a user-written *PDK profile* that only references local files. |
| Results must be trustworthy. | No simulated number is ever fabricated: every metric comes from a simulator run; the verification status of each backend is stated explicitly (section 13). |

Non-goals for v0.1: layout, EM simulation, schematic capture, Monte-Carlo
mismatch flows (the IR allows them later), full harmonic-balance in ngspice.

## 2. Design principles

1. **Neutral core, thin adapters.** Circuits, analyses, designs, metrics,
   flows, storage and UI never import a simulator module. Everything
   simulator-specific lives in `analogforge.backends.<name>`.
2. **Concrete numbers in the IR.** Design variables are resolved in Python
   before netlisting, so every backend receives plain numbers. (Backends that
   want symbolic design variables — the OCEAN *cellview* mode — get the
   variable map separately.)
3. **Normalized results.** Every backend converts its output into the same
   `SimResult`/`Dataset` structure with the same signal naming
   (`v(node)`, `i(source)`, `<device>:<param>`), so metric code is shared.
4. **Explicit capabilities.** A backend declares which analyses it supports;
   requesting an unsupported analysis raises `UnsupportedAnalysis` instead of
   silently degrading. Fallbacks (e.g. ISF phase noise on ngspice) are
   explicit algorithms with their own name in the results.
5. **Reproducibility.** Each stored run keeps the exact netlist text, the
   simulator version, PDK location/corner, parameters and metrics.
6. **Fail soft in flows.** A failing point in a sweep/optimization is recorded
   with its diagnosis and NaN metrics; the flow continues.

## 3. Layered architecture

```
 ┌───────────────────────────────────────────────────────────────────────┐
 │  Front-ends:   CLI (argparse)      FastAPI + web UI      Python API    │
 ├───────────────────────────────────────────────────────────────────────┤
 │  Jobs & storage: JobManager (threads, progress, cancel)  SQLite store  │
 │                  artifacts (netlists, waveforms .json.gz)  plots       │
 ├───────────────────────────────────────────────────────────────────────┤
 │  Flows:  evaluate · sweep (grid) · PVT corners · optimize              │
 │          (scipy DE / Nelder-Mead, Bayesian GP-EI) · parallel executor  │
 ├───────────────────────────────────────────────────────────────────────┤
 │  Designs: Python classes or YAML → testbenches (+ measure step)        │
 │  Metrics: waveform · amplifier · oscillator · phase noise (ISF/Leeson) │
 ├───────────────────────────────────────────────────────────────────────┤
 │  Neutral core: Circuit IR · Analyses · SimResult/Dataset · PDK profile │
 ├──────────────────────┬────────────────────────┬───────────────────────┤
 │ backends.ngspice     │ backends.spectre       │ backends.ads          │
 │ netlist → ngspice -b │ netlist (.scs)         │ netlist (ADS syntax)  │
 │ binary raw parser    │ OCEAN script (.ocn)    │ keysight.edatoolbox / │
 │ auto-fix retries     │ spectre CLI / ocean    │ keysight.ads.dataset  │
 │ (fully implemented,  │ PSF-ASCII + nutmeg     │ (adapter; runs where  │
 │  tested here)        │ parser (adapter)       │  ADS is installed)    │
 └──────────────────────┴────────────────────────┴───────────────────────┘
```

Dependency rule: arrows only point downwards; `core` depends on nothing but
numpy; backends depend on `core`; designs/metrics depend on `core`; flows
depend on designs + a backend *instance* passed in at runtime.

## 4. Circuit intermediate representation (`analogforge.core.circuit`)

A `Circuit` is a flat list of elements plus initial conditions. Nodes are
strings; `"0"` is ground. Hierarchy is not needed for v0.1 (designs are
small and generated programmatically); a `Subckt` element type is reserved.

| Element | Fields | Notes |
|---|---|---|
| `Resistor` | n1, n2, r | |
| `Capacitor` | n1, n2, c | |
| `Inductor` | n1, n2, l | lossy inductors are built as L + series R (`Circuit.lossy_inductor(L, Q, f0)`) |
| `MutualInductance` | l1, l2, k | transformers for class-F / transformer-coupled VCOs |
| `TLine` | ports, z0, td (lossless) | reserved for RTWO; ngspice `T`, Spectre `tline`, ADS `TLIN` |
| `VSource` / `ISource` | p, n, dc, ac_mag, ac_phase, waveform | waveforms: `Sine`, `Pulse`, `PWL` |
| `VCVS` / `VCCS` | p, n, cp, cn, gain | used by testbench servos |
| `Device` | name, kind, nodes, params | **PDK device** (`nmos`, `pmos`, `varactor`, ...). Rendered through the PDK profile. |
| `Raw` | text per backend | escape hatch for backend-specific lines |

All values are SI floats. Device geometry is given in metres (`w`, `l`) plus
integers (`nf`, `m`); unit conversion (e.g. SKY130's µm scale) is the PDK
profile's job.

### 4.1 Front-ends

* **Python**: subclass `Design`, build the circuit with
  `c.nmos("M1", d, g, s, b, w=..., l=...)`, `c.inductor(...)` etc.
* **YAML**: a declarative file with `parameters`, `circuit` (list of
  elements whose values are *expressions* of the parameters, e.g.
  `w: Wn*2`), `testbenches` (analyses) and `metrics` (built-in extractor
  name + signal expression, or a formula of other metrics). Expressions are
  evaluated with a restricted AST evaluator (`analogforge.expr`): numbers,
  parameter names, arithmetic, comparison, and a whitelist of math functions —
  no attribute access, no calls to arbitrary Python.

Both front-ends produce the same `Design` object; the rest of the system cannot
tell them apart.

## 5. PDK profiles (`analogforge.core.pdk`, `analogforge/pdks/*.yaml`)

A PDK profile maps abstract device kinds to concrete per-backend syntax and
says how to load models for a corner:

```yaml
name: sky130
nominal_vdd: 1.8
corners: [tt, ff, ss, sf, fs]
default_corner: tt
devices:
  nmos:
    pins: [d, g, s, b]
    defaults: {nf: 1, m: 1}
    ngspice: 'X{name} {d} {g} {s} {b} sky130_fd_pr__nfet_01v8 W={w*1e6:.6g} L={l*1e6:.6g} nf={nf} ...'
    spectre: '{name} ({d} {g} {s} {b}) nfet_01v8 w={w} l={l} nf={nf} m={m}'   # edit for your PDK
    ads:     'nfet_01v8:{name} {d} {g} {s} {b} w={w} l={l} nf={nf}'           # edit for your PDK
    op_path: {ngspice: 'm.x{name}.msky130_fd_pr__nfet_01v8'}
models:
  ngspice: {loader: sky130}        # Python hook: builds a minimal per-corner wrapper
  spectre: {include: '/path/to/models.scs', section: '{corner}'}
  ads:     {include: '/path/to/models.net'}
```

* Templates use `{expr}` / `{expr:fmt}` placeholders evaluated with the safe
  expression evaluator over the device parameters and pins.
* `models.<backend>.loader` names a Python hook for non-trivial cases. The
  SKY130 ngspice hook writes a small wrapper library containing only the
  needed SkyWater files for the corner (MOSFETs from `corners/<c>.spice`,
  R/C parameters from `sky130.lib.spice`, varactor parameters from
  `corners/<c>/nonfet.spice`) — loading takes 0.1–1.2 s instead of ~17 s.
  Files are included verbatim; nothing is edited.
* SKY130 models are located via `$ANALOGFORGE_SKY130_PATH`, the AnalogForge
  cache (`analogforge pdk install sky130` extracts them from the PyPI wheel
  `sky130`, which embeds `google/skywater-pdk-libs-sky130_fd_pr`), an
  installed `sky130` package, or an open_pdks/volare tree under `$PDK_ROOT`.
* `pdks/template_institute.yaml` is a commented template for an institute
  PDK used with Spectre/ADS (cell names, include paths are placeholders the
  user fills in; nothing proprietary is shipped).

## 6. Analyses (`analogforge.core.analyses`) and capability matrix

| Analysis | Fields | ngspice | Spectre | ADS |
|---|---|---|---|---|
| `Op` | – | `op` | `dc` (oppoint) | `DC` |
| `DCSweep` | source, start, stop, step | `dc` | `dc dev= param=dc` | `DC` + `SweepPlan` |
| `AC` | fstart, fstop, points/decade | `ac dec` | `ac dec=` | `AC` + `SweepPlan` |
| `Tran` | tstop, tstep, tmax, uic | `tran` | `tran` (`skipdc` for uic) | `Tran` |
| `Noise` | output pair, input source, f-range | `noise` | `noise` | `AC CalcNoise=yes` |
| `PSS` | fund (guess), harmonics, tstab, osc nodes | `pss` (autonomous, shooting) | `pss (p n)` autonomous | `HB` + `OscPort` |
| `PNoise` | f-offset range, osc nodes | **not available** → ISF/Leeson estimate | `pnoise` | `HB` phase noise |

The netlister of each backend translates an analysis or raises
`UnsupportedAnalysis`. Designs ask `backend.supports("pnoise")` to choose the
phase-noise method.

## 7. Backend interface (`analogforge.backends.base`)

```python
class Backend(ABC):
    name: str
    capabilities: frozenset[str]
    def available(self) -> tuple[bool, str]          # executable / API present?
    def version(self) -> str | None
    def netlist(self, tb: Testbench, pdk: PDK) -> str # full input deck
    def write_inputs(self, tb, pdk, workdir) -> list[Path]   # deck (+ OCEAN script, ...)
    def run(self, tb, pdk) -> SimResult             # simulate + parse + normalize
```

`Testbench` = `Circuit` + list of analyses + corner + temperature + options +
list of device operating-point parameters to save.

### 7.1 ngspice (reference backend, fully implemented)

* One `ngspice -n -b` process per testbench; `.control` block runs each
  analysis and `write`s one binary raw file per analysis; the plot name in the
  file is checked so a failed analysis cannot masquerade as the previous one.
* Device OP parameters are written with the OP raw file (`@m.xm1.msky130...[gm]`).
* Failures are classified from the log (singular matrix, DC non-convergence,
  timestep too small, missing model/subckt/include, syntax) and — only for
  convergence classes — retried with escalating aids (gmin/source stepping +
  iteration limits, `rshunt`, Gear integration, finally relaxed tolerances,
  flagged as *reduced accuracy*). All attempts are recorded.
* A process-wide semaphore limits concurrent ngspice processes to the CPU
  count, so nested parallelism (sweep points × testbenches) cannot oversubscribe.

### 7.2 Cadence Spectre (adapter)

Two run modes, both producing the same `SimResult`:

1. **direct** — `spectre input.scs +escchars -format psfascii -raw <dir>`;
   the PSF-ASCII files (`*.dc`, `*.ac`, `*.tran.tran`, `*.pss`, `*.pnoise`, ...)
   are parsed by `backends/spectre/psf.py`. `-format nutascii` is also
   supported and parsed by the common raw-file reader. Needs only `spectre`
   on `PATH` (no Virtuoso).
2. **ocean** — for designs drawn in Virtuoso: an OCEAN script is generated
   that selects the cellview (`design(lib cell view)`) or a netlist, sets
   `modelFile`, `desVar`s, `temp`, analyses (`tran`, `ac`, `pss`, `pnoise`),
   runs, and writes scalar measurements (`frequency`, `phaseMargin`,
   `bandwidth`, phase noise at offsets, ...) and selected waveforms
   (`ocnPrint`) to text files that the adapter parses. This lets AnalogForge's
   sweeps/corners/optimizers drive an existing ADE testbench.

### 7.3 Keysight ADS (adapter)

* Netlist generation in ADS netlist syntax (`R:R1 a b R=...`, `Tran:Tran1 ...`,
  `HB` + `OscPort` for oscillators). The syntax table lives in one module so it
  can be corrected quickly against the installed ADS version.
* Execution uses the Python API shipped with ADS (≥ 2024):
  `keysight.edatoolbox.ads.CircuitSimulator().run_netlist(...)`, results read
  with `keysight.ads.dataset` and converted to `Dataset`s. Alternatively the
  netlist can be produced by ADS itself from a schematic
  (`keysight.ads.de`), with AnalogForge substituting variable values.
* All API calls are isolated in `backends/ads/api.py`, imported lazily; the
  adapter is unit-tested with a fake `keysight` package.

## 8. Results model (`analogforge.core.results`)

```python
SimResult(ok, datasets: dict[str, Dataset], log, diagnoses, attempts,
          fixes_applied, reduced_accuracy, netlist, elapsed_s, backend, version)
Dataset(kind, sweep_name, sweep: ndarray, signals: dict[str, ndarray])
```

Signal naming (lower case): `v(node)` node voltage, `i(source)` source
current (**positive into the + terminal**, SPICE convention), `<dev>:<param>`
device OP parameter (`m1:gm`). `Dataset.eval("v(outp)-v(outn)")` evaluates a
signal expression. Complex arrays for AC/noise/PSS spectra.

## 9. Designs, testbenches and measurements (`analogforge.core.design`)

```python
class Design:
    name, title, kind ('oscillator' | 'amplifier' | 'generic')
    parameters: list[ParamSpec]           # name, default, min, max, unit, log
    metrics: list[MetricSpec]             # name, unit, goal ('max'/'min')
    def testbenches(self, p, ctx) -> list[Testbench]
    def measure(self, p, results, ctx) -> Measurement   # metrics + waveforms
```

`ctx` (`DesignContext`) carries the PDK, corner, temperature, supply scaling,
options (e.g. `phase_noise="isf"`) and a `simulate(testbenches)` callback.
The callback lets a measurement run follow-up simulations — e.g. the ISF
phase-noise procedure first needs the steady-state period, then launches
impulse-injection transients; the tuning-range measurement launches one
oscillator run per tuning voltage. The callback goes through the same backend
and parallel executor.

`evaluate(design, params, backend, pdk, corner, temperature, vdd_scale,
options) -> EvaluationResult` is the single entry point used by all flows.

## 10. Metric library (`analogforge.metrics`)

* `waveform`: crossings, interpolation, resampling, FFT spectrum, THD.
* `amplifier`: DC gain, −3 dB bandwidth, unity-gain frequency, phase margin
  (phase referenced to its low-frequency value), gain margin, slew rate,
  settling time, output swing.
* `oscillator`: frequency from interpolated zero crossings of the
  differential output in the steady-state window, FFT cross-check, amplitude,
  start-up time, tuning range `2(fmax−fmin)/(fmax+fmin)`, K_VCO, FoM
  `20log10(f0/Δf) − L(Δf) − 10log10(P_DC/1 mW)`, FoM_T `= FoM + 20log10(TR%/10)`.
* `phasenoise`:
  * **ISF method (Hajimiri–Lee, simulation based, ngspice):** inject a small
    charge impulse into a tank node at K phases of the steady-state period,
    measure the resulting phase shift after several cycles against an
    unperturbed reference, obtain Γ(φ) = Δφ·q_max/Δq; combine Γ_rms with the
    tank-loss noise current 4kT/R_p and the transistor channel-noise estimate
    (4kTγ·g_m, with the large-signal g_m modulation approximated from the
    simulated drain current) to give L(Δf) in the 1/f² region.
  * **Leeson model** with the simulated tank Q, amplitude and power (noise
    factor F as a parameter) — a quick sanity estimate.
  * **Simulator-native** Spectre `pnoise` / ADS HB phase noise when those
    backends are used.
  Each result carries the method name; ngspice estimates are labelled as
  estimates (section 13).

## 11. Flows (`analogforge.flows`)

* **Executor**: `parallel_map(fn, items, workers, progress, cancel)` on a
  thread pool (simulators are subprocesses, so threads suffice); progress
  callback and cooperative cancellation.
* **Sweep**: grid (cartesian) or zipped lists over any design parameters,
  also over corner/temperature/supply.
* **PVT corners**: cartesian product of process corners × supply scale ×
  temperature; summary with worst case per metric.
* **Optimize**: single scalar objective (`max`/`min` of a metric) with
  inequality constraints on metrics handled by a smooth penalty; methods:
  `de` (scipy differential evolution, parallel population evaluation),
  `nelder-mead` (scipy), `bayes` (Gaussian process with Matérn-5/2 kernel and
  expected improvement, implemented with numpy/scipy; batch of initial Latin
  hypercube points evaluated in parallel), `random` (baseline). Log-scaled
  variables are optimized in log space. History and best point are stored.

## 12. Storage, plots, jobs, web

**SQLite** (`<data dir>/analogforge.db`, default `~/.analogforge`,
override `ANALOGFORGE_DATA`):

```sql
jobs(id TEXT PRIMARY KEY, kind TEXT, name TEXT, status TEXT, progress REAL,
     total INTEGER, done INTEGER, created REAL, started REAL, finished REAL,
     config TEXT, summary TEXT, error TEXT)
runs(id INTEGER PRIMARY KEY, job_id TEXT, idx INTEGER, design TEXT,
     backend TEXT, pdk TEXT, corner TEXT, temperature REAL, vdd_scale REAL,
     params TEXT, metrics TEXT, ok INTEGER, errors TEXT, warnings TEXT,
     elapsed REAL, created REAL, artifact TEXT)
```
JSON columns hold dicts; waveforms and netlists go to
`artifacts/<job>/<run>.json.gz` (keeps the DB small). WAL mode for concurrent
readers (web) and a single writer lock.

**Plots**: `plots.py` renders matplotlib PNGs (Bode, waveforms, spectra, sweep
curves, PVT tables, optimization convergence, phase noise) for CLI reports;
the web UI draws Plotly charts client-side from JSON (Plotly is served from
the installed `plotly` package, no CDN).

**JobManager**: background thread pool; each job runs a flow, updates
`progress/done/total` in SQLite, supports cancel, survives page reloads
(state is in the DB).

**FastAPI** endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/info` | versions, backends (availability), PDKs |
| GET | `/api/designs`, `/api/designs/{name}` | design catalogue, parameters, metrics |
| POST | `/api/netlist` | netlist for any backend (Spectre/ADS export from the UI) |
| POST | `/api/jobs` | submit `simulate` / `sweep` / `corners` / `optimize` |
| GET | `/api/jobs`, `/api/jobs/{id}` | list / status + progress + summary |
| POST | `/api/jobs/{id}/cancel` | cancel |
| GET | `/api/jobs/{id}/runs` | runs of a job |
| GET | `/api/runs/{id}` | run details incl. waveforms |
| GET | `/api/compare?ids=1,2` | side-by-side metrics + overlay data |

Front-end: one static page (vanilla JS + Plotly) with tabs *New job*,
*Jobs* (live progress), *Results* (tables + charts), *Compare*,
*Netlist export*.

**CLI** (`analogforge`): `doctor`, `pdk install|info`, `designs`, `netlist`,
`simulate`, `sweep`, `corners`, `optimize`, `jobs`, `runs`, `report`,
`ocean`, `serve`. Flow parameters can also be given as a YAML job file.

## 13. Verification status (kept up to date in README)

| Component | Status |
|---|---|
| ngspice + SKY130 backend, all flows, web, CLI | implemented and covered by pytest (runs real ngspice) |
| Spectre netlist + OCEAN generation | implemented; syntax unit-tested; **not executed** (no Spectre in dev env) |
| PSF-ASCII / nutmeg parsing | implemented; tested on synthetic files following the documented format |
| ADS netlist + Python-API adapter | implemented; tested against a fake API; **not executed** (no ADS in dev env) |
| Phase noise on ngspice | ISF/Leeson **estimates**; compare with Spectre `pnoise` / ADS HB before relying on absolute numbers |

## 14. Error handling and diagnostics

* `BackendUnavailable` (tool/API missing) → reported by `analogforge doctor`
  and the UI, flows refuse to start.
* Per-run failures → `EvaluationResult.errors` with the diagnosis text; NaN
  metrics; flows continue; the optimizer treats them as infeasible.
* Automatic retries are visible as warnings (`fixes_applied`).

## 15. Testing strategy

* Unit tests (no simulator): units, expressions, IR validation, netlisters
  (ngspice/Spectre/ADS golden text fragments), raw/PSF parsers, metrics on
  analytic waveforms (sine, single-pole/two-pole transfer functions with known
  margins), optimizer on analytic functions, SQLite store, YAML loader,
  ADS adapter with a fake `keysight` module.
* Integration tests (ngspice + SKY130, auto-skipped with a clear reason if
  unavailable): LC VCO oscillates and transient frequency agrees with PSS
  and with 1/(2π√(LC_tot)); tuning curve monotonic; ISF phase noise in a
  plausible range and scaling correctly with tank Q; op-amp gain/UGBW/PM
  extracted and consistent with small-signal estimates; sweep/corners/optimize
  flows; job manager + SQLite; FastAPI end-to-end (submit → progress → result
  → compare).

## 16. Extensibility (roadmap for class-F and RTWO)

* **Class-F VCO** (transformer-based, e.g. Babaie/Staszewski): needs
  `MutualInductance` (in the IR), a second resonance check (measure the
  3rd-harmonic content with `metrics.waveform.spectrum`), and PSS/pnoise on
  the commercial backends. A design module can reuse the LC-VCO measurement
  code unchanged.
* **RTWO**: needs distributed transmission lines (`TLine` IR element,
  rendered as ngspice `T`, Spectre `tline`, ADS `TLIN`/`CPW` via PDK profile)
  and multi-phase outputs (metrics: phase spacing between taps). The flow,
  storage and UI layers need no change.
* New backends (e.g. Xyce) implement the `Backend` interface only.

## 17. Repository layout

```
analogforge/
  docs/                 architecture.md, yaml_format.md, phase_noise.md,
                        cadence_spectre.md, keysight_ads.md, limitations.md
  src/analogforge/
    core/               circuit.py analyses.py results.py design.py pdk.py yaml_design.py
    pdks/               sky130.yaml sky130.py template_institute.yaml
    backends/           base.py ngspice/ spectre/ ads/
    metrics/            waveform.py amplifier.py oscillator.py phasenoise.py
    designs/            lc_vco.py two_stage_opamp.py yaml/*.yaml (registry also loads user YAML)
    flows/              executor.py sweep.py corners.py optimize.py
    store.py plots.py jobs.py cli.py expr.py units.py
    web/                app.py static/
  examples/             jobs/*.yaml  python/*.py  cadence/*.yaml
  tests/
  scripts/              spectre_selftest.sh  ads_selftest.py  package.sh
```

## 18. Implementation notes (v0.1, after building against the design)

Deviations from / additions to the baseline above, decided during implementation:

* **Testbench fields** — besides circuit/analyses/corner/temperature/options/save_op,
  a `Testbench` carries `variables` (the design point; used as `desVar`s in OCEAN
  cellview mode and as ADS `VAR` substitutions) and `probes` (signals a measurement
  needs; OCEAN mode exports exactly these with `ocnPrint`).  `Tran.save` records
  device parameters versus time (e.g. `M1:gm` for cyclostationary noise).
* **Backend options in jobs** — `backend_options` (CLI `--backend-opt KEY=VALUE`)
  instantiate a configured backend, e.g. `{mode: ocean, cellview: Lib/cell/view}`.
* **Phase-noise options** of the LC VCO: `leeson` (default), `isf`, `pnoise`
  (native; falls back to Leeson with a warning where unsupported), `none`.
  Unrequested metrics (PSS frequency, tuning, phase noise) are omitted instead of
  being reported as NaN.
* **ngspice process environment** — `OMP_NUM_THREADS=1`, `OMP_WAIT_POLICY=PASSIVE`.
  libgomp's default busy-waiting made concurrent ngspice processes ~17x slower.
* **SKY130 wrapper** — written atomically through a unique temporary file
  (parallel testbenches create it concurrently).  The installer extracts
  `models/` completely (transitive includes such as `sky130_fd_pr__model__r+c.model.spice`)
  plus the used cells.
* **VCO sizing** — the SKY130 varactor model has very low Q at GHz for wide/long
  fingers; the default uses 2 µm x 0.18 µm fingers (Q ~ 25 at 3 GHz).
* **Bayesian optimization** — an exact (L1) penalty on relative constraint
  violations, outlier clipping (75th percentile) before the GP fit, EI candidates
  around the incumbent, and the current design as an initial point were needed
  for reliable progress when the feasible region is small (op-amp power limit).
* **Spectre adapter tests** use a fake `spectre` executable that writes PSF-ASCII
  files, so the complete run path (netlist -> subprocess -> PSF parsing ->
  normalized datasets) is exercised without Cadence software.
