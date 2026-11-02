"""FastAPI end-to-end (submit -> progress -> results -> compare) and CLI commands."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from analogforge.cli import main
from analogforge.web.app import create_app


@pytest.fixture()
def client(tmp_path, sky130):
    app = create_app(data=str(tmp_path / "web"))
    with TestClient(app) as c:
        yield c


def _wait(client, job_id, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.5)
    raise TimeoutError(job_id)


def test_static_and_catalogue(client):
    assert client.get("/").status_code == 200 and "AnalogForge" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    js = client.get("/plotly.min.js")
    assert js.status_code in (200, 404)
    info = client.get("/api/info").json()
    assert {b["name"] for b in info["backends"]} == {"ngspice", "spectre", "ads"}
    assert "lc_vco" in info["designs"]
    d = client.get("/api/designs/lc_vco").json()
    assert d["kind"] == "oscillator"
    assert client.get("/api/designs/nope").status_code == 404


@pytest.mark.parametrize("backend,marker", [("ngspice", ".control"), ("spectre", "simulator lang=spectre"),
                                            ("ads", "Tran:AF_tran")])
def test_netlist_export(client, backend, marker, monkeypatch):
    monkeypatch.setenv("SKY130_SPECTRE_LIB", "/pdk/sky130.scs")
    monkeypatch.setenv("SKY130_ADS_LIB", "/pdk/sky130.net")
    r = client.post("/api/netlist", json={"design": "lc_vco", "backend": backend, "options": {"tuning_points": 0}})
    assert r.status_code == 200, r.text
    tbs = r.json()["testbenches"]
    assert tbs[0]["name"] == "tran" and marker in tbs[0]["netlist"]
    if backend == "spectre":
        assert "simulator( 'spectre )" in tbs[0]["ocean"]


def test_job_lifecycle_and_compare(client):
    r = client.post("/api/jobs", json={"kind": "sweep", "design": "two_stage_opamp", "options": {"tran": False},
                                       "sweep": {"variables": {"IBIAS": ["10u", "20u"]}}})
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done" and len(job["runs"]) == 2 and job["progress"] == 1
    ids = [run["id"] for run in job["runs"]]
    run = client.get(f"/api/runs/{ids[0]}").json()
    assert run["metrics"]["gain_db"] > 40 and any(c["name"] == "bode_mag" for c in run["curves"])
    cmp = client.get(f"/api/compare?ids={ids[0]},{ids[1]}").json()
    assert len(cmp["runs"]) == 2 and "ugbw" in cmp["metrics"]
    rep = client.get(f"/api/report/{job['id']}")
    assert rep.status_code == 200 and "AnalogForge report" in rep.text
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def test_bad_job_requests(client):
    assert client.post("/api/jobs", json={"kind": "simulate", "design": "nope"}).status_code == 400
    assert client.post("/api/jobs", json={"kind": "simulate", "design": "lc_vco", "backend": "ads"}).status_code == 400
    assert client.get("/api/compare?ids=a,b").status_code == 400


def test_cli_commands(capsys, tmp_path, sky130, monkeypatch):
    monkeypatch.setenv("ANALOGFORGE_DATA", str(tmp_path / "cli"))
    assert main(["designs"]) == 0
    assert "lc_vco" in capsys.readouterr().out
    assert main(["designs", "two_stage_opamp", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "two_stage_opamp"
    assert main(["netlist", "two_stage_opamp", "--backend", "spectre", "--out", str(tmp_path / "scs")]) == 0
    assert (tmp_path / "scs" / "ol.ocn").exists() and (tmp_path / "scs" / "ol.scs").exists()
    capsys.readouterr()
    assert main(["simulate", "rc_lowpass", "-p", "R=2k", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "done" and abs(out["metrics"]["error_pct"]) < 0.5
    assert main(["sweep", "rc_lowpass", "--var", "C=1p,2p", "--report", str(tmp_path / "rep")]) == 0
    assert (tmp_path / "rep" / "report.md").exists()
    assert main(["jobs"]) == 0 and "sweep" in capsys.readouterr().out
    assert main(["doctor"]) == 0
    assert main(["simulate", "rc_lowpass", "-p", "BAD=1"]) == 2
