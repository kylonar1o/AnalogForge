"""Parameter sweeps (grid or zipped) and PVT corner sweeps."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

from ..core.design import EvaluationResult
from ..units import parse_si
from .executor import Point, evaluate_points

CONDITION_KEYS = {"corner", "temperature", "vdd_scale"}


def parse_values(spec) -> list:
    """Sweep values from a list, ``{start, stop, num[, log]}``, ``{start, stop, step}`` or ``'a:b:n'``."""
    if isinstance(spec, (list, tuple)):
        return [v if isinstance(v, str) and not _is_number(v) else parse_si(v) for v in spec]
    if isinstance(spec, dict):
        if "values" in spec:
            return parse_values(spec["values"])
        start, stop = parse_si(spec["start"]), parse_si(spec["stop"])
        if "num" in spec:
            num = int(spec["num"])
            if spec.get("log"):
                return list(np.logspace(math.log10(start), math.log10(stop), num))
            return list(np.linspace(start, stop, num))
        step = parse_si(spec["step"])
        n = int(math.floor((stop - start) / step + 1e-9)) + 1
        return [start + i * step for i in range(n)]
    if isinstance(spec, str) and ":" in spec:
        a, b, c = spec.split(":")
        return list(np.linspace(parse_si(a), parse_si(b), int(c)))
    return [spec if isinstance(spec, str) and not _is_number(spec) else parse_si(spec)]


def _is_number(s: str) -> bool:
    try:
        parse_si(s)
        return True
    except ValueError:
        return False


def make_points(variables: dict[str, object], base_params: dict | None = None, mode: str = "grid",
                corner: str | None = None, temperature: float = 27.0, vdd_scale: float = 1.0) -> list[Point]:
    names = list(variables)
    values = [parse_values(variables[k]) for k in names]
    if mode == "zip":
        lengths = {len(v) for v in values}
        if len(lengths) != 1:
            raise ValueError("zip sweep: all variables need the same number of values")
        combos = list(zip(*values))
    else:
        combos = list(itertools.product(*values))
    pts = []
    for combo in combos:
        params = dict(base_params or {})
        cond = {"corner": corner, "temperature": temperature, "vdd_scale": vdd_scale}
        label = {}
        for k, v in zip(names, combo):
            label[k] = v if isinstance(v, str) else float(v)
            if k in CONDITION_KEYS:
                cond[k] = v if k == "corner" else float(v)
            else:
                params[k] = float(v)
        pts.append(Point(params, cond["corner"], float(cond["temperature"]), float(cond["vdd_scale"]), label))
    return pts


@dataclass
class SweepResult:
    points: list[Point]
    results: list[EvaluationResult]
    variables: list[str] = field(default_factory=list)

    def table(self, metrics: list[str] | None = None) -> list[dict]:
        rows = []
        for pt, r in zip(self.points, self.results):
            row = dict(pt.label)
            ms = metrics or list(r.metrics)
            row.update({m: r.metrics.get(m, float("nan")) for m in ms})
            row["ok"] = r.ok
            rows.append(row)
        return rows


def run_sweep(design, variables: dict, base_params: dict | None = None, mode: str = "grid", backend="ngspice",
              pdk="sky130", corner=None, temperature=27.0, vdd_scale=1.0, options=None, workers=None,
              progress=None, cancel=None, on_result=None) -> SweepResult:
    pts = make_points(variables, base_params, mode, corner, temperature, vdd_scale)
    res = evaluate_points(design, pts, backend, pdk, options, workers, progress, cancel, on_result)
    return SweepResult(pts, res, list(variables))


# ----------------------------------------------------------------------- PVT
def pvt_points(process: list[str], voltage: list[float], temperature: list[float],
               base_params: dict | None = None) -> list[Point]:
    pts = []
    for c, v, t in itertools.product(process, voltage, temperature):
        pts.append(Point(dict(base_params or {}), c, float(t), float(v),
                         {"corner": c, "vdd_scale": float(v), "temperature": float(t)}))
    return pts


def worst_case(design, results: list[EvaluationResult], points: list[Point]) -> dict[str, dict]:
    """Per metric: nominal-direction worst value and the PVT point where it occurs."""
    goals = {m.name: m.goal for m in getattr(design, "metrics", [])}
    names = sorted({k for r in results for k in r.metrics})
    out = {}
    for name in names:
        vals = np.array([r.metrics.get(name, np.nan) for r in results], float)
        ok = np.isfinite(vals)
        if not ok.any():
            continue
        goal = goals.get(name, "")
        if goal == "max":
            idx = int(np.nanargmin(vals))
        elif goal == "min":
            idx = int(np.nanargmax(vals))
        else:
            idx = None
        out[name] = {"min": float(np.nanmin(vals)), "max": float(np.nanmax(vals)),
                     "mean": float(np.nanmean(vals)), "goal": goal,
                     "worst": float(vals[idx]) if idx is not None else None,
                     "worst_at": points[idx].label if idx is not None else None,
                     "spread_pct": float(100 * (np.nanmax(vals) - np.nanmin(vals)) / abs(np.nanmean(vals)))
                     if np.nanmean(vals) != 0 else None}
    return out


def run_corners(design, base_params: dict | None = None, process: list[str] | None = None,
                voltage: list[float] | None = None, temperature: list[float] | None = None, backend="ngspice",
                pdk="sky130", options=None, workers=None, progress=None, cancel=None, on_result=None):
    from ..pdks import get_pdk
    pdk_obj = get_pdk(pdk)
    process = process or list(pdk_obj.corners)
    voltage = voltage or [0.9, 1.0, 1.1]
    temperature = temperature or list(pdk_obj.temperatures)
    pts = pvt_points(process, voltage, temperature, base_params)
    res = evaluate_points(design, pts, backend, pdk, options, workers, progress, cancel, on_result)
    from ..designs import get_design
    d = get_design(design) if isinstance(design, str) else design
    return SweepResult(pts, res, ["corner", "vdd_scale", "temperature"]), worst_case(d, res, pts)
