import math

import pytest

from analogforge.expr import ExpressionError, evaluate, parse_constraint, render_template
from analogforge.units import format_si, parse_si, spice_num


@pytest.mark.parametrize("text,value", [
    ("10u", 10e-6), ("2.2k", 2200.0), ("1meg", 1e6), ("1M", 1e-3), ("3", 3.0), ("1e-9", 1e-9),
    ("5pF", 5e-12), ("-4.7n", -4.7e-9), ("100", 100.0), (".5", 0.5), ("2mil", 50.8e-6),
])
def test_parse_si(text, value):
    assert parse_si(text) == pytest.approx(value)


def test_parse_si_errors():
    with pytest.raises(ValueError):
        parse_si("abc")
    with pytest.raises(ValueError):
        parse_si(True)


def test_format_si():
    assert format_si(1.5e-6, "A") == "1.5 µA"
    assert format_si(3.37e9, "Hz") == "3.37 GHz"
    assert format_si(0, "V") == "0 V"
    assert format_si(float("nan")) == "nan"


def test_spice_num():
    assert spice_num(2.0) == "2"
    assert spice_num(1e-12) == "1e-12"
    assert float(spice_num(1.23456789012e-7)) == pytest.approx(1.23456789012e-7)


def test_expression_arithmetic_and_si():
    assert evaluate("W*2", {"W": 1e-6}) == pytest.approx(2e-6)
    assert evaluate("2n + 1n") == pytest.approx(3e-9)
    assert evaluate("1/(2*pi*sqrt(L*C))", {"L": 1e-9, "C": 1e-12}) == pytest.approx(
        1 / (2 * math.pi * math.sqrt(1e-21)))
    assert evaluate("a if a > b else b", {"a": 1, "b": 2}) == 2
    assert evaluate("db20(10)") == pytest.approx(20.0)


@pytest.mark.parametrize("bad", ["__import__('os')", "x.__class__", "[i for i in range(3)]", "open('f')",
                                 "(lambda: 1)()", "x[0]"])
def test_expression_rejects_unsafe(bad):
    with pytest.raises(ExpressionError):
        evaluate(bad, {"x": [1]})


def test_render_template():
    out = render_template("X{name} {d} W={w*1e6:.6g} nf={nf:.0f}", {"name": "M1", "d": "out", "w": 2e-6, "nf": 2})
    assert out == "XM1 out W=2 nf=2"


def test_parse_constraint():
    assert parse_constraint("phase_margin >= 60") == ("phase_margin", ">=", "60")
    with pytest.raises(ExpressionError):
        parse_constraint("nonsense")
