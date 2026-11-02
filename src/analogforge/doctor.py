"""Environment diagnostics (``analogforge doctor``)."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def run_doctor(quick: bool = True) -> dict:
    checks = []
    ok_py = sys.version_info >= (3, 10)
    checks.append(_check("python", "OK" if ok_py else "FAIL", sys.version.split()[0]))
    for mod, required in (("numpy", True), ("scipy", True), ("matplotlib", True), ("yaml", True),
                          ("fastapi", True), ("uvicorn", True), ("plotly", False)):
        try:
            m = importlib.import_module(mod)
            checks.append(_check(f"package {mod}", "OK", getattr(m, "__version__", "")))
        except ImportError as exc:
            checks.append(_check(f"package {mod}", "FAIL" if required else "WARN", str(exc)))

    from .backends.base import get_backend
    ng = get_backend("ngspice")
    ok, why = ng.available()
    checks.append(_check("ngspice", "OK" if ok else "FAIL", why))
    if ok:
        from .core.analyses import AC
        from .core.circuit import Circuit
        from .core.testbench import Testbench
        from .metrics.amplifier import bandwidth_3db
        from .pdks import get_pdk
        c = Circuit("doctor_rc")
        c.vsource("VIN", "in", ac=1.0)
        c.resistor("R1", "in", "out", 1e3)
        c.capacitor("C1", "out", "0", 1e-9)
        try:
            pdk = get_pdk("sky130")
            # a passive circuit needs no device models: run it without loading any model file
            r = ng.run(Testbench("rc", c, [AC("ac", 1e3, 1e9, 20)]), _NoModels(pdk))
            f3 = bandwidth_3db(r["ac"].sweep, r["ac"].v("out")) if r.ok else float("nan")
            good = abs(f3 / 159154.9 - 1) < 0.01
            checks.append(_check("ngspice RC smoke test", "OK" if good else "FAIL",
                                 f"f3dB = {f3:.6g} Hz (expected 159155 Hz)" if r.ok else r.error))
        except Exception as exc:  # pragma: no cover
            checks.append(_check("ngspice RC smoke test", "FAIL", f"{type(exc).__name__}: {exc}"))

    from .pdks.sky130 import availability
    ok_pdk, why_pdk = availability()
    checks.append(_check("SKY130 models", "OK" if ok_pdk else "WARN", why_pdk))
    if ok and ok_pdk and not quick:
        from .core.design import evaluate
        r = evaluate("lc_vco", options={"tuning_points": 0})
        f = r.metrics.get("f_osc", float("nan"))
        checks.append(_check("SKY130 LC-VCO smoke test", "OK" if r.ok and f == f else "FAIL",
                             f"f_osc = {f / 1e9:.4f} GHz" if r.ok else "; ".join(r.errors)))
    for name in ("spectre", "ads"):
        okb, whyb = get_backend(name).available()
        checks.append(_check(f"backend {name}", "OK" if okb else "INFO", whyb))
    try:
        from .store import data_dir
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=d):
            pass
        checks.append(_check("data directory", "OK", str(d)))
    except OSError as exc:
        checks.append(_check("data directory", "FAIL", str(exc)))
    checks.append(_check("parallel workers", "INFO", f"{os.cpu_count()} CPUs; ngspice OMP wait policy PASSIVE"))
    overall = "FAIL" if any(c["status"] == "FAIL" for c in checks) else (
        "WARN" if any(c["status"] == "WARN" for c in checks) else "OK")
    return {"overall": overall, "checks": checks}


class _NoModels:
    """PDK proxy that loads no model files (for the passive RC smoke test)."""

    def __init__(self, pdk):
        self._pdk = pdk

    def __getattr__(self, item):
        return getattr(self._pdk, item)

    def model_statements(self, backend, corner, kinds):
        return []
