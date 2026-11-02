"""Run a job described by a plain dict (CLI YAML files, web API) and record it in the store.

Job config keys::

    kind: simulate | sweep | corners | optimize
    design: lc_vco            # built-in name, YAML design name or path
    backend: ngspice          # ngspice | spectre | ads
    backend_options: {}       # e.g. {mode: ocean, cellview: myLib/vco_tb/schematic} for Spectre
    pdk: sky130               # profile name or YAML path
    corner: tt
    temperature: 27
    vdd_scale: 1.0
    params: {IBIAS: 2m}       # overrides
    options: {phase_noise: isf}
    workers: 4
    sweep:    {variables: {VTUNE: {start: 0, stop: 1.8, num: 7}}, mode: grid}
    corners:  {process: [tt, ff, ss], voltage: [0.9, 1.0, 1.1], temperature: [-40, 27, 125]}
    optimize: {method: bayes, variables: {WN: [5u, 60u]} | [WN, IBIAS] | null,
               objective: "max:fom", constraints: ["f_osc >= 3e9"], budget: 30, seed: 0}
"""

from __future__ import annotations

import threading
import time
import traceback

from ..core.design import evaluate
from ..designs import get_design
from ..store import Store
from .executor import Cancelled, Point
from .optimize import Objective, run_optimization, variables_from_design
from .sweep import make_points, pvt_points, worst_case

KINDS = ("simulate", "sweep", "corners", "optimize")


def validate_config(cfg: dict) -> dict:
    cfg = dict(cfg)
    kind = cfg.get("kind", "simulate")
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}; expected one of {KINDS}")
    if "design" not in cfg:
        raise ValueError("job config needs a 'design'")
    design = get_design(cfg["design"])
    design.resolve(cfg.get("params"))  # raises for unknown parameters
    cfg.setdefault("backend", "ngspice")
    cfg.setdefault("pdk", "sky130")
    from ..backends.base import get_backend
    bopts = dict(cfg.get("backend_options") or {})
    if "cellview" in bopts and isinstance(bopts["cellview"], str):
        bopts["cellview"] = tuple(bopts["cellview"].split("/"))
    cfg["backend_options"] = bopts
    ok, why = get_backend(cfg["backend"], **bopts).available()
    if not ok:
        raise ValueError(f"backend {cfg['backend']!r} cannot run simulations here: {why}")
    cfg.setdefault("temperature", 27.0)
    cfg.setdefault("vdd_scale", 1.0)
    if kind == "sweep" and not (cfg.get("sweep") or {}).get("variables"):
        raise ValueError("sweep job needs sweep.variables")
    if kind == "optimize":
        opt = cfg.setdefault("optimize", {})
        opt.setdefault("objective", design.default_objective or "")
        if not opt["objective"]:
            raise ValueError("optimize job needs optimize.objective, e.g. 'max:fom'")
        Objective.parse(opt["objective"])
    return cfg


def run_job(cfg: dict, store: Store | None = None, job_id: str | None = None,
            cancel: threading.Event | None = None, progress=None) -> dict:
    """Execute a job synchronously.  Returns the job summary (also stored if ``store`` given)."""
    cfg = validate_config(cfg)
    kind = cfg["kind"] if "kind" in cfg else "simulate"
    design = get_design(cfg["design"])
    if store is not None and job_id is None:
        job_id = store.create_job(kind, cfg, cfg.get("name", ""))
    cancel = cancel or threading.Event()
    from ..backends.base import get_backend
    backend = get_backend(cfg["backend"], **(cfg.get("backend_options") or {}))
    common = dict(backend=backend, pdk=cfg["pdk"], options=cfg.get("options"))
    workers = cfg.get("workers")
    t0 = time.time()
    if store is not None:
        store.update_job(job_id, status="running", started=t0)

    def on_result(idx, pt: Point, res):
        if store is not None:
            store.add_run(job_id, idx, res, label=pt.label)

    def on_progress(done, total, *_):
        if store is not None:
            store.update_job(job_id, done=done, total=total, progress=done / max(total, 1))
        if progress:
            progress(done, total)

    summary: dict = {"kind": kind, "design": design.name}
    status, error = "done", None
    try:
        if kind == "simulate":
            pt = Point(cfg.get("params") or {}, cfg.get("corner"), float(cfg["temperature"]), float(cfg["vdd_scale"]))
            if store is not None:
                store.update_job(job_id, total=1)
            res = evaluate(design, pt.params, corner=pt.corner, temperature=pt.temperature,
                           vdd_scale=pt.vdd_scale, **common)
            on_result(0, pt, res)
            on_progress(1, 1)
            summary.update({"metrics": res.metrics, "ok": res.ok, "errors": res.errors, "warnings": res.warnings})
        elif kind == "sweep":
            sw = cfg["sweep"]
            pts = make_points(sw["variables"], cfg.get("params"), sw.get("mode", "grid"), cfg.get("corner"),
                              float(cfg["temperature"]), float(cfg["vdd_scale"]))
            from .executor import evaluate_points
            if store is not None:
                store.update_job(job_id, total=len(pts))
            res = evaluate_points(design, pts, workers=workers, progress=on_progress, cancel=cancel,
                                  on_result=on_result, **common)
            summary.update({"variables": list(sw["variables"]), "points": len(pts),
                            "failed": sum(not r.ok for r in res)})
        elif kind == "corners":
            from ..pdks import get_pdk
            from .executor import evaluate_points
            c = cfg.get("corners") or {}
            pdk = get_pdk(cfg["pdk"])
            pts = pvt_points(c.get("process") or pdk.corners, [float(v) for v in c.get("voltage") or [0.9, 1.0, 1.1]],
                             [float(t) for t in c.get("temperature") or pdk.temperatures], cfg.get("params"))
            if store is not None:
                store.update_job(job_id, total=len(pts))
            res = evaluate_points(design, pts, workers=workers, progress=on_progress, cancel=cancel,
                                  on_result=on_result, **common)
            summary.update({"points": len(pts), "failed": sum(not r.ok for r in res),
                            "worst_case": worst_case(design, res, pts)})
        else:  # optimize
            o = cfg["optimize"]
            var_spec = o.get("variables")
            if isinstance(var_spec, dict):
                variables = variables_from_design(design, list(var_spec), {k: v for k, v in var_spec.items() if v})
            else:
                variables = variables_from_design(design, var_spec)
            budget = int(o.get("budget", 30))
            if store is not None:
                store.update_job(job_id, total=budget)
            res = run_optimization(design, variables, o["objective"], o.get("constraints") or [],
                                   o.get("method", "bayes"), budget, int(o.get("seed", 0)), cfg.get("params"),
                                   corner=cfg.get("corner"), temperature=float(cfg["temperature"]),
                                   vdd_scale=float(cfg["vdd_scale"]), workers=workers, progress=on_progress,
                                   cancel=cancel, on_result=on_result, **common)
            summary.update(res.summary())
            summary["variables"] = [v.__dict__ for v in variables]
        if cancel.is_set():
            status = "cancelled"
    except Cancelled:
        status = "cancelled"
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc(limit=8)
    summary["elapsed_s"] = round(time.time() - t0, 3)
    if store is not None:
        store.update_job(job_id, status=status, finished=time.time(), summary=summary, error=error)
    summary["job_id"] = job_id
    summary["status"] = status
    if error:
        summary["error"] = error
    return summary
