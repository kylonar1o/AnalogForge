"""SPICE3 raw file reader (binary and ASCII) on synthetic files."""

import numpy as np
import pytest

from analogforge.backends.ngspice.rawfile import RawFileError, parse_raw_bytes


def _header(plot, flags, names, npts):
    lines = [f"Title: test", "Date: now", f"Plotname: {plot}", f"Flags: {flags}",
             f"No. Variables: {len(names)}", f"No. Points: {npts}", "Variables:"]
    lines += [f"\t{i}\t{n}\t{t}" for i, (n, t) in enumerate(names)]
    return ("\n".join(lines) + "\n").encode()


def test_binary_real():
    t = np.linspace(0, 1e-9, 5)
    v = np.sin(t * 1e9)
    data = np.column_stack([t, v]).astype("<f8").tobytes()
    blob = _header("Transient Analysis", "real", [("time", "time"), ("v(out)", "voltage")], 5) + b"Binary:\n" + data
    (plot,) = parse_raw_bytes(blob)
    assert plot.plotname == "Transient Analysis"
    np.testing.assert_allclose(plot.get("v(out)"), v)
    np.testing.assert_allclose(plot.get("out"), v)


def test_binary_complex_and_multiple_plots():
    fr = np.array([1.0, 10.0, 100.0])
    h = np.array([1 + 1j, 0.5 - 0.5j, 0.1j])
    arr = np.zeros((3, 2, 2))
    arr[:, 0, 0] = fr
    arr[:, 1, 0], arr[:, 1, 1] = h.real, h.imag
    blob1 = _header("AC Analysis", "complex", [("frequency", "frequency"), ("v(out)", "voltage")], 3) \
        + b"Binary:\n" + arr.astype("<f8").tobytes()
    blob2 = _header("Operating Point", "real", [("v(a)", "voltage")], 1) + b"Binary:\n" + np.array([1.5]).tobytes()
    plots = parse_raw_bytes(blob1 + blob2)
    assert [p.plotname for p in plots] == ["AC Analysis", "Operating Point"]
    np.testing.assert_allclose(plots[0].get("v(out)"), h)
    assert plots[1].scalar("v(a)") == 1.5


def test_ascii():
    body = b"Values:\n0\t0.0\n\t1.0\n1\t1e-9\n\t2.0\n"
    blob = _header("Transient Analysis", "real", [("time", "time"), ("v(x)", "voltage")], 2) + body
    (plot,) = parse_raw_bytes(blob)
    np.testing.assert_allclose(plot.get("v(x)"), [1.0, 2.0])


def test_truncated_binary_uses_available_points():
    data = np.array([0.0, 1.0, 1.0, 2.0, 2.0]).astype("<f8").tobytes()  # 2.5 records of 2 vars
    blob = _header("Transient Analysis", "real", [("time", "time"), ("v(x)", "voltage")], 10) + b"Binary:\n" + data
    (plot,) = parse_raw_bytes(blob)
    assert len(plot.scale) == 2


def test_bad_file():
    with pytest.raises(RawFileError):
        parse_raw_bytes(b"Title: x\nPlotname: y\n")
