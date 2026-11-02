#!/usr/bin/env python3
"""Self-test of the Spectre adapter - run this FIRST on a Cadence machine.

    python scripts/spectre_selftest.py            # direct mode (needs `spectre` on PATH)
    python scripts/spectre_selftest.py --ocean    # additionally tests OCEAN mode (needs `ocean`)

It simulates a passive RC network (no PDK required) with AC and transient
analyses, parses the results through AnalogForge and compares them with the
analytic answers.  Input and output files are kept in ./af_spectre_selftest
so problems can be inspected (or attached to a bug report).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

from analogforge.backends.base import get_backend
from analogforge.core.analyses import AC, Op, Tran
from analogforge.core.circuit import Circuit, Pulse
from analogforge.core.pdk import PDK
from analogforge.core.testbench import Testbench
from analogforge.metrics.amplifier import bandwidth_3db

R, C = 1e3, 1e-9
F3 = 1 / (2 * math.pi * R * C)


def testbench() -> Testbench:
    c = Circuit("rc_selftest")
    c.vsource("VIN", "in", dc=0.0, ac=1.0, waveform=Pulse(0, 1, 1e-6, 1e-9, 1e-9, 1, 2))
    c.resistor("R1", "in", "out", R)
    c.capacitor("C1", "out", "0", C)
    return Testbench("rc", c, [Op("op"), AC("ac", 1e3, 1e8, 20), Tran("tran", 10e-6, 10e-9)],
                     probes=["v(out)", "v(in)"])


def check(r, label: str) -> bool:
    ok = True
    print(f"--- {label}: ok={r.ok} error={r.error!r}")
    if "ac" in r.datasets:
        f3 = bandwidth_3db(r["ac"].sweep, r["ac"].v("out"))
        good = abs(f3 / F3 - 1) < 0.01
        ok &= good
        print(f"  AC  f-3dB = {f3:.6g} Hz (expected {F3:.6g})  {'PASS' if good else 'FAIL'}")
    else:
        ok = False
        print("  AC  missing")
    if "tran" in r.datasets:
        tr = r["tran"]
        v = float(np.interp(1e-6 + R * C, tr.sweep, np.real(tr.v("out"))))
        good = abs(v - (1 - math.exp(-1))) < 0.02
        ok &= good
        print(f"  TRAN v(out) one tau after the step = {v:.4f} (expected 0.632)  {'PASS' if good else 'FAIL'}")
    else:
        ok = False
        print("  TRAN missing")
    if not r.ok:
        print("  log tail:\n" + "\n".join("    " + line for line in r.log.splitlines()[-25:]))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocean", action="store_true", help="also test OCEAN mode")
    ap.add_argument("--format", default="psfascii", help="psfascii (default) or nutascii")
    args = ap.parse_args()
    pdk = PDK.from_dict({"name": "none", "corners": ["tt"], "models": {"spectre": {"include": []}}})
    out = Path("af_spectre_selftest")
    out.mkdir(exist_ok=True)
    tb = testbench()
    be = get_backend("spectre", output_format=args.format, keep_files=True)
    print("spectre:", be.available(), "version:", be.version())
    for f in be.write_inputs(tb, pdk, out):
        print("wrote", f)
    results = [check(be.run(tb, pdk), f"direct mode ({args.format})")]
    if args.ocean:
        bo = get_backend("spectre", mode="ocean", keep_files=True)
        print("ocean:", bo.available())
        results.append(check(bo.run(tb, pdk), "OCEAN mode"))
    passed = all(results)
    print("\nSELF-TEST", "PASSED" if passed else "FAILED",
          "- result directories are kept under /tmp/afspectre_* (keep_files=True)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
