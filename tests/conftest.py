"""Shared fixtures.

Simulation tests need ngspice and the SKY130 models.  If the models are
missing they are installed once from PyPI (``analogforge pdk install sky130``);
set ``ANALOGFORGE_NO_AUTO_PDK=1`` to skip those tests instead.
"""

from __future__ import annotations

import os

import pytest

from analogforge.backends.ngspice.runner import find_ngspice
from analogforge.pdks.sky130 import find_sky130

HAVE_NGSPICE = find_ngspice() is not None


def _ensure_sky130() -> bool:
    if find_sky130() is not None:
        return True
    if os.environ.get("ANALOGFORGE_NO_AUTO_PDK"):
        return False
    try:
        from analogforge.pdks.sky130 import install_sky130
        install_sky130(quiet=True)
    except Exception:
        return False
    return find_sky130() is not None


@pytest.fixture(scope="session")
def ngspice():
    if not HAVE_NGSPICE:
        pytest.skip("ngspice not installed")
    return find_ngspice()


@pytest.fixture(scope="session")
def sky130(ngspice):
    if not _ensure_sky130():
        pytest.skip("SKY130 models not available (run `analogforge pdk install sky130`)")
    from analogforge.pdks import get_pdk
    return get_pdk("sky130")


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "afdata"
    monkeypatch.setenv("ANALOGFORGE_DATA", str(d))
    return d


@pytest.fixture(scope="session")
def vco_result(sky130):
    """One full LC-VCO evaluation (tuning curve + ISF phase noise + PSS), shared by several tests."""
    from analogforge.core.design import evaluate
    return evaluate("lc_vco", options={"tuning_points": 5, "phase_noise": "isf", "isf_points": 8, "pss": True})


@pytest.fixture(scope="session")
def opamp_result(sky130):
    from analogforge.core.design import evaluate
    return evaluate("two_stage_opamp")
