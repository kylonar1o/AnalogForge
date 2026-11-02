import math

import pytest

from analogforge.core.circuit import Circuit, CircuitError, Inductor, Resistor
from analogforge.core.pdk import PDK, PDKError
from analogforge.pdks import get_pdk, list_pdks


def test_circuit_helpers_and_validation():
    c = Circuit("t")
    c.vsource("VDD", "vdd", dc=1.8)
    c.lossy_inductor("L1", "vdd", "out", 2e-9, q=10, f0=3e9)
    c.capacitor("C1", "out", "0", 1e-12)
    rs = c["L1_rs"]
    assert isinstance(c["L1"], Inductor) and isinstance(rs, Resistor)
    assert rs.r == pytest.approx(2 * math.pi * 3e9 * 2e-9 / 10)
    assert c.validate() == []
    with pytest.raises(CircuitError):
        c.resistor("l1", "a", "b", 1)  # names are case-insensitive


def test_validation_warnings_and_errors():
    c = Circuit("t")
    c.resistor("R1", "a", "b", 1e3)
    warnings = c.validate()
    assert any("ground" in w for w in warnings) and any("'a'" in w for w in warnings)
    c2 = Circuit("t2")
    c2.inductor("L1", "a", "0", 1e-9)
    c2.mutual("K1", "L1", "LX", 0.5)
    with pytest.raises(CircuitError):
        c2.validate()


def test_servo_elements():
    c = Circuit("s")
    c.add_servo("SV", "out", "ref", "g")
    names = {e.name for e in c.elements}
    assert {"ESV", "LSV", "CSV"} <= names
    assert c["LSV"].l == 1e9 and c["CSV"].c == 1.0


def test_builtin_profiles():
    assert {"sky130", "template_institute"} <= set(list_pdks())
    pdk = get_pdk("sky130")
    assert pdk.corners == ["tt", "ff", "ss", "sf", "fs"]
    c = Circuit("x")
    dev = c.nmos("M1", "d", "g", "s", "b", w=2e-6, l=0.15e-6)
    line = pdk.render_device("ngspice", dev)
    assert line.startswith("XM1 d g s b sky130_fd_pr__nfet_01v8 W=2 L=0.15 nf=1")
    assert "ad=0.58" in line
    var = c.varactor("V1", "outp", "vt", 2e-6, 0.18e-6, m=10)
    assert pdk.render_device("ngspice", var) == "XV1 outp vt 0 sky130_fd_pr__cap_var_lvt w=2 l=0.18 vm=10"
    assert pdk.op_path("ngspice", dev) == "m.xM1.msky130_fd_pr__nfet_01v8"
    with pytest.raises(PDKError):
        pdk.check_corner("xx")


def test_template_profile_reports_placeholders():
    pdk = get_pdk("template_institute")
    ok, why = pdk.available("spectre")
    assert not ok and "placeholder" in why
    ok, why = pdk.available("ngspice")
    assert not ok


def test_profile_from_dict_and_missing_device():
    pdk = PDK.from_dict({"name": "mini", "devices": {"nmos": {"pins": ["d", "g", "s", "b"],
                                                               "ngspice": "M{name} {d} {g} {s} {b} n W={w}"}}})
    c = Circuit("x")
    pmos = c.pmos("M2", "d", "g", "s", "b", 1e-6, 1e-6)
    with pytest.raises(PDKError):
        pdk.render_device("ngspice", pmos)
    nmos = c.nmos("M1", "d", "g", "s", "b", 1e-6, 1e-6)
    with pytest.raises(PDKError):
        pdk.render_device("spectre", nmos)


def test_sky130_wrapper(sky130):
    sts = sky130.model_statements("ngspice", "ss", {"nmos", "varactor"})
    assert len(sts) == 1 and sts[0].kind == "include"
    text = open(sts[0].path).read()
    assert "nfet_01v8__ss.pm3.spice" in text and "pfet_01v8__ss.corner.spice" in text
    assert "cap_var_lvt" in text and ".option scale=1.0u" in text
