"""ngspice backend: deck rendering, real simulations, diagnostics and automatic retries."""

import math

import numpy as np
import pytest

from analogforge.backends.base import get_backend
from analogforge.backends.ngspice.diagnostics import diagnose, plan_fixes
from analogforge.backends.ngspice.runner import Expected, NgspiceRunner
from analogforge.core.analyses import AC, DCSweep, Noise, Op, PNoise, Tran, UnsupportedAnalysis
from analogforge.core.circuit import Circuit, Pulse
from analogforge.core.testbench import Testbench
from analogforge.pdks import get_pdk


class NoModels:
    """Wrap a PDK so passive test circuits load no model files."""

    def __init__(self, pdk):
        self._p = pdk

    def __getattr__(self, k):
        return getattr(self._p, k)

    def model_statements(self, *a):
        return []


def rc():
    c = Circuit("rc")
    c.vsource("VIN", "in", dc=0.0, ac=1.0, waveform=Pulse(0, 1, 1e-9, 1e-12, 1e-12, 1, 2))
    c.resistor("R1", "in", "out", 1e3)
    c.capacitor("C1", "out", "0", 1e-9)
    return c


def test_deck_rendering_without_simulator():
    pdk = get_pdk("sky130")
    be = get_backend("ngspice")
    c = rc()
    c.nmos("M1", "out", "in", "0", "0", 1e-6, 0.15e-6)
    tb = Testbench("tb", c, [Op("op"), Tran("tran", 1e-6, 1e-9, save=["M1:gm"])], save_op=["M1"], temperature=85)
    try:
        deck = be.netlist(tb, pdk)
    except Exception as exc:  # models missing -> loader error is acceptable here
        pytest.skip(str(exc))
    assert ".temp 85" in deck and "VIN in 0 DC 0 AC 1 0 PULSE(" in deck
    assert "write op.raw all @m.xm1.msky130_fd_pr__nfet_01v8[id]" in deck
    assert "save all @m.xm1.msky130_fd_pr__nfet_01v8[gm]" in deck
    with pytest.raises(UnsupportedAnalysis):
        be.check_supported(Testbench("x", c, [PNoise()]))


def test_rc_ac_tran_dc_noise(ngspice):
    pdk = NoModels(get_pdk("sky130"))
    be = get_backend("ngspice")
    tb = Testbench("rc", rc(), [Op("op"), AC("ac", 1e3, 1e8, 20), Tran("tran", 10e-6, 10e-9),
                                DCSweep("dc", "VIN", 0, 1, 0.5),
                                Noise("noise", "out", "0", "VIN", 1e3, 1e8, 5)], temperature=27)
    r = be.run(tb, pdk)
    assert r.ok, r.error
    from analogforge.metrics.amplifier import bandwidth_3db
    f3 = bandwidth_3db(r["ac"].sweep, r["ac"].v("out"))
    assert f3 == pytest.approx(1 / (2 * math.pi * 1e3 * 1e-9), rel=5e-3)
    tr = r["tran"]
    v_tau = np.interp(1e-9 + 1e-6, tr.sweep, tr.v("out"))  # one time constant after the step
    assert v_tau == pytest.approx(1 - math.exp(-1), abs=0.01)
    np.testing.assert_allclose(r["dc"].v("out"), [0, 0.5, 1.0], atol=1e-9)
    # thermal noise of R at low frequency: sqrt(4kTR)
    onoise = np.abs(r["noise"].get("onoise_spectrum"))
    assert onoise[0] == pytest.approx(math.sqrt(4 * 1.380649e-23 * 300.15 * 1e3), rel=0.02)
    assert r["op"].scalar("i(vin)") == pytest.approx(0.0, abs=1e-12)


def test_diagnose_messages():
    log = "Warning: singular matrix:  check node x\nError: Could not find include file foo.lib\n" \
          "doAnalyses: TRAN:  Timestep too small; time = 1e-9\n"
    cats = {d.category for d in diagnose(log)}
    assert {"singular_matrix", "missing_include", "tran_timestep"} <= cats
    assert plan_fixes(diagnose("Error: unknown subckt: x1"), ["op"]) == []
    names = [f.name for f in plan_fixes(diagnose("Timestep too small"), ["tran"])]
    assert names[0] == "gear_integration" and names[-1] == "loose_tolerances"


def test_runner_retries_with_fixes(monkeypatch, tmp_path, ngspice):
    """Simulate a convergence failure on the first attempt and check that a fix is applied."""
    runner = NgspiceRunner()
    calls = []

    def fake_exec(self, text, workdir, defines):
        calls.append(text)
        if len(calls) == 1:
            return "doAnalyses: TRAN:  Timestep too small\nrun simulation(s) aborted"
        # second attempt "succeeds": write a valid raw file
        hdr = ("Title: t\nDate: d\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: 2\n"
               "No. Points: 2\nVariables:\n\t0\ttime\ttime\n\t1\tv(a)\tvoltage\nBinary:\n").encode()
        (workdir / "tran.raw").write_bytes(hdr + np.array([0, 1, 1e-9, 2], "<f8").tobytes())
        return "ok"

    monkeypatch.setattr(NgspiceRunner, "_exec", fake_exec)
    out = runner.run(lambda extra: f"* deck\n.options {' '.join(f'{k}={v}' for k, v in extra.items())}\n",
                     [Expected("tran", "transient analysis")])
    assert out.ok and out.fixes_applied == ["gear_integration"]
    assert "method=gear" in calls[1] and len(out.attempts) == 2


def test_missing_model_is_reported(ngspice):
    pdk = NoModels(get_pdk("sky130"))
    c = Circuit("bad")
    c.vsource("V1", "a", dc=1.0)
    c.raw("M1", {"ngspice": "M1 a a 0 0 nosuchmodel W=1u L=1u"}, ["a", "0"])
    r = get_backend("ngspice").run(Testbench("bad", c, [Op("op")]), pdk)
    assert not r.ok
    assert any(d["category"] == "missing_model" for d in r.diagnoses)
    assert len(r.attempts) == 1  # deterministic error: no pointless retries
