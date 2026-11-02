"""ADS adapter: netlist syntax, variable substitution and the run path against a fake Keysight API."""

import sys
import types

import numpy as np
import pytest

from analogforge.backends.ads.netlist import render_netlist, substitute_variables
from analogforge.backends.base import get_backend
from analogforge.core.analyses import AC, PSS, Op, Tran
from analogforge.core.circuit import Circuit, Pulse
from analogforge.core.pdk import PDK
from analogforge.core.testbench import Testbench

PDK_DICT = {"name": "fake", "corners": ["tt"],
            "devices": {"nmos": {"pins": ["d", "g", "s", "b"], "ads": "nch:{name} {d} {g} {s} {b} w={w} l={l}"}},
            "models": {"ads": {"include": __file__}}}


def circuit():
    c = Circuit("demo")
    c.vsource("VDD", "vdd", dc=1.8)
    c.vsource("VIN", "in", dc=0.9, ac=1.0)
    c.isource("IK", "0", "outp", waveform=Pulse(0, 1e-4, 1e-10, 1e-11, 1e-11, 1e-10, 1))
    c.resistor("R1", "vdd", "outp", 300)
    c.capacitor("C1", "outp", "0", 1e-12)
    c.inductor("L1", "vdd", "outp", 2e-9)
    c.vcvs("E1", "o", "0", "in", "0", 10)
    c.nmos("M1", "outp", "in", "0", "0", 1e-6, 0.15e-6)
    return c


def test_ads_netlist_syntax():
    pdk = PDK.from_dict(PDK_DICT)
    tb = Testbench("tb", circuit(), [Op("op"), AC("ac", 1, 1e9, 10), Tran("tran", 1e-6, 1e-9, tmax=1e-10),
                                     PSS("hb", 3e9, 7, 20e-9, "outp", "outn")], variables={"IBIAS": 2e-3})
    lines = render_netlist(tb, pdk).splitlines()
    assert any(l.startswith("Options ResourceUsage=yes") and "Temp=27" in l for l in lines)
    assert f'#include "{__file__}"' in lines
    assert 'V_Source:VDD vdd 0 Type="V_DC" Vdc=1.8 SaveCurrent=1' in lines
    assert 'V_Source:VIN in 0 Type="V_AC" Vdc=0.9 Vac=polar(1,0) SaveCurrent=1' in lines
    assert any(l.startswith("ItPulse:IK 0 outp Ilow=0 Ihigh=0.0001") for l in lines)
    assert "R:R1 vdd outp R=300" in lines and "L:L1 vdd outp L=2e-09 R=0" in lines
    assert "VCVS:E1 in o 0 0 G=10" in lines
    assert "nch:M1 outp in 0 0 w=1e-06 l=1.5e-07" in lines
    assert "DC:AF_op" in lines and 'AC:AF_ac SweepVar="freq" Start=1 Stop=1000000000 Dec=10' in lines
    assert "Tran:AF_tran StartTime=0 StopTime=1e-06 MaxTimeStep=1e-10" in lines
    assert any(l.startswith("OscPort:AF_OSC outp outn") for l in lines)
    assert "HB:AF_hb Freq[1]=3000000000 Order[1]=7" in lines


def test_substitute_variables():
    net = "; ADS netlist\nIBIAS=1 mA\nWN=10u\nR:R1 a b R=50\n"
    out = substitute_variables(net, {"IBIAS": 2e-3, "VTUNE": 0.5})
    assert "IBIAS=0.002" in out and "WN=10u" in out and "VTUNE=0.5" in out and "R:R1 a b R=50" in out


class _Index(list):
    name = None


class _Frame:
    def __init__(self, index_name, x, cols):
        self.index = _Index(x)
        self.index.name = index_name
        self._cols = cols
        self.columns = list(cols)

    def __getitem__(self, k):
        return np.asarray(self._cols[k])


@pytest.fixture()
def fake_keysight(monkeypatch, tmp_path):
    calls = {}

    class CircuitSimulator:
        def run_netlist(self, netlist, output_dir):
            calls["netlist"] = netlist
            (tmp_path / "x").mkdir(exist_ok=True)
            import pathlib
            pathlib.Path(output_dir, "result.ds").write_text("fake dataset")

    class _Block:
        def __init__(self, frame):
            self.frame = frame

        def to_dataframe(self):
            return self.frame

    class _Dataset:
        varblock_names = ["AF_tran.Tran", "AF_ac.AC"]

        def __getitem__(self, name):
            if "tran" in name.lower():
                return _Block(_Frame("time", [0, 1e-9, 2e-9], {"outp": [1.0, 1.2, 1.4], "VDD.i": [-1e-3] * 3}))
            return _Block(_Frame("freq", [1.0, 10.0], {"outp": [2 + 0j, 1 - 1j]}))

    keysight = types.ModuleType("keysight")
    eda = types.ModuleType("keysight.edatoolbox")
    ads = types.ModuleType("keysight.edatoolbox.ads")
    ads.CircuitSimulator = CircuitSimulator
    kads = types.ModuleType("keysight.ads")
    dataset = types.ModuleType("keysight.ads.dataset")
    dataset.open = lambda path: _Dataset()
    eda.ads = ads
    kads.dataset = dataset
    keysight.edatoolbox, keysight.ads = eda, kads
    for name, mod in {"keysight": keysight, "keysight.edatoolbox": eda, "keysight.edatoolbox.ads": ads,
                      "keysight.ads": kads, "keysight.ads.dataset": dataset}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return calls


def test_ads_run_with_fake_api(fake_keysight):
    pdk = PDK.from_dict(PDK_DICT)
    be = get_backend("ads")
    assert be.available()[0]
    tb = Testbench("tb", circuit(), [Tran("tran", 2e-9, 1e-9), AC("ac", 1, 10, 1)])
    r = be.run(tb, pdk)
    assert r.ok, r.error
    assert "Tran:AF_tran" in fake_keysight["netlist"]
    np.testing.assert_allclose(r["tran"].v("outp"), [1.0, 1.2, 1.4])
    np.testing.assert_allclose(r["tran"].i("VDD"), [-1e-3] * 3)
    np.testing.assert_allclose(r["ac"].v("outp"), [2, 1 - 1j])
    assert r["ac"].sweep_name == "freq"


def test_ads_unavailable_reports_reason():
    if "keysight" in sys.modules:
        pytest.skip("keysight importable")
    be = get_backend("ads")
    ok, why = be.available()
    assert not ok and "Keysight ADS Python API" in why
    r = be.run(Testbench("tb", circuit(), [Op("op")]), PDK.from_dict(PDK_DICT))
    assert not r.ok and r.netlist.startswith("; AnalogForge testbench")
