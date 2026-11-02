# Connecting AnalogForge to Keysight ADS (Python API)

Status: **adapter** — netlist generation, variable substitution and dataset
conversion are unit-tested against a fake `keysight` module, but not against a
real ADS installation (none was available while developing).  Run the
self-test in step 2 first.

## 1. What the adapter does

ADS 2024 and later ship a Python environment with Keysight's packages:

* `keysight.edatoolbox.ads.CircuitSimulator().run_netlist(...)` — runs the ADS
  circuit simulator on a netlist and writes a dataset (`.ds`);
* `keysight.ads.dataset.open(path)` — reads datasets; blocks convert to pandas
  via `to_dataframe()`;
* `keysight.ads.de` — workspace/design access, e.g. `design.generate_netlist()`.

All calls are isolated in `analogforge/backends/ads/api.py`.  Two ways of use:

| Mode | How | Recommended for |
|---|---|---|
| **schematic** | `ADSBackend(workspace=..., cellview=(lib, cell, "schematic"))`: ADS netlists your schematic, AnalogForge substitutes the `VAR` values (design parameters) and runs it | real work with your institute's ADS PDK, EM models, OscPort setup |
| **generated** | `ADSBackend()`: AnalogForge renders its own ADS netlist from the neutral circuit (needs an ADS section in the PDK profile) | quick experiments, the built-in designs |

Results are mapped to AnalogForge's normalized names: dataset column `vout` →
`v(vout)`, `VDD.i` → `i(vdd)`; blocks are matched to analyses by controller
name (`AF_<analysis>`) or analysis type.

## 2. Installation into the ADS Python

```bash
# locate the interpreter (Linux example; on Windows: <ADS>\tools\python\python.exe)
export ADSPY=$HPEESOF_DIR/tools/python/bin/python3
$ADSPY -c "import keysight.edatoolbox, keysight.ads.dataset; print('ok')"

# install AnalogForge for this interpreter (user site); numpy/scipy/matplotlib are
# usually already present in the ADS Python - add --no-deps if pip cannot reach PyPI
tar xzf analogforge-0.1.0-repo.tar.gz && cd analogforge-0.1.0
$ADSPY -m pip install --user -e .
$ADSPY -m analogforge doctor        # 'backend ads  OK' expected
```

Alternatively keep AnalogForge in your normal Python and add ADS's
`site-packages` to `PYTHONPATH` (only if the Python versions match).

### Self-test

```bash
$ADSPY scripts/ads_selftest.py
```

It prints the generated netlist, the dataset block/column names and checks
f-3dB and the transient step of an RC network.  If the simulator rejects the
netlist, compare it with a netlist ADS writes for the same RC schematic
(*Simulate → Generate Netlist*) and adjust the syntax table in
`backends/ads/netlist.py` (all component/controller syntax is in that file).
If the dataset names differ, adjust `backends/ads/api.py:read_dataset` or
`blocks_to_datasets`.

## 3. Schematic-driven runs (recommended)

1. Build the testbench in ADS as usual (PDK devices, OscPort for oscillators,
   HB/Tran/AC controllers).  Put every quantity you want to vary into a `VAR`
   block (e.g. `Ibias`, `Vtune`, `Wn`).
2. Describe the parameters and metrics in a small YAML design with an empty
   circuit (like `examples/cadence/virtuoso_vco_tb.yaml`), with analysis names
   that match your controllers' dataset blocks.
3. Run from Python (inside the ADS Python):

```python
from analogforge.backends.base import get_backend
from analogforge.flows.sweep import run_sweep
be = get_backend("ads", workspace="/home/me/ads/VCO_wrk", cellview=("VCO_lib", "vco_tb", "schematic"))
sw = run_sweep("my_ads_vco.yaml", {"Vtune": {"start": 0, "stop": 1.2, "num": 7}}, backend=be, pdk="mypdk")
for row in sw.table(["f_osc", "power"]):
    print(row)
```

## 4. Generated netlists

Add an `ads:` template to each device kind in your PDK profile, e.g.

```yaml
devices:
  nmos:
    ads: 'nmos_rf:{name} {d} {g} {s} {b} w={w*1e6} um l={l*1e6} um nf={nf:.0f}'
models:
  ads:
    include: '/path/to/pdk/ads/model_include.net'
```

then `analogforge netlist lc_vco --backend ads --pdk mypdk` shows the netlist
and `analogforge simulate ... --backend ads` runs it (inside the ADS Python).

## 5. Known gaps

* The ADS netlist syntax of controllers beyond DC/AC/Tran (HB oscillator with
  `OscPort`, harmonic-balance phase noise) is a best-effort template —
  schematic mode avoids depending on it.
* Phase-noise results from HB are mapped by the VCO design's
  `phase_noise=pnoise` option only if the dataset exposes them in dBc/Hz;
  otherwise it warns and falls back to Leeson.
* Workspace locking: close the workspace in the ADS GUI or use a copy when
  running batch jobs.
