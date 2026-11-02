"""Parameter sweep, PVT corners and Bayesian optimization through the Python API (results in SQLite)."""
from analogforge.flows.job import run_job
from analogforge.plots import write_report
from analogforge.store import Store

store = Store()   # ~/.analogforge or $ANALOGFORGE_DATA

sweep = run_job({"kind": "sweep", "design": "lc_vco", "options": {"tuning_points": 0},
                 "sweep": {"variables": {"IBIAS": {"start": "1m", "stop": "4m", "num": 4}}}}, store)
print("sweep", sweep["status"], write_report(store, sweep["job_id"], "out_sweep"))

pvt = run_job({"kind": "corners", "design": "two_stage_opamp", "options": {"tran": False},
               "corners": {"process": ["tt", "ff", "ss"], "voltage": [0.9, 1.1], "temperature": [-40, 125]}}, store)
print("worst-case UGBW:", pvt["worst_case"]["ugbw"])

opt = run_job({"kind": "optimize", "design": "two_stage_opamp", "options": {"tran": False, "psrr": False},
               "optimize": {"method": "bayes", "variables": ["W1", "W6", "CC", "IBIAS"], "objective": "max:ugbw",
                            "constraints": ["phase_margin >= 60", "gain_db >= 60", "power <= 0.4e-3"],
                            "budget": 24}}, store)
print("best:", opt["best"]["params"], {k: opt["best"]["metrics"][k] for k in ("ugbw", "phase_margin", "power")})
