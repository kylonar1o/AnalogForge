"""Sweeps, PVT, optimization, SQLite store, job runner and job manager."""

import math
import threading
import time

import numpy as np
import pytest

import analogforge.flows.optimize as O
from analogforge.core.design import Design, EvaluationResult, MetricSpec, ParamSpec
from analogforge.flows.executor import Cancelled, parallel_map
from analogforge.flows.job import run_job, validate_config
from analogforge.flows.sweep import make_points, parse_values
from analogforge.store import Store


def test_parse_values_and_points():
    assert parse_values("1:3:3") == [1.0, 2.0, 3.0]
    assert parse_values({"start": "1m", "stop": "4m", "num": 4}) == pytest.approx([1e-3, 2e-3, 3e-3, 4e-3])
    assert parse_values({"start": 1, "stop": 100, "num": 3, "log": True}) == pytest.approx([1, 10, 100])
    assert parse_values(["tt", "ss"]) == ["tt", "ss"]
    pts = make_points({"IBIAS": [1e-3, 2e-3], "corner": ["tt", "ss"], "temperature": [27]}, {"WN": 1e-5})
    assert len(pts) == 4 and {p.corner for p in pts} == {"tt", "ss"} and all(p.params["WN"] == 1e-5 for p in pts)
    with pytest.raises(ValueError):
        make_points({"a": [1, 2], "b": [1]}, mode="zip")


def test_parallel_map_order_progress_cancel():
    seen = []
    out = parallel_map(lambda x: x * x, range(10), workers=3, progress=lambda d, t, i, r: seen.append(d))
    assert out == [x * x for x in range(10)] and seen[-1] == 10
    ev = threading.Event()

    def slow(x):
        time.sleep(0.05)
        if x == 2:
            ev.set()
        return x
    with pytest.raises(Cancelled):
        parallel_map(slow, range(50), workers=2, cancel=ev)


class Branin(Design):
    name = "branin"
    parameters = [ParamSpec("x", 0.5, -5, 10), ParamSpec("y", 0.5, 0, 15)]
    metrics = [MetricSpec("f", goal="min")]


def _fake_points(design, pts, *a, **k):
    out = []
    for i, p in enumerate(pts):
        x, y = p.params["x"], p.params["y"]
        f = (y - 5.1 / (4 * math.pi ** 2) * x ** 2 + 5 / math.pi * x - 6) ** 2 + 10 * (1 - 1 / (8 * math.pi)) * math.cos(x) + 10
        r = EvaluationResult("branin", "fake", "fake", "tt", 27, 1, p.params, metrics={"f": f, "x": x})
        out.append(r)
    return out


@pytest.mark.parametrize("method,tol", [("bayes", 0.05), ("nelder-mead", 0.1), ("de", 1.5), ("random", 5.0)])
def test_optimizers_on_branin(monkeypatch, method, tol):
    monkeypatch.setattr(O, "evaluate_points", _fake_points)
    d = Branin()
    r = O.run_optimization(d, O.variables_from_design(d), "min:f", [], method=method, budget=40, seed=1, workers=4)
    assert len(r.trials) <= 40
    assert r.best.metrics["f"] < 0.397887 + tol
    conv = r.summary()["convergence"]
    assert all(b <= a for a, b in zip(conv, conv[1:]))


def test_constrained_bayes(monkeypatch):
    monkeypatch.setattr(O, "evaluate_points", _fake_points)
    d = Branin()
    r = O.run_optimization(d, O.variables_from_design(d), "min:f", ["x >= 5"], "bayes", 40, 2, workers=4)
    assert r.best.feasible and r.best.params["x"] >= 5 and r.best.metrics["f"] < 0.5


def test_objective_constraint_parsing():
    assert O.Objective.parse("max:fom").goal == "max"
    with pytest.raises(ValueError):
        O.Objective.parse("fom")
    c = O.Constraint.parse("phase_margin >= 60")
    assert c.violation({"phase_margin": 70}) == 0 and c.violation({"phase_margin": 45}) == pytest.approx(0.25)
    assert c.violation({}) == float("inf")
    v = O.Variable("w", 1e-6, 1e-4, log=True)
    assert v.to_real(0.5) == pytest.approx(1e-5) and v.to_unit(1e-5) == pytest.approx(0.5)


def test_store_roundtrip(tmp_path):
    st = Store(tmp_path)
    jid = st.create_job("sweep", {"design": "x"}, "my job")
    r = EvaluationResult("x", "ngspice", "sky130", "tt", 27, 1.0, {"a": 1.0}, metrics={"m": 2.0, "bad": float("nan")})
    rid = st.add_run(jid, 0, r, {"a": 1.0})
    st.update_job(jid, status="done", summary={"n": 1, "v": float("inf")})
    job = st.get_job(jid)
    assert job["status"] == "done" and job["summary"] == {"n": 1, "v": None}
    run = st.get_run(rid)
    assert run["metrics"] == {"m": 2.0, "bad": None} and run["label"] == {"a": 1.0} and "curves" in run
    assert st.compare([rid])["metrics"] == ["bad", "m"]
    st.delete_job(jid)
    assert st.get_job(jid) is None and st.list_runs(jid) == []


def test_validate_config_errors(sky130):
    with pytest.raises(ValueError):
        validate_config({"kind": "magic", "design": "lc_vco"})
    with pytest.raises(Exception):
        validate_config({"design": "lc_vco", "params": {"NOPE": 1}})
    with pytest.raises(ValueError):
        validate_config({"kind": "sweep", "design": "lc_vco"})
    with pytest.raises(ValueError):
        validate_config({"design": "lc_vco", "backend": "ads"})  # not runnable here


def test_jobs_end_to_end(sky130, tmp_path):
    st = Store(tmp_path)
    fast = {"tran": False, "psrr": False}
    s = run_job({"kind": "sweep", "design": "two_stage_opamp", "options": fast,
                 "sweep": {"variables": {"CC": [0.5e-12, 1e-12, 2e-12]}}}, st)
    assert s["status"] == "done" and s["failed"] == 0
    runs = st.list_runs(s["job_id"])
    ugbw = [r["metrics"]["ugbw"] for r in sorted(runs, key=lambda r: r["label"]["CC"])]
    assert ugbw[0] > ugbw[1] > ugbw[2]
    s = run_job({"kind": "corners", "design": "two_stage_opamp", "options": fast,
                 "corners": {"process": ["tt", "ss", "ff"], "voltage": [1.0], "temperature": [27, 125]}}, st)
    assert s["status"] == "done" and s["points"] == 6
    wc = s["worst_case"]["ugbw"]
    assert wc["worst"] == wc["min"] and wc["worst_at"]["corner"] in ("tt", "ss", "ff")
    s = run_job({"kind": "optimize", "design": "two_stage_opamp", "options": fast,
                 "optimize": {"method": "bayes", "variables": ["W1", "CC"], "objective": "max:ugbw",
                              "constraints": ["phase_margin >= 60"], "budget": 10, "seed": 3}}, st)
    assert s["status"] == "done" and s["evaluations"] == 10 and s["best"]["feasible"]
    from analogforge.plots import write_report
    rep = write_report(st, s["job_id"], tmp_path / "rep")
    assert rep.exists() and (tmp_path / "rep" / "optimization.png").exists()


def test_job_manager_background_and_cancel(sky130, tmp_path):
    from analogforge.jobs import JobManager
    st = Store(tmp_path)
    jm = JobManager(st, 2)
    jid = jm.submit({"kind": "simulate", "design": "rc_lowpass"})
    jm.wait(jid, 120)
    assert st.get_job(jid)["status"] == "done"
    jid2 = jm.submit({"kind": "sweep", "design": "two_stage_opamp", "options": {"tran": False},
                      "sweep": {"variables": {"CC": {"start": "0.5p", "stop": "3p", "num": 40}}}, "workers": 1})
    time.sleep(1.0)
    jm.cancel(jid2)
    jm.wait(jid2, 300)
    job = st.get_job(jid2)
    assert job["status"] == "cancelled" and job["done"] < 40
    jm.shutdown()
