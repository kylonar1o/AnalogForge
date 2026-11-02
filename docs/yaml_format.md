# YAML formats

## Design files

```yaml
name: my_design                 # unique name (required)
title: Human readable title
kind: oscillator                # oscillator | amplifier | generic
description: Free text

parameters:                     # design variables; values accept SPICE suffixes (10u, 2.2k, 1meg)
  W:  {default: 10u, min: 1u, max: 50u, unit: m, log: true, description: "width"}
  CL: {default: 1p, unit: F, tunable: false}   # tunable: false -> optimizers skip it
  N:  {default: 4, min: 1, max: 20, integer: true}
  R:  1k                        # shorthand: default only

options: {}                     # free options passed to the measurement (Python designs use them)
default_objective: "max:gain_db"
default_constraints: ["f3db >= 10meg"]

circuit:                        # element list; numeric fields are expressions of the parameters
  - {type: vsource, name: VDD, p: vdd, n: 0, dc: "VDD*vdd_scale"}
  - {type: vsource, name: VIN, p: in, n: 0, dc: 0.9, ac: 1}
  - {type: vsource, name: VP,  p: a,  n: 0, pulse: {v1: 0, v2: 1, delay: 1n, rise: 10p, fall: 10p, width: 5n, period: 10n}}
  - {type: vsource, name: VS,  p: b,  n: 0, sine: {offset: 0, amplitude: 0.1, freq: 1meg}}
  - {type: isource, name: IB,  p: vdd, n: nb, dc: 100u}       # current flows p -> n through the source
  - {type: resistor,  name: R1, nodes: [a, b], value: R}
  - {type: capacitor, name: C1, nodes: [b, 0], value: CL}
  - {type: inductor,  name: L1, nodes: [vdd, x], value: 2n, q: 10, f0: 3g}  # q/f0 -> series loss resistor
  - {type: mutual,    name: K1, l1: L1, l2: L2, k: 0.7}
  - {type: nmos, name: M1, d: out, g: in, s: 0, b: 0, w: W, l: 0.15u, nf: 1, m: 1}
  - {type: pmos, name: M2, d: out, g: nb, s: vdd, b: vdd, w: 2*W, l: 0.15u}
  - {type: varactor, name: XV1, g: outp, nw: vtune, w: 2u, l: 0.18u, m: 60}
  - {type: device, name: X1, kind: my_kind, pins: {a: n1, b: n2}, params: {w: 1u}}  # any PDK device kind
  - {type: vcvs, name: E1, nodes: [p, n, cp, cn], gain: 10}
  - {type: vccs, name: G1, nodes: [p, n, cp, cn], gm: 1m}
  - {type: servo, name: SV, sense: out, ref: vref, out: g}     # DC operating-point servo (open-loop benches)
  - {type: raw, name: B1, text: {ngspice: "B1 x 0 V=v(a)*v(b)"}, nodes: [x, a, b]}

ic: {outp: 1.8}                 # initial conditions (all testbenches)

testbenches:
  - name: main
    analyses:
      - {type: op, name: op}
      - {type: dc, name: dc, source: VIN, start: 0, stop: 1.8, step: 10m}
      - {type: ac, name: ac, fstart: 1, fstop: 10g, ppd: 20}
      - {type: tran, name: tran, tstop: 50n, tstep: 5p, tmax: 5p, uic: false, save: ["M1:gm"]}
      - {type: noise, name: noise, out_p: out, out_n: 0, input_source: VIN, fstart: 1, fstop: 1g}
      - {type: pss, name: pss, fund: 3g, harmonics: 10, tstab: 20n, osc_p: outp, osc_n: outn}
      - {type: pnoise, name: pn, fstart: 1k, fstop: 100meg, osc_p: outp, osc_n: outn}   # Spectre/ADS only
    save_op: [M1]               # operating-point parameters (gm, gds, id, vth, vdsat, cgg, ...)
    probes: ["v(out)"]          # signals exported in OCEAN mode
    ic: {}                      # extra initial conditions
    extra_elements: []          # elements added only to this testbench
    options: {reltol: 1e-4}

metrics:
  - {name: gain_db, extractor: gain_db, testbench: main, analysis: ac, signal: "v(out)/v(in)", unit: dB, goal: max}
  - {name: power, extractor: supply_power, analysis: op, signal: VDD, args: {vdd: "VDD*vdd_scale"}, unit: W}
  - {name: gm, extractor: value, analysis: op, signal: "op(m1, gm)"}
  - {name: f_osc, extractor: osc_frequency, analysis: tran, signal: "v(outp)-v(outn)", args: {fraction: 0.3}}
  - {name: ratio, expr: "gain_db - 20*log10(gm*R)"}     # formula of parameters and earlier metrics

plots:
  - {name: bode, analysis: ac, signals: ["v(out)"], db: true, title: "Gain", ylabel: dB}
```

Namespace available in expressions: all parameters, `vdd_nom` (PDK nominal
supply), `vdd_scale` (PVT supply factor), `temperature`, `corner`, constants
`pi`, `e`, `k_B`, `q_e`, and functions `sqrt exp log log10 sin cos tan atan
abs min max db20 db10 mag phase real imag`.  Expressions are evaluated by a
restricted evaluator (no attribute access, no imports).

Signal expressions (metrics, plots): `v(node)`, `i(source)`, `op(device, param)`,
arithmetic and the functions above, e.g. `db20(v(out)/(v(inp)-v(inn)))`.

Built-in extractors: `osc_frequency fft_frequency amplitude startup_time thd
slew_rate settling_time max min mean rms value final dc_gain gain_db
bandwidth_3db unity_gain_frequency phase_margin gain_margin supply_power`.

## Job files (`analogforge run job.yaml`)

```yaml
kind: optimize                 # simulate | sweep | corners | optimize
name: optional label
design: lc_vco                 # built-in, YAML design name, or path
backend: ngspice               # ngspice | spectre | ads
backend_options: {}            # e.g. {mode: ocean, cellview: MyLib/vco_tb/schematic}
pdk: sky130                    # profile name or path
corner: tt
temperature: 27
vdd_scale: 1.0
params: {IBIAS: 2m}
options: {tuning_points: 3, phase_noise: leeson}
workers: 4

sweep:                         # kind: sweep
  mode: grid                   # grid (cartesian) | zip
  variables:
    IBIAS: {start: 1m, stop: 4m, num: 7}            # or {start, stop, step}, log: true, or a list
    corner: [tt, ss]                                # conditions can be swept too
    temperature: [-40, 27, 125]

corners:                       # kind: corners
  process: [tt, ff, ss, sf, fs]
  voltage: [0.9, 1.0, 1.1]     # supply scale factors
  temperature: [-40, 27, 125]

optimize:                      # kind: optimize
  method: bayes                # bayes | de | nelder-mead | random
  variables: {IBIAS: [1m, 4m], WN: [10u, 50u]}      # or a list of names (ranges from the design), or null
  objective: "max:fom"
  constraints: ["f_osc >= 3e9", "amplitude_diff >= 0.5"]
  budget: 24
  seed: 0
```
