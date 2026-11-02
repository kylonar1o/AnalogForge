"""Designs, design context and the single evaluation entry point used by all flows."""

from __future__ import annotations

import inspect
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..units import parse_si
from .pdk import PDK
from .results import SimResult
from .testbench import Testbench


@dataclass
class ParamSpec:
    name: str
    default: float
    min: float | None = None
    max: float | None = None
    unit: str = ""
    log: bool = False
    description: str = ""
    #: False for specifications (supply, load) that optimizers should not touch by default
    tunable: bool = True
    integer: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "default": self.default, "min": self.min, "max": self.max,
                "unit": self.unit, "log": self.log, "description": self.description,
                "tunable": self.tunable, "integer": self.integer}


@dataclass
class MetricSpec:
    name: str
    unit: str = ""
    description: str = ""
    goal: str = ""          # "max", "min" or ""

    def to_dict(self) -> dict:
        return {"name": self.name, "unit": self.unit, "description": self.description, "goal": self.goal}


@dataclass
class Curve:
    """Plot-ready data: one x axis and named y series."""

    name: str
    title: str
    x: np.ndarray
    series: dict[str, np.ndarray]
    xlabel: str = ""
    ylabel: str = ""
    xlog: bool = False
    ylog: bool = False

    def to_dict(self, max_points: int | None = 2000) -> dict:
        x = np.real(np.asarray(self.x, dtype=complex)).astype(float)
        idx = np.arange(len(x))
        if max_points and len(x) > max_points:
            idx = np.unique(np.linspace(0, len(x) - 1, max_points).astype(int))

        def clean(a):
            a = np.real(np.asarray(a, dtype=complex)).astype(float)[idx]
            return [None if not np.isfinite(v) else float(v) for v in a]
        return {"name": self.name, "title": self.title, "x": clean(x),
                "series": {k: clean(v) for k, v in self.series.items()},
                "xlabel": self.xlabel, "ylabel": self.ylabel, "xlog": self.xlog, "ylog": self.ylog}

    @classmethod
    def from_dict(cls, d: dict) -> "Curve":
        nanify = lambda seq: np.array([np.nan if v is None else v for v in seq], float)  # noqa: E731
        return cls(d["name"], d.get("title", ""), nanify(d["x"]),
                   {k: nanify(v) for k, v in d["series"].items()}, d.get("xlabel", ""),
                   d.get("ylabel", ""), d.get("xlog", False), d.get("ylog", False))


@dataclass
class Measurement:
    metrics: dict[str, float] = field(default_factory=dict)
    curves: list[Curve] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DesignContext:
    pdk: PDK
    backend: object  # analogforge.backends.base.Backend (kept untyped to avoid an import cycle)
    corner: str
    temperature: float = 27.0
    vdd_scale: float = 1.0
    options: dict = field(default_factory=dict)
    simulate: Callable[[list[Testbench]], dict[str, SimResult]] | None = None

    def option(self, key: str, default=None):
        return self.options.get(key, default)


class DesignError(ValueError):
    pass


class Design:
    """Base class.  Subclasses define parameters, testbenches and the measurement step."""

    name = "design"
    title = "Design"
    description = ""
    kind = "generic"             # "oscillator", "amplifier" or "generic"
    parameters: list[ParamSpec] = []
    metrics: list[MetricSpec] = []
    #: default options (e.g. {"phase_noise": "leeson", "pss": False})
    options: dict = {}
    #: suggested optimization setup, used by the UI as a starting point
    default_objective: str = ""
    default_constraints: list[str] = []

    def param_map(self) -> dict[str, ParamSpec]:
        return {p.name: p for p in self.parameters}

    def defaults(self) -> dict[str, float]:
        return {p.name: float(p.default) for p in self.parameters}

    def resolve(self, overrides: dict | None = None) -> dict[str, float]:
        values = self.defaults()
        pmap = {k.lower(): k for k in self.param_map()}
        for key, val in (overrides or {}).items():
            real = pmap.get(str(key).lower())
            if real is None:
                raise DesignError(f"unknown parameter {key!r} for design {self.name!r}; "
                                  f"valid: {', '.join(self.param_map())}")
            v = parse_si(val)
            if self.param_map()[real].integer:
                v = float(round(v))
            values[real] = v
        return values

    def testbenches(self, p: dict[str, float], ctx: DesignContext) -> list[Testbench]:
        raise NotImplementedError

    def measure(self, p: dict[str, float], results: dict[str, SimResult], ctx: DesignContext) -> Measurement:
        raise NotImplementedError

    def info(self) -> dict:
        return {"name": self.name, "title": self.title, "description": inspect.cleandoc(self.description or ""),
                "kind": self.kind, "parameters": [p.to_dict() for p in self.parameters],
                "metrics": [m.to_dict() for m in self.metrics], "options": dict(self.options),
                "default_objective": self.default_objective, "default_constraints": self.default_constraints}


# --------------------------------------------------------------------- evaluation
@dataclass
class EvaluationResult:
    design: str
    backend: str
    pdk: str
    corner: str
    temperature: float
    vdd_scale: float
    params: dict[str, float]
    options: dict = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    curves: list[Curve] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    netlists: dict[str, str] = field(default_factory=dict)
    runs: dict[str, dict] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    simulator_version: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def metric(self, name: str) -> float:
        return float(self.metrics.get(name, float("nan")))

    def to_dict(self, curves: bool = True, netlists: bool = True, max_points: int = 2000) -> dict:
        def clean(v):
            if isinstance(v, (float, np.floating)):
                return float(v) if np.isfinite(v) else None
            return v
        d = {
            "design": self.design, "backend": self.backend, "pdk": self.pdk, "corner": self.corner,
            "temperature": self.temperature, "vdd_scale": self.vdd_scale,
            "params": {k: clean(v) for k, v in self.params.items()}, "options": self.options,
            "metrics": {k: clean(v) for k, v in self.metrics.items()},
            "extra": _jsonable(self.extra), "runs": self.runs, "errors": self.errors,
            "warnings": self.warnings, "ok": self.ok, "elapsed_s": round(self.elapsed_s, 4),
            "simulator_version": self.simulator_version,
        }
        if curves:
            d["curves"] = [c.to_dict(max_points) for c in self.curves]
        if netlists:
            d["netlists"] = self.netlists
        return d


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    if isinstance(obj, (float, np.floating)):
        return float(obj) if np.isfinite(obj) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    return obj


def evaluate(design, params: dict | None = None, backend="ngspice", pdk="sky130", corner: str | None = None,
             temperature: float = 27.0, vdd_scale: float = 1.0, options: dict | None = None,
             max_parallel: int | None = None, keep_netlists: int = 8) -> EvaluationResult:
    """Simulate ``design`` at one design point and extract its metrics.

    Never raises for simulation problems: they end up in ``result.errors``
    (metrics that could not be measured are NaN).
    """
    from ..backends.base import get_backend
    from ..designs import get_design
    from ..pdks import get_pdk

    t0 = time.perf_counter()
    design = get_design(design) if isinstance(design, str) else design
    pdk_obj = get_pdk(pdk)
    be = get_backend(backend)
    corner = corner or pdk_obj.default_corner
    opts = dict(design.options)
    opts.update(options or {})
    res = EvaluationResult(design.name, be.name, pdk_obj.name, corner, float(temperature), float(vdd_scale),
                           params={}, options=opts)
    try:
        p = design.resolve(params)
        res.params = p
        pdk_obj.check_corner(corner)
        ok, why = be.available()
        if not ok:
            raise RuntimeError(f"backend {be.name} unavailable: {why}")
        ok, why = pdk_obj.available(be.name)
        if not ok:
            raise RuntimeError(f"PDK {pdk_obj.name} not usable with {be.name}: {why}")
        res.simulator_version = be.version()
    except Exception as exc:
        res.errors.append(f"{type(exc).__name__}: {exc}")
        res.elapsed_s = time.perf_counter() - t0
        return res

    stored = {"n": 0}

    def simulate(tbs: list[Testbench]) -> dict[str, SimResult]:
        for tb in tbs:
            tb.corner = tb.corner or corner
            tb.temperature = float(temperature)  # one evaluation = one temperature
        workers = max(1, min(len(tbs), max_parallel or len(tbs)))
        out: dict[str, SimResult] = {}
        if workers == 1:
            for tb in tbs:
                out[tb.name] = be.run(tb, pdk_obj)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for tb, r in zip(tbs, pool.map(lambda t: be.run(t, pdk_obj), tbs)):
                    out[tb.name] = r
        for tb in tbs:
            r = out[tb.name]
            res.runs[tb.name] = r.summary()
            if stored["n"] < keep_netlists or not r.ok:
                res.netlists[tb.name] = r.netlist
                stored["n"] += 1
            if r.fixes_applied:
                res.warnings.append(f"{tb.name}: automatic convergence fix(es) {r.fixes_applied}"
                                    + (" (reduced accuracy)" if r.reduced_accuracy else ""))
        return out

    ctx = DesignContext(pdk_obj, be, corner, float(temperature), float(vdd_scale), opts, simulate)
    try:
        tbs = design.testbenches(p, ctx)
        results = simulate(tbs)
        for name, r in results.items():
            if not r.ok:
                res.errors.append(f"testbench '{name}' failed: {r.error}")
        meas = design.measure(p, results, ctx)
        res.metrics = {k: float(v) for k, v in meas.metrics.items()}
        res.curves = meas.curves
        res.extra = meas.extra
        res.warnings.extend(meas.warnings)
    except Exception as exc:
        res.errors.append(f"{type(exc).__name__}: {exc}")
    nan = [k for k, v in res.metrics.items() if not math.isfinite(v)]
    if nan:
        res.warnings.append("metrics not measurable (NaN): " + ", ".join(nan))
    res.elapsed_s = time.perf_counter() - t0
    return res
