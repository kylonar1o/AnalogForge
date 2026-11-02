"""FastAPI backend + single-page web UI.

Start with ``analogforge serve`` (default http://127.0.0.1:8000).  The UI is a
static page (``static/``) talking to the JSON API below; Plotly is served
from the installed ``plotly`` Python package, so no internet access is needed.
"""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..backends.base import get_backend, list_backends
from ..core.design import DesignContext
from ..designs import get_design, list_designs
from ..jobs import JobManager
from ..pdks import get_pdk, list_pdks
from ..store import Store, _clean

STATIC = Path(__file__).parent / "static"


class NetlistRequest(BaseModel):
    design: str
    backend: str = "ngspice"
    pdk: str = "sky130"
    corner: str | None = None
    temperature: float = 27.0
    vdd_scale: float = 1.0
    params: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class JobRequest(BaseModel):
    kind: str = "simulate"
    design: str
    name: str = ""
    backend: str = "ngspice"
    pdk: str = "sky130"
    corner: str | None = None
    temperature: float = 27.0
    vdd_scale: float = 1.0
    params: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    workers: int | None = None
    sweep: dict | None = None
    corners: dict | None = None
    optimize: dict | None = None


def _plotly_js() -> Path | None:
    spec = importlib.util.find_spec("plotly")
    if spec is None or not spec.submodule_search_locations:
        return None
    for loc in spec.submodule_search_locations:
        p = Path(loc) / "package_data" / "plotly.min.js"
        if p.is_file():
            return p
    return None


def create_app(data: str | None = None, max_jobs: int = 2) -> FastAPI:
    store = Store(data)
    manager = JobManager(store, max_jobs)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        manager.shutdown()

    app = FastAPI(title="AnalogForge", version=__version__, lifespan=lifespan,
                  description="Simulator-agnostic analog/RF IC design automation")
    app.state.store = store
    app.state.jobs = manager

    # ------------------------------------------------------------ catalogue
    @app.get("/api/info")
    def info():
        backends = [get_backend(b).info() for b in list_backends()]
        pdks = []
        for name in list_pdks():
            try:
                p = get_pdk(name)
                pdks.append({"name": name, "description": p.description, "corners": p.corners,
                             "temperatures": p.temperatures, "nominal_vdd": p.nominal_vdd,
                             "backends": {b: dict(zip(("available", "status"), p.available(b))) for b in p.models}})
            except Exception as exc:  # pragma: no cover
                pdks.append({"name": name, "error": str(exc)})
        return {"version": __version__, "backends": backends, "pdks": pdks, "designs": list_designs(),
                "data_dir": str(store.root)}

    @app.get("/api/designs")
    def designs():
        out = []
        for n in list_designs():
            d = get_design(n)
            out.append({"name": d.name, "title": d.title, "kind": d.kind})
        return out

    @app.get("/api/designs/{name}")
    def design(name: str):
        try:
            return get_design(name).info()
        except Exception as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/netlist")
    def netlist(req: NetlistRequest):
        try:
            d = get_design(req.design)
            pdk = get_pdk(req.pdk)
            be = get_backend(req.backend)
            opts = dict(d.options)
            opts.update(req.options)
            corner = req.corner or pdk.default_corner
            ctx = DesignContext(pdk, be, corner, req.temperature, req.vdd_scale, opts)
            tbs = d.testbenches(d.resolve(req.params), ctx)
            out = []
            for tb in tbs:
                tb.corner, tb.temperature = corner, req.temperature
                item = {"name": tb.name, "description": tb.description, "netlist": be.netlist(tb, pdk),
                        "suffix": be.netlist_suffix}
                if be.name == "spectre":
                    item["ocean"] = be.ocean_script(tb, pdk, f"./{tb.name}_ocean", f"./{tb.name}_circuit.scs")
                out.append(item)
            return {"backend": be.name, "testbenches": out}
        except Exception as exc:
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------- jobs
    @app.post("/api/jobs")
    def submit(req: JobRequest):
        cfg = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        cfg = {k: v for k, v in cfg.items() if v is not None or k in ("corner",)}
        try:
            job_id = manager.submit(cfg)
        except Exception as exc:
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")
        return {"job_id": job_id}

    @app.get("/api/jobs")
    def jobs(limit: int = 100):
        return JSONResponse(_clean(store.list_jobs(limit)))

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        j = store.get_job(job_id)
        if j is None:
            raise HTTPException(404, "job not found")
        j["runs"] = store.list_runs(job_id)
        return JSONResponse(_clean(j))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        if store.get_job(job_id) is None:
            raise HTTPException(404, "job not found")
        return {"cancelled": manager.cancel(job_id)}

    @app.delete("/api/jobs/{job_id}")
    def delete(job_id: str):
        j = store.get_job(job_id)
        if j is None:
            raise HTTPException(404, "job not found")
        if j["status"] in ("queued", "running"):
            raise HTTPException(409, "cancel the job first")
        store.delete_job(job_id)
        return {"deleted": job_id}

    @app.get("/api/runs/{run_id}")
    def run(run_id: int):
        r = store.get_run(run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return JSONResponse(_clean(r))

    @app.get("/api/compare")
    def compare(ids: str):
        try:
            id_list = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "ids must be a comma separated list of run ids")
        return JSONResponse(_clean(store.compare(id_list)))

    @app.get("/api/report/{job_id}", response_class=PlainTextResponse)
    def report(job_id: str):
        from ..plots import write_report
        try:
            path = write_report(store, job_id, store.root / "reports" / job_id)
        except KeyError:
            raise HTTPException(404, "job not found")
        return path.read_text()

    # --------------------------------------------------------------- static
    @app.get("/plotly.min.js")
    def plotly_js():
        p = _plotly_js()
        if p is None:
            raise HTTPException(404, "plotly package not installed (pip install plotly) - charts disabled")
        return FileResponse(p, media_type="application/javascript")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
