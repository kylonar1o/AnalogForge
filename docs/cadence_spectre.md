# Connecting AnalogForge to Cadence Spectre / Virtuoso (OCEAN)

Status: **adapter** — netlist/OCEAN generation and result parsing are unit-tested
with synthetic PSF files and a fake `spectre` executable, but have not been run
against a real Spectre installation (none was available while developing).
Follow the self-test in step 2 before relying on results.

## 1. What the adapter does

| Mode | Command line AnalogForge runs | Needs | Use it for |
|---|---|---|---|
| `direct` (default) | `spectre input.scs +escchars =log spectre.out -format psfascii -raw psf` | `spectre` on `PATH` | circuits described in AnalogForge (Python/YAML); fastest, no Virtuoso session |
| `ocean`, netlist | `ocean -nograph -restore tb.ocn` on a circuit-only netlist | `ocean` on `PATH` | same circuits, but measurements done by OCEAN calculator functions |
| `ocean`, cellview | `ocean -nograph -restore tb.ocn` with `design("lib" "cell" "view")` | Virtuoso library + `ocean` | **your existing ADE testbenches**: AnalogForge sets `desVar`s, runs sweeps/PVT/optimization, parses the exported waveforms |

Results are parsed from PSF-ASCII (`analogforge/backends/spectre/psf.py`), from
nutmeg files if you choose `-format nutascii`, or (OCEAN mode) from the
`ocnPrint` tables and `analogforge_measurements.txt` the generated script writes.

## 2. Installation on the institute machine

```bash
# 1. Cadence environment (site specific - ask your admin / institute wiki), e.g.
source /opt/cadence/setup.csh        # or: module load cadence/ic cadence/spectre
which spectre ocean virtuoso

# 2. Python >= 3.10 in your home directory (if the system Python is older)
#    e.g. Miniforge: https://github.com/conda-forge/miniforge
conda create -n af python=3.12 -y && conda activate af

# 3. Install AnalogForge from the downloaded repository archive
tar xzf analogforge-0.1.0-repo.tar.gz && cd analogforge-0.1.0
pip install -e .
analogforge doctor             # 'backend spectre  OK  /path/to/spectre' expected
```

ngspice and SKY130 are optional on this machine; they are only needed for the
open-source reference flow.

### Self-test (do this first)

```bash
python scripts/spectre_selftest.py            # direct mode, passive RC, no PDK needed
python scripts/spectre_selftest.py --ocean    # also OCEAN mode
```

Expected: `AC f-3dB = 159155 Hz ... PASS`, `TRAN ... PASS`, `SELF-TEST PASSED`.
If it fails, the kept files (`af_spectre_selftest/`, `/tmp/afspectre_*`) show
the netlist, the Spectre log and the raw PSF files.  Typical fixes:

* PSF files named differently by your Spectre version -> add the suffix to
  `_SUFFIXES` in `backends/spectre/__init__.py`, or run with
  `--format nutascii` (`get_backend("spectre", output_format="nutascii")`).
* A PSF construct the parser does not know -> the parser raises `PSFError`;
  use nutascii or send the file for a parser fix.

## 3. Create a PDK profile for the institute PDK

AnalogForge never ships PDK data; you describe your PDK in a YAML profile.

```bash
mkdir -p ~/.analogforge/pdks
cp src/analogforge/pdks/template_institute.yaml ~/.analogforge/pdks/mypdk.yaml
```

Fill in the placeholders:

1. **Model library** — in ADE L/XL: *Setup → Model Libraries* shows the file
   and section names, e.g. `/cad/pdk/xyz/models/spectre/corners.scs` with
   sections `tt ff ss fs sf`.  Put the path under `models.spectre.include`,
   `section: '{corner}'` and list the sections under `corners:`.
2. **Device instance lines** — open the PDK device in Virtuoso (or netlist a
   small schematic in ADE: *Simulation → Netlist → Display*) and copy the
   instance syntax, e.g. `M0 (d g s b) nch_lvt w=1u l=100n nf=1 m=1`.  Write it
   as a template: `'{name} ({d} {g} {s} {b}) nch_lvt w={w} l={l} nf={nf:.0f} m={m:.0f}'`
   (AnalogForge passes `w`, `l` in metres; use `{w*1e6}u` style if your PDK expects µm).
3. **Operating-point names** (optional, for `save_op`): if the PDK device is a
   subcircuit wrapper, the BSIM instance inside has a hierarchical name such as
   `M1.m0` — check it in the ADE results browser after a DC run and set `op_path`.
4. Check: `analogforge pdk info ~/.analogforge/pdks/mypdk.yaml` →
   `spectre: available: true`.

## 4. Running designs

```bash
# AnalogForge-described circuit, direct Spectre run
analogforge simulate examples/designs/cs_amplifier.yaml --backend spectre --pdk mypdk
# PVT and sweeps work the same way as with ngspice
analogforge corners lc_vco --backend spectre --pdk mypdk --process tt,ss,ff
# native phase noise (PSS + pnoise) instead of the ngspice ISF estimate
analogforge simulate lc_vco --backend spectre --pdk mypdk -o phase_noise=pnoise
# only generate netlist + OCEAN script (e.g. to inspect or run by hand)
analogforge netlist lc_vco --backend spectre --pdk mypdk --out spectre_in/
```

The built-in designs use `nmos`, `pmos` and `varactor` device kinds, so they run
on any PDK whose profile defines those kinds (sizes may need adapting to your
technology: use `-p NAME=VALUE` or a YAML job file).

### Driving an existing ADE testbench (cellview mode)

1. In Virtuoso, make sure the testbench uses design variables for everything
   you want to sweep/optimize (`Ibias`, `Vtune`, ...).
2. Write a small YAML design that lists those variables as `parameters`, an
   empty `circuit: []`, the analyses, the probe nodes and the metrics — see
   `examples/designs/virtuoso_vco_tb.yaml`.
3. Run with the OCEAN backend options:

```bash
analogforge sweep examples/designs/virtuoso_vco_tb.yaml --var Vtune=0:1.2:7 \
    --backend spectre --pdk mypdk --backend-opt mode=ocean --backend-opt cellview=MyLib/vco_tb/schematic
```

The same works from Python:

```python
from analogforge.backends.base import get_backend
from analogforge.core.design import evaluate
be = get_backend("spectre", mode="ocean", cellview=("MyLib", "vco_tb", "schematic"))
res = evaluate("examples/designs/virtuoso_vco_tb.yaml", {"Vtune": 0.8}, backend=be, pdk="mypdk")
print(res.metrics)
```

## 5. Notes and known gaps

* Spectre is case-sensitive; AnalogForge keeps instance and node names as written.
* `ic` statements are emitted for initial conditions; `uic` transient becomes `skipdc=yes`.
* OCEAN result names (`pss_fd`, `pnoise`) and the `phaseNoise()` call follow the OCEAN
  reference manual; verify them once in your IC release (CIW: `help phaseNoise`).
* Phase noise from Spectre `pnoise` is mapped into the same `pn_100k/pn_1M/pn_10M`
  metrics; if your release names the output differently, the VCO measurement warns and
  falls back to the Leeson estimate - adapt `LCVCO._native_phase_noise`.
* License queueing / farm submission (LSF, Grid Engine) is not handled; wrap the
  `spectre` command via `get_backend("spectre", executable="/path/to/wrapper")`.
