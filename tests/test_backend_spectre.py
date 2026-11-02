"""Spectre adapter: netlist/OCEAN syntax, PSF-ASCII parsing and the run path with a fake ``spectre``."""

import os
import stat
import sys
import textwrap

import numpy as np
import pytest

from analogforge.backends.base import get_backend
from analogforge.backends.spectre.netlist import render_netlist
from analogforge.backends.spectre.ocean import parse_measurements, parse_ocnprint
from analogforge.backends.spectre.psf import parse_psf_ascii, signal_key
from analogforge.core.analyses import AC, PSS, Noise, Op, PNoise, Tran
from analogforge.core.circuit import Circuit, Pulse, Sine
from analogforge.core.pdk import PDK
from analogforge.core.testbench import Testbench

PDK_DICT = {
    "name": "fake", "corners": ["tt", "ss"],
    "devices": {"nmos": {"pins": ["d", "g", "s", "b"], "spectre": "{name} ({d} {g} {s} {b}) nch w={w} l={l}",
                         "op_path": {"spectre": "{name}"}, "op_params": {"spectre": {"gm": "gm", "id": "ids"}}}},
    "models": {"spectre": {"include": __file__, "section": "{corner}"}},
}


def circuit():
    c = Circuit("demo")
    c.vsource("VDD", "vdd", dc=1.8)
    c.vsource("VIN", "in", dc=0.9, ac=1.0)
    c.vsource("VS", "s", waveform=Sine(0, 0.1, 1e6))
    c.isource("IK", "0", "out", waveform=Pulse(0, 1e-4, 1e-10, 1e-11, 1e-11, 1e-10, 1))
    c.resistor("R1", "vdd", "out", 1e3)
    c.capacitor("C1", "out", "0", 1e-12)
    c.inductor("L1", "vdd", "x", 1e-9)
    c.inductor("L2", "x", "out", 1e-9)
    c.mutual("K1", "L1", "L2", 0.6)
    c.nmos("M1", "out", "in", "0", "0", 1e-6, 0.15e-6)
    c.ics["out"] = 1.0
    return c


def test_spectre_netlist_syntax():
    pdk = PDK.from_dict(PDK_DICT)
    tb = Testbench("tb", circuit(), [Op("op"), AC("ac", 1, 1e9, 10), Tran("tran", 1e-6, 1e-9, tmax=1e-10),
                                     PSS("pss", 1e9, 10, 20e-9, "out", "0"), PNoise("pn", osc_p="out", osc_n="0"),
                                     Noise("noise", "out", "0", "VIN")], corner="ss", temperature=85,
                   save_op=["M1"])
    text = render_netlist(tb, pdk, ["M1:gm", "M1:ids"])
    lines = text.splitlines()
    assert "simulator lang=spectre" in lines and "global 0" in lines
    assert f'include "{__file__}" section=ss' in lines
    assert "VDD (vdd 0) vsource type=dc dc=1.8" in lines
    assert "VIN (in 0) vsource type=dc dc=0.9 mag=1" in lines
    assert any(l.startswith("VS (s 0) vsource type=sine sinedc=0 ampl=0.1 freq=1000000") for l in lines)
    assert any(l.startswith("IK (0 out) isource type=pulse val0=0 val1=0.0001") for l in lines)
    assert "R1 (vdd out) resistor r=1000" in lines and "K1 mutual_inductor coupling=0.6 ind1=L1 ind2=L2" in lines
    assert "M1 (out in 0 0) nch w=1e-06 l=1.5e-07" in lines
    assert "ic out=1" in lines
    assert any(l.startswith("simulatorOptions options temp=85") for l in lines)
    assert "save M1:gm M1:ids" in lines
    assert "op dc oppoint=rawfile maxiters=150 maxsteps=10000" in lines
    assert "ac ac start=1 stop=1000000000 dec=10" in lines
    assert "tran tran stop=1e-06 step=1e-09 maxstep=1e-10 errpreset=conservative" in lines
    assert "pss (out 0) pss fund=1000000000 harms=10 tstab=2e-08 errpreset=conservative" in lines
    assert any(l.startswith("pn (out 0) pnoise start=1000") and "noisetype=timeaverage" in l for l in lines)
    assert "noise (out 0) noise start=1 stop=1000000000 dec=10 iprobe=VIN" in lines


def test_ocean_script_modes(tmp_path):
    pdk = PDK.from_dict(PDK_DICT)
    tb = Testbench("tb", circuit(), [Op("op"), Tran("tran", 1e-6, 1e-9)], corner="tt",
                   variables={"IBIAS": 1e-3}, probes=["v(out)", "i(VDD)"])
    be = get_backend("spectre")
    files = be.write_inputs(tb, pdk, tmp_path)
    assert [f.name for f in files] == ["tb.scs", "tb_circuit.scs", "tb.ocn"]
    circ_only = files[1].read_text()
    assert "include" not in circ_only and " tran " not in circ_only
    ocn = files[2].read_text()
    assert "simulator( 'spectre )" in ocn and f'design( "{files[1]}" )' in ocn
    assert f'modelFile( ("{__file__}" "tt") )' in ocn
    assert "desVar" not in ocn  # netlist mode: values are already numbers
    assert "analysis( 'tran ?stop \"1e-06\"" in ocn and "run()" in ocn
    assert 'ocnPrint( ?output' in ocn and 'VT("/out")' in ocn and 'IT("/VDD/PLUS")' in ocn
    cv = get_backend("spectre", mode="ocean", cellview=("myLib", "vco_tb", "schematic"))
    ocn2 = cv.ocean_script(tb, pdk, tmp_path)
    assert 'design( "myLib" "vco_tb" "schematic" )' in ocn2 and 'desVar( "IBIAS" 0.001 )' in ocn2


PSF_TRAN = textwrap.dedent('''\
    HEADER
    "PSFversion" "1.00"
    "simulator" "spectre"
    "analysis type" "tran"
    TYPE
    "sweep" FLOAT DOUBLE PROP(
    "key" "sweep"
    )
    "V" FLOAT DOUBLE PROP(
    "key" "node"
    "units" "V"
    )
    "I" FLOAT DOUBLE PROP(
    "key" "branch"
    "units" "A"
    )
    SWEEP
    "time" "sweep" PROP(
    "key" "sweep"
    )
    TRACE
    "out" "V"
    "VDD:p" "I"
    VALUE
    "time" 0.000000000000000e+00
    "out" 1.000000000000000e+00
    "VDD:p" -1.0e-03
    "time" 1.000000000000000e-09
    "out" 1.100000000000000e+00
    "VDD:p" -1.1e-03
    END
    ''')

PSF_AC = textwrap.dedent('''\
    HEADER
    "PSFversion" "1.00"
    TYPE
    "sweep" FLOAT DOUBLE
    "V" COMPLEX DOUBLE PROP(
    "key" "node"
    )
    SWEEP
    "freq" "sweep"
    TRACE
    "out" "V"
    VALUE
    "freq" 1.0
    "out" (10.0 -1.0)
    "freq" 10.0
    "out" (5.0 -5.0)
    END
    ''')

PSF_DC = textwrap.dedent('''\
    HEADER
    "PSFversion" "1.00"
    TYPE
    "V" FLOAT DOUBLE PROP(
    "key" "node"
    )
    "bsim4" STRUCT(
    "gm" FLOAT DOUBLE PROP( "units" "S" )
    "ids" FLOAT DOUBLE
    ) PROP( "key" "instance" )
    VALUE
    "out" "V" 0.9
    "M1" "bsim4" (1.5e-3 2.0e-4)
    "M1:gm" "V" 1.5e-3
    END
    ''')


def test_psf_parsing():
    tr = parse_psf_ascii(PSF_TRAN)
    assert tr.sweep_name == "time"
    np.testing.assert_allclose(tr.sweep, [0, 1e-9])
    np.testing.assert_allclose(tr.signals["out"], [1.0, 1.1])
    ac = parse_psf_ascii(PSF_AC)
    np.testing.assert_allclose(ac.signals["out"], [10 - 1j, 5 - 5j])
    dc = parse_psf_ascii(PSF_DC)
    assert dc.scalars["out"] == 0.9 and dc.scalars["M1:gm"] == 1.5e-3 and dc.scalars["M1:ids"] == 2e-4
    assert signal_key("VDD:p") == "i(vdd)" and signal_key("out", "V") == "v(out)"
    assert signal_key("M1:gm") == "m1:gm"


FAKE_SPECTRE = textwrap.dedent('''\
    #!{python}
    """Fake spectre: writes PSF-ASCII results for the analyses found in the netlist."""
    import re, sys, pathlib
    args = sys.argv[1:]
    net = pathlib.Path(args[0]).read_text()
    raw = pathlib.Path(args[args.index("-raw") + 1]); raw.mkdir(parents=True, exist_ok=True)
    tran = {tran!r}
    dc = {dc!r}
    for line in net.splitlines():
        m = re.match(r"^(\\w+) tran ", line)
        if m: (raw / (m.group(1) + ".tran.tran")).write_text(tran)
        m = re.match(r"^(\\w+) dc oppoint", line)
        if m: (raw / (m.group(1) + ".dc")).write_text(dc)
    print("spectre (fake) completes with 0 errors")
    ''')


def test_spectre_run_with_fake_executable(tmp_path, monkeypatch):
    exe = tmp_path / "spectre"
    exe.write_text(FAKE_SPECTRE.format(python=sys.executable, tran=PSF_TRAN, dc=PSF_DC))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    pdk = PDK.from_dict(PDK_DICT)
    be = get_backend("spectre")
    assert be.available()[0]
    tb = Testbench("tb", circuit(), [Op("op"), Tran("tran", 1e-6, 1e-9)], save_op=["M1"])
    r = be.run(tb, pdk)
    assert r.ok, r.error
    np.testing.assert_allclose(r["tran"].v("out"), [1.0, 1.1])
    np.testing.assert_allclose(r["tran"].i("VDD"), [-1e-3, -1.1e-3])
    assert r["op"].scalar("v(out)") == 0.9
    assert r["op"].op("m1", "gm") == 1.5e-3


def test_ocean_output_parsers(tmp_path):
    (tmp_path / "m.txt").write_text("f_osc 3.370000000000e+09\npn_1e6 -1.19e+02\nbad x\n")
    m = parse_measurements(tmp_path / "m.txt")
    assert m["f_osc"] == 3.37e9 and m["pn_1e6"] == -119.0 and m["bad"] != m["bad"]
    (tmp_path / "w.txt").write_text('time (s)   VT("/out") (V)\n0 1.0\n1e-9 1.5\n2e-9 2.0\n')
    labels, arr = parse_ocnprint(tmp_path / "w.txt")
    assert arr.shape == (3, 2) and arr[2, 1] == 2.0
