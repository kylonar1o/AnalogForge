"""Declarative YAML designs.

Example (see ``analogforge/designs/yaml/*.yaml`` and ``docs/yaml_format.md``)::

    name: rc_lowpass
    kind: amplifier
    parameters:
      R: {default: 1k, min: 100, max: 100k, unit: Ohm, log: true}
      C: 1p
    circuit:
      - {type: vsource, name: VIN, p: in, n: 0, dc: 0, ac: 1}
      - {type: resistor, name: R1, nodes: [in, out], value: R}
      - {type: capacitor, name: C1, nodes: [out, 0], value: C}
    testbenches:
      - name: main
        analyses:
          - {type: ac, fstart: 1k, fstop: 10g, ppd: 20}
    metrics:
      - {name: f3db, extractor: bandwidth_3db, analysis: ac, signal: "v(out)", unit: Hz, goal: max}
      - {name: tau, expr: "R*C"}

Every numeric value may be an expression of the parameters; the namespace
also contains ``vdd_nom`` (PDK nominal supply), ``vdd_scale`` (PVT supply
factor), ``temperature`` and ``corner``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from ..expr import evaluate, evaluate_number
from ..metrics import EXTRACTORS
from ..units import parse_si
from . import analyses as A
from .circuit import PWL, Circuit, Pulse, Sine
from .design import Curve, Design, DesignContext, DesignError, Measurement, MetricSpec, ParamSpec
from .testbench import Testbench

_TWO_TERMINAL = {"resistor": ("r", "R"), "capacitor": ("c", "C"), "inductor": ("l", "L")}


def _num(value, ns) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return parse_si(value)
        except ValueError:
            return evaluate_number(value, ns)
    raise DesignError(f"cannot interpret {value!r} as a number")


def _waveform(spec: dict, ns):
    if "sine" in spec:
        s = spec["sine"]
        return Sine(_num(s.get("offset", 0), ns), _num(s.get("amplitude", 0), ns), _num(s.get("freq", 1e6), ns),
                    _num(s.get("delay", 0), ns), _num(s.get("phase", 0), ns))
    if "pulse" in spec:
        s = spec["pulse"]
        return Pulse(_num(s.get("v1", 0), ns), _num(s.get("v2", 1), ns), _num(s.get("delay", 0), ns),
                     _num(s.get("rise", 1e-9), ns), _num(s.get("fall", 1e-9), ns),
                     _num(s.get("width", 1e-6), ns), _num(s.get("period", 2e-6), ns))
    if "pwl" in spec:
        return PWL([(_num(t, ns), _num(v, ns)) for t, v in spec["pwl"]])
    return None


def build_circuit(name: str, elements: list[dict], ns: dict, ics: dict | None = None) -> Circuit:
    c = Circuit(name)
    for i, el in enumerate(elements):
        t = str(el.get("type", "")).lower()
        nm = el.get("name") or f"{t[:1].upper()}{i}"
        try:
            if t in _TWO_TERMINAL:
                n1, n2 = el["nodes"]
                val = _num(el.get("value"), ns)
                if t == "inductor" and el.get("q") is not None:
                    c.lossy_inductor(nm, str(n1), str(n2), val, _num(el["q"], ns), _num(el["f0"], ns))
                else:
                    getattr(c, t)(nm, str(n1), str(n2), val)
            elif t in ("vsource", "isource"):
                p, n = (el["nodes"] if "nodes" in el else (el["p"], el.get("n", "0")))
                fn = c.vsource if t == "vsource" else c.isource
                fn(nm, str(p), str(n), dc=_num(el.get("dc", 0), ns), ac=_num(el.get("ac", 0), ns),
                   waveform=_waveform(el, ns))
            elif t in ("vcvs", "vccs"):
                p, n, cp, cn = el["nodes"]
                if t == "vcvs":
                    c.vcvs(nm, str(p), str(n), str(cp), str(cn), _num(el.get("gain", 1), ns))
                else:
                    c.vccs(nm, str(p), str(n), str(cp), str(cn), _num(el.get("gm", 1e-3), ns))
            elif t == "mutual":
                c.mutual(nm, el["l1"], el["l2"], _num(el["k"], ns))
            elif t == "servo":
                c.add_servo(nm, str(el["sense"]), str(el["ref"]), str(el["out"]), _num(el.get("gain", 1000), ns))
            elif t in ("nmos", "pmos"):
                pins = {k: str(el[k]) for k in ("d", "g", "s", "b")}
                params = {k: _num(el[k], ns) for k in ("w", "l") if k in el}
                params.update({k: _num(el[k], ns) for k in ("nf", "m") if k in el})
                c.device(nm, t, pins, **params)
            elif t == "device":
                pins = {k: str(v) for k, v in el["pins"].items()}
                params = {k: _num(v, ns) for k, v in (el.get("params") or {}).items()}
                c.device(nm, el["kind"], pins, **params)
            elif t == "varactor":
                c.varactor(nm, str(el["g"]), str(el["nw"]), _num(el["w"], ns), _num(el["l"], ns),
                           int(_num(el.get("m", 1), ns)), str(el.get("b", "0")))
            elif t == "raw":
                c.raw(nm, dict(el["text"]), el.get("nodes", []))
            else:
                raise DesignError(f"unknown element type {t!r}")
        except KeyError as exc:
            raise DesignError(f"element #{i} ({nm}, type {t}): missing field {exc}") from None
    for node, v in (ics or {}).items():
        c.ics[str(node)] = _num(v, ns)
    return c


def _analysis(spec: dict, ns) -> A.Analysis:
    t = str(spec.get("type", "")).lower()
    name = spec.get("name", "")
    g = lambda k, d=None: _num(spec[k], ns) if k in spec else d  # noqa: E731
    if t == "op":
        return A.Op(name)
    if t == "dc":
        return A.DCSweep(name, spec["source"], g("start"), g("stop"), g("step"))
    if t == "ac":
        return A.AC(name, g("fstart", 1.0), g("fstop", 1e9), int(g("ppd", 20)))
    if t == "tran":
        return A.Tran(name, g("tstop"), g("tstep", g("tstop") / 1000), g("tstart", 0.0), g("tmax"),
                      bool(spec.get("uic", False)), list(spec.get("save", [])))
    if t == "noise":
        return A.Noise(name, spec.get("out_p", "out"), spec.get("out_n", "0"), spec["input_source"],
                       g("fstart", 1.0), g("fstop", 1e9), int(g("ppd", 10)))
    if t == "pss":
        return A.PSS(name, g("fund"), int(g("harmonics", 10)), g("tstab", 10e-9), spec.get("osc_p"),
                     spec.get("osc_n", "0"), int(g("points", 128)))
    if t == "pnoise":
        return A.PNoise(name, g("fstart", 1e3), g("fstop", 1e8), int(g("ppd", 10)),
                        spec.get("osc_p", "outp"), spec.get("osc_n", "outn"))
    raise DesignError(f"unknown analysis type {t!r}")


class YamlDesign(Design):
    def __init__(self, data: dict, source: str = ""):
        self.data = data
        self.source = source
        self.name = data["name"]
        self.title = data.get("title", self.name)
        self.description = data.get("description", "")
        self.kind = data.get("kind", "generic")
        self.parameters = []
        for pname, spec in (data.get("parameters") or {}).items():
            if not isinstance(spec, dict):
                spec = {"default": spec}
            self.parameters.append(ParamSpec(
                pname, parse_si(spec["default"]),
                parse_si(spec["min"]) if spec.get("min") is not None else None,
                parse_si(spec["max"]) if spec.get("max") is not None else None,
                spec.get("unit", ""), bool(spec.get("log", False)), spec.get("description", ""),
                bool(spec.get("tunable", True)), bool(spec.get("integer", False))))
        self.metrics = [MetricSpec(m["name"], m.get("unit", ""), m.get("description", ""), m.get("goal", ""))
                        for m in data.get("metrics", [])]
        self.options = dict(data.get("options") or {})
        self.default_objective = data.get("default_objective", "")
        self.default_constraints = list(data.get("default_constraints", []))
        for m in data.get("metrics", []):
            if "extractor" in m and m["extractor"] not in EXTRACTORS:
                raise DesignError(f"metric {m['name']}: unknown extractor {m['extractor']!r} "
                                  f"(available: {', '.join(EXTRACTORS)})")
        if not data.get("testbenches"):
            raise DesignError(f"YAML design {self.name!r} has no testbenches")

    def _ns(self, p: dict, ctx: DesignContext) -> dict:
        ns = dict(p)
        ns.update({"vdd_nom": ctx.pdk.nominal_vdd, "vdd_scale": ctx.vdd_scale,
                   "temperature": ctx.temperature, "corner": ctx.corner})
        return ns

    def testbenches(self, p, ctx):
        ns = self._ns(p, ctx)
        out = []
        for tbspec in self.data["testbenches"]:
            elements = list(self.data.get("circuit", [])) + list(tbspec.get("extra_elements", []))
            circ = build_circuit(f"{self.name}", elements, ns, {**(self.data.get("ic") or {}),
                                                               **(tbspec.get("ic") or {})})
            ans = [_analysis(a, ns) for a in tbspec.get("analyses", [])]
            out.append(Testbench(tbspec.get("name", "main"), circ, ans, save_op=list(tbspec.get("save_op", [])),
                                 options=dict(tbspec.get("options") or {}), variables=dict(p),
                                 probes=list(tbspec.get("probes", []))))
        return out

    def measure(self, p, results, ctx):
        ns = self._ns(p, ctx)
        meas = Measurement()
        default_tb = self.data["testbenches"][0].get("name", "main")
        for m in self.data.get("metrics", []):
            name = m["name"]
            try:
                if "expr" in m:
                    val = evaluate(m["expr"], {**ns, **meas.metrics})
                else:
                    r = results.get(m.get("testbench", default_tb))
                    if r is None:
                        raise DesignError(f"testbench {m.get('testbench', default_tb)!r} not found")
                    ds = r.get(m["analysis"])
                    if ds is None:
                        raise DesignError(f"analysis {m['analysis']!r} has no data ({r.error})")
                    args = {k: (_num(v, {**ns, **meas.metrics}) if not isinstance(v, bool) else v)
                            for k, v in (m.get("args") or {}).items()}
                    val = EXTRACTORS[m["extractor"]](ds, m["signal"], **args)
                meas.metrics[name] = float(val)
            except Exception as exc:  # keep going, record NaN
                meas.metrics[name] = float("nan")
                meas.warnings.append(f"metric {name}: {type(exc).__name__}: {exc}")
        for pl in self.data.get("plots", []):
            r = results.get(pl.get("testbench", default_tb))
            ds = r.get(pl["analysis"]) if r else None
            if ds is None:
                continue
            try:
                series = {s: np.asarray(ds.eval(s)) for s in pl["signals"]}
                if pl.get("db"):
                    series = {k: 20 * np.log10(np.maximum(np.abs(v), 1e-300)) for k, v in series.items()}
                meas.curves.append(Curve(pl.get("name", pl["analysis"]), pl.get("title", ""), ds.sweep, series,
                                         pl.get("xlabel", ds.sweep_name), pl.get("ylabel", ""),
                                         bool(pl.get("xlog", ds.kind in ("ac", "noise")))))
            except Exception as exc:
                meas.warnings.append(f"plot {pl.get('name')}: {exc}")
        return meas


def load_yaml_design(path: str | Path) -> YamlDesign:
    path = Path(path)
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "name" not in data:
        raise DesignError(f"{path}: a YAML design needs at least 'name', 'circuit', 'testbenches'")
    return YamlDesign(data, str(path))


__all__ = ["YamlDesign", "load_yaml_design", "build_circuit"]
