#!/usr/bin/env python3
"""Self-test of the ADS adapter - run with the Python interpreter that ships with ADS:

    $HPEESOF_DIR/tools/python/bin/python3 scripts/ads_selftest.py

(adjust the path to your ADS installation; on Windows use the python.exe under
``<ADS install>\\tools\\python``).  It simulates a passive RC network through
``keysight.edatoolbox.ads.CircuitSimulator`` and reads the dataset with
``keysight.ads.dataset``, then compares with the analytic answers.  If the
netlist syntax or the dataset naming of your ADS version differ, the printed
netlist / block names tell you what to adapt in
``analogforge/backends/ads/netlist.py`` or ``api.py``.
"""

from __future__ import annotations

import math
import sys

import numpy as np

from analogforge.backends.ads import api
from analogforge.backends.ads.netlist import render_netlist
from analogforge.backends.base import get_backend
from analogforge.core.analyses import AC, Tran
from analogforge.core.circuit import Circuit, Pulse
from analogforge.core.pdk import PDK
from analogforge.core.testbench import Testbench
from analogforge.metrics.amplifier import bandwidth_3db

R, C = 1e3, 1e-9
F3 = 1 / (2 * math.pi * R * C)


def main() -> int:
    ok, why = api.api_available()
    print("Keysight API:", ok, why)
    if not ok:
        return 1
    c = Circuit("rc_selftest")
    c.vsource("VIN", "vin", dc=0.0, ac=1.0)
    c.resistor("R1", "vin", "vout", R)
    c.capacitor("C1", "vout", "0", C)
    pdk = PDK.from_dict({"name": "none", "corners": ["tt"], "models": {"ads": {"include": []}}})
    tb = Testbench("rc", c, [AC("ac", 1e3, 1e8, 20)])
    print("---- generated ADS netlist ----\n" + render_netlist(tb, pdk))
    be = get_backend("ads", keep_files=True)
    r = be.run(tb, pdk)
    print("result ok:", r.ok, "error:", r.error)
    print("datasets:", {k: sorted(v.signals) for k, v in r.datasets.items()})
    passed = False
    if "ac" in r.datasets and r["ac"].has("v(vout)"):
        f3 = bandwidth_3db(r["ac"].sweep, r["ac"].v("vout"))
        passed = abs(f3 / F3 - 1) < 0.01
        print(f"AC f-3dB = {f3:.6g} Hz (expected {F3:.6g})  {'PASS' if passed else 'FAIL'}")
    # transient with a pulse source
    c2 = Circuit("rc_tran")
    c2.vsource("VIN", "vin", waveform=Pulse(0, 1, 1e-6, 1e-9, 1e-9, 1, 2))
    c2.resistor("R1", "vin", "vout", R)
    c2.capacitor("C1", "vout", "0", C)
    r2 = be.run(Testbench("rct", c2, [Tran("tran", 10e-6, 10e-9)]), pdk)
    if "tran" in r2.datasets and r2["tran"].has("v(vout)"):
        tr = r2["tran"]
        v = float(np.interp(1e-6 + R * C, tr.sweep, np.real(tr.v("vout"))))
        good = abs(v - (1 - math.exp(-1))) < 0.02
        passed &= good
        print(f"TRAN v(vout) one tau after the step = {v:.4f} (expected 0.632)  {'PASS' if good else 'FAIL'}")
    else:
        passed = False
        print("TRAN failed:", r2.error)
    print("\nSELF-TEST", "PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
