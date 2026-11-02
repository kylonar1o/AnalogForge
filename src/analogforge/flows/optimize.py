"""Single-objective optimization with metric constraints.

Methods: ``bayes`` (Gaussian process, Matern-5/2 ARD kernel, expected
improvement; batches of points proposed with the constant-liar heuristic and
evaluated in parallel), ``de`` (scipy differential evolution, population
evaluated in parallel), ``nelder-mead`` (scipy) and ``random`` (Latin
hypercube baseline).  All methods work in the unit cube; log-scaled variables
are optimized in log space; integer variables are rounded.

Cost = -metric (``max:``) or +metric (``min:``) plus a quadratic penalty for
violated constraints, scaled by the magnitude of the objective.  Failed or
NaN evaluations get a large penalty, so the optimizer steers away from them.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..core.design import EvaluationResult
from ..expr import evaluate as eval_expr
from ..expr import parse_constraint
from ..units import parse_si
from .executor import Cancelled, Point, evaluate_points

FAIL_COST = 1e6


@dataclass
class Variable:
    name: str
    low: float
    high: float
    log: bool = False
    integer: bool = False

    def to_real(self, u: float) -> float:
        u = min(max(float(u), 0.0), 1.0)
        if self.log:
            v = math.exp(math.log(self.low) + u * (math.log(self.high) - math.log(self.low)))
        else:
            v = self.low + u * (self.high - self.low)
        return float(round(v)) if self.integer else float(v)

    def to_unit(self, v: float) -> float:
        if self.log:
            return (math.log(v) - math.log(self.low)) / (math.log(self.high) - math.log(self.low))
        return (v - self.low) / (self.high - self.low)


@dataclass
class Objective:
    metric: str
    goal: str = "max"

    @classmethod
    def parse(cls, text: str) -> "Objective":
        goal, _, metric = text.partition(":")
        if not metric:
            raise ValueError(f"objective {text!r} must look like 'max:metric' or 'min:metric'")
        if goal not in ("max", "min"):
            raise ValueError(f"objective goal must be 'max' or 'min', got {goal!r}")
        return cls(metric.strip(), goal)


@dataclass
class Constraint:
    metric: str
    op: str
    limit: float
    text: str = ""

    @classmethod
    def parse(cls, text: str, namespace: dict | None = None) -> "Constraint":
        lhs, op, rhs = parse_constraint(text)
        try:
            limit = parse_si(rhs)
        except ValueError:
            limit = float(eval_expr(rhs, namespace or {}))
        return cls(lhs.strip(), op, float(limit), text)

    def violation(self, metrics: dict[str, float]) -> float:
        v = metrics.get(self.metric, float("nan"))
        if not np.isfinite(v):
            return float("inf")
        scale = max(abs(self.limit), 1e-30)
        if self.op in (">=", ">"):
            return max(0.0, (self.limit - v) / scale)
        if self.op in ("<=", "<"):
            return max(0.0, (v - self.limit) / scale)
        return abs(v - self.limit) / scale


@dataclass
class Trial:
    index: int
    params: dict[str, float]
    metrics: dict[str, float]
    cost: float
    feasible: bool
    ok: bool


@dataclass
class OptimizationResult:
    method: str
    objective: Objective
    trials: list[Trial] = field(default_factory=list)
    best: Trial | None = None
    message: str = ""

    def summary(self) -> dict:
        b = self.best
        return {
            "method": self.method, "objective": f"{self.objective.goal}:{self.objective.metric}",
            "evaluations": len(self.trials), "message": self.message,
            "best": None if b is None else {"index": b.index, "params": b.params, "metrics": b.metrics,
                                              "cost": b.cost, "feasible": b.feasible},
            "convergence": _convergence(self.trials),
        }


def _convergence(trials: list[Trial]) -> list[float | None]:
    best, out = float("inf"), []
    for t in trials:
        if t.cost < best:
            best = t.cost
        out.append(best if np.isfinite(best) else None)
    return out


def variables_from_design(design, names: list[str] | None = None, overrides: dict | None = None) -> list[Variable]:
    """Optimization variables from design parameter ranges (tunable ones if ``names`` is None)."""
    pm = design.param_map()
    names = names or [p.name for p in design.parameters if p.tunable and p.min is not None and p.max is not None]
    out = []
    for nm in names:
        p = pm[nm]
        lo, hi = p.min, p.max
        if overrides and nm in overrides:
            lo, hi = (parse_si(x) for x in overrides[nm])
        if lo is None or hi is None or not hi > lo:
            raise ValueError(f"parameter {nm} needs a valid [min, max] range")
        log = p.log and lo > 0
        out.append(Variable(nm, float(lo), float(hi), log, p.integer))
    return out


# ------------------------------------------------------------------- GP / BO
def _matern52(X1, X2, ls, sf2):
    d = np.sqrt(np.maximum(((X1[:, None, :] - X2[None, :, :]) / ls) ** 2, 0).sum(-1))
    s5 = math.sqrt(5.0) * d
    return sf2 * (1 + s5 + s5 ** 2 / 3.0) * np.exp(-s5)


class GaussianProcess:
    def __init__(self, dim: int, seed: int = 0):
        self.dim = dim
        self.rng = np.random.default_rng(seed)
        self.theta = np.concatenate([np.log(np.full(dim, 0.3)), [0.0, math.log(1e-4)]])

    def _unpack(self, th):
        return np.exp(th[:self.dim]), math.exp(th[self.dim]), math.exp(th[self.dim + 1])

    def _nll(self, th, X, y):
        ls, sf2, sn2 = self._unpack(th)
        K = _matern52(X, X, ls, sf2) + (sn2 + 1e-10) * np.eye(len(X))
        try:
            L = np.linalg.cholesky(K)
        except np.linalg.LinAlgError:
            return 1e10
        a = np.linalg.solve(L.T, np.linalg.solve(L, y))
        return float(0.5 * y @ a + np.log(np.diag(L)).sum() + 0.5 * len(X) * math.log(2 * math.pi))

    def fit(self, X, y):
        from scipy.optimize import minimize
        self.X = np.asarray(X, float)
        self.ymean, self.ystd = float(np.mean(y)), float(np.std(y) or 1.0)
        self.y = (np.asarray(y, float) - self.ymean) / self.ystd
        bounds = [(math.log(0.03), math.log(3.0))] * self.dim + [(math.log(0.05), math.log(20.0)),
                                                                  (math.log(1e-6), math.log(0.2))]
        best = (self._nll(self.theta, self.X, self.y), self.theta)
        starts = [self.theta] + [np.array([self.rng.uniform(a, b) for a, b in bounds]) for _ in range(3)]
        for th0 in starts:
            try:
                r = minimize(self._nll, th0, args=(self.X, self.y), method="L-BFGS-B", bounds=bounds,
                             options={"maxiter": 100})
                if r.fun < best[0]:
                    best = (r.fun, r.x)
            except Exception:  # pragma: no cover - numerical trouble, keep previous
                continue
        self.theta = best[1]
        self._factor()

    def _factor(self):
        ls, sf2, sn2 = self._unpack(self.theta)
        K = _matern52(self.X, self.X, ls, sf2) + (sn2 + 1e-10) * np.eye(len(self.X))
        self.L = np.linalg.cholesky(K)
        self.alpha = np.linalg.solve(self.L.T, np.linalg.solve(self.L, self.y))

    def add_point(self, x, y_raw):
        """Append an observation keeping hyper-parameters (used for constant-liar batches)."""
        self.X = np.vstack([self.X, x])
        self.y = np.append(self.y, (y_raw - self.ymean) / self.ystd)
        self._factor()

    def predict(self, Xs):
        ls, sf2, _ = self._unpack(self.theta)
        Ks = _matern52(np.atleast_2d(Xs), self.X, ls, sf2)
        mu = Ks @ self.alpha
        v = np.linalg.solve(self.L, Ks.T)
        var = np.maximum(sf2 - (v ** 2).sum(0), 1e-12)
        return mu * self.ystd + self.ymean, np.sqrt(var) * self.ystd


def expected_improvement(mu, sigma, best, xi=0.01):
    from scipy.stats import norm
    imp = best - mu - xi * abs(best if best != 0 else 1.0)
    z = imp / sigma
    return imp * norm.cdf(z) + sigma * norm.pdf(z)


def _propose(gp: GaussianProcess, best: float, rng, x_best: np.ndarray | None = None,
             n_cand: int = 2000) -> np.ndarray:
    from scipy.optimize import minimize
    cand = rng.random((n_cand, gp.dim))
    if x_best is not None:  # local candidates around the incumbent (exploitation of small feasible regions)
        sig = rng.choice([0.02, 0.05, 0.1, 0.2], size=(n_cand, 1))
        local = np.clip(x_best + sig * rng.standard_normal((n_cand, gp.dim)), 0.0, 1.0)
        cand = np.vstack([cand, local])
    mu, sd = gp.predict(cand)
    ei = expected_improvement(mu, sd, best)
    top = cand[np.argsort(-ei)[:5]]
    best_x, best_ei = top[0], -np.inf
    for x0 in top:
        f = lambda x: -float(expected_improvement(*gp.predict(x[None, :]), best)[0])  # noqa: E731
        r = minimize(f, x0, method="L-BFGS-B", bounds=[(0, 1)] * gp.dim, options={"maxiter": 50})
        if -r.fun > best_ei:
            best_ei, best_x = -r.fun, np.clip(r.x, 0, 1)
    return best_x


def latin_hypercube(n: int, d: int, rng) -> np.ndarray:
    u = (rng.random((n, d)) + np.arange(n)[:, None]) / n
    for j in range(d):
        u[:, j] = u[rng.permutation(n), j]
    return u


# ------------------------------------------------------------------ driver
def run_optimization(design, variables: list[Variable], objective: Objective | str,
                     constraints: list[Constraint | str] | None = None, method: str = "bayes", budget: int = 30,
                     seed: int = 0, base_params: dict | None = None, backend="ngspice", pdk="sky130",
                     corner=None, temperature=27.0, vdd_scale=1.0, options=None, workers=None,
                     progress: Callable | None = None, cancel=None,
                     on_result: Callable[[int, Point, EvaluationResult], None] | None = None,
                     include_start: bool = True) -> OptimizationResult:
    from ..designs import get_design
    design = get_design(design) if isinstance(design, str) else design
    if isinstance(objective, str):
        objective = Objective.parse(objective)
    ns = dict(design.resolve(base_params))
    cons = [c if isinstance(c, Constraint) else Constraint.parse(c, ns) for c in (constraints or [])]
    out = OptimizationResult(method, objective)
    d = len(variables)
    if d == 0:
        raise ValueError("no optimization variables")
    rng = np.random.default_rng(seed)
    lock = threading.Lock()
    scale = {"value": None}
    is_cancelled = (cancel.is_set if hasattr(cancel, "is_set") else cancel) or (lambda: False)

    # starting point = current parameter values (design defaults + overrides), clipped into the box
    start_vals = dict(ns)
    x_start = np.clip(np.array([v.to_unit(start_vals[v.name]) if v.low <= start_vals[v.name] <= v.high
                                else 0.5 for v in variables]), 0.0, 1.0)

    def to_params(u) -> dict:
        p = dict(base_params or {})
        p.update({v.name: v.to_real(x) for v, x in zip(variables, u)})
        return p

    def cost_of(res: EvaluationResult) -> tuple[float, bool]:
        val = res.metrics.get(objective.metric, float("nan"))
        if not res.ok and not np.isfinite(val):
            return FAIL_COST, False
        if not np.isfinite(val):
            return FAIL_COST, False
        if scale["value"] is None:
            scale["value"] = max(abs(val), 1e-30)
        base = -val if objective.goal == "max" else val
        viol = [c.violation(res.metrics) for c in cons]
        if any(not np.isfinite(v) for v in viol):
            return FAIL_COST / 10 + base / scale["value"], False
        pen = sum(viol)  # exact (L1) penalty on relative violations: bounded, keeps the cost landscape smooth
        return base / scale["value"] + 10.0 * pen, pen == 0

    def evaluate_batch(U: np.ndarray) -> list[float]:
        if len(out.trials) >= budget or is_cancelled():
            return [FAIL_COST] * len(U)
        U = U[: max(0, budget - len(out.trials))]
        pts = [Point(to_params(u), corner, temperature, vdd_scale) for u in U]
        start = len(out.trials)

        def _cb(i, pt, res):
            if on_result:
                on_result(start + i, pt, res)

        results = evaluate_points(design, pts, backend, pdk, options, workers, None, cancel, _cb)
        costs = []
        with lock:
            for pt, res in zip(pts, results):
                c, feas = cost_of(res)
                tr = Trial(len(out.trials), {v.name: pt.params[v.name] for v in variables}, dict(res.metrics),
                           float(c), feas, res.ok)
                out.trials.append(tr)
                if out.best is None or (tr.feasible, -tr.cost) > (out.best.feasible, -out.best.cost):
                    out.best = tr
                costs.append(float(c))
                if progress:
                    progress(len(out.trials), budget, tr.index, tr)
        return costs

    method = method.lower()
    try:
        if method == "random":
            U = latin_hypercube(budget, d, rng)
            if include_start:
                U[0] = x_start
            evaluate_batch(U)
        elif method == "bayes":
            n_init = min(budget, max(2 * d, 6))
            U0 = latin_hypercube(n_init, d, rng)
            if include_start:
                U0[0] = x_start  # the current design is always among the initial points
            X, Y = list(U0), evaluate_batch(U0)
            batch = max(1, min(workers or 4, 4))
            while len(out.trials) < budget and not is_cancelled():
                Yc = np.array(Y, float)
                finite_max = np.max(Yc[Yc < FAIL_COST / 20]) if np.any(Yc < FAIL_COST / 20) else 1.0
                Yc = np.where(Yc >= FAIL_COST / 20, finite_max + abs(finite_max) + 1.0, Yc)  # tame failures
                Yc = np.minimum(Yc, np.percentile(Yc, 75))  # outliers must not dominate the GP scale
                gp = GaussianProcess(d, seed + len(X))
                gp.fit(np.array(X), Yc)
                best = float(np.min(Yc))
                x_best = np.array(X)[int(np.argmin(Yc))]
                prop = []
                for _ in range(min(batch, budget - len(out.trials))):
                    x = _propose(gp, best, rng, x_best)
                    prop.append(x)
                    gp.add_point(x, best)  # constant liar
                Yn = evaluate_batch(np.array(prop))
                X.extend(prop)
                Y.extend(Yn)
            out.message = "budget exhausted"
        elif method == "de":
            from scipy.optimize import differential_evolution
            popsize = max(4, min(10, budget // (2 * d) or 1))

            def f_vec(Ux):
                U = np.atleast_2d(np.asarray(Ux).T) if np.ndim(Ux) == 2 else np.atleast_2d(Ux)
                return np.array(evaluate_batch(U))

            # scipy's vectorized mode passes an array of shape (d, S): the whole population at once,
            # which evaluate_batch simulates in parallel
            r = differential_evolution(f_vec, [(0, 1)] * d, seed=seed, popsize=popsize, polish=False,
                                       maxiter=max(1, budget // (popsize * d)), vectorized=True,
                                       updating="deferred", tol=0, callback=lambda *a, **k: len(out.trials) >= budget,
                                       x0=x_start if include_start else None)
            out.message = str(r.message)
        elif method in ("nelder-mead", "nm"):
            from scipy.optimize import minimize
            x0 = x_start
            r = minimize(lambda u: evaluate_batch(np.atleast_2d(np.clip(u, 0, 1)))[0], x0, method="Nelder-Mead",
                         bounds=[(0, 1)] * d, options={"maxfev": budget, "xatol": 1e-3, "fatol": 1e-4,
                                                        "initial_simplex": _simplex(x0)})
            out.message = str(r.message)
        else:
            raise ValueError(f"unknown optimization method {method!r} (bayes, de, nelder-mead, random)")
    except Cancelled:
        out.message = "cancelled"
    if is_cancelled() and not out.message:
        out.message = "cancelled"
    return out


def _simplex(x0: np.ndarray, step: float = 0.25) -> np.ndarray:
    pts = [x0]
    for i in range(len(x0)):
        x = x0.copy()
        x[i] = x[i] + step if x[i] + step <= 1 else x[i] - step
        pts.append(x)
    return np.array(pts)
