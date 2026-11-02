"""Command-line interface: ``analogforge <command> ...`` (see ``analogforge -h``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .units import format_si


def _kv(items: list[str] | None) -> dict:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"expected NAME=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = _auto(v.strip())
    return out


def _auto(v: str):
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("none", "null"):
        return None
    return v


def _list(text: str | None, cast=str):
    if text is None:
        return None
    return [cast(x) for x in text.split(",") if x.strip()]


def _var_spec(text: str):
    """'1m:4m:7' (linear) | '1m:4m:7:log' | 'a,b,c'"""
    if ":" in text:
        parts = text.split(":")
        spec = {"start": parts[0], "stop": parts[1], "num": int(parts[2]) if len(parts) > 2 else 5}
        if len(parts) > 3 and parts[3] == "log":
            spec["log"] = True
        return spec
    return text.split(",")


def _print_metrics(design, metrics: dict) -> None:
    units = {m.name: m.unit for m in design.metrics}
    for k, v in metrics.items():
        unit = units.get(k, "")
        if unit in ("dB", "dBc/Hz", "deg", "%", "", "V/V"):
            txt = "n/a" if v is None or v != v else f"{v:.4g} {unit}".strip()
        else:
            txt = format_si(v, unit, 5)
        print(f"  {k:<20} {txt}")


def _store(args):
    from .store import Store
    return None if getattr(args, "no_store", False) else Store(getattr(args, "data", None))


def _base_cfg(args, kind: str) -> dict:
    return {"kind": kind, "design": args.design, "backend": args.backend, "pdk": args.pdk,
            "backend_options": _kv(args.backend_opt),
            "corner": args.corner, "temperature": args.temp, "vdd_scale": args.vdd_scale,
            "params": _kv(args.param), "options": _kv(args.option), "workers": args.workers}


def _finish(args, summary: dict, store) -> int:
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"job {summary.get('job_id')} -> {summary.get('status')} ({summary.get('elapsed_s')} s)")
        if summary.get("error"):
            print("error:", summary["error"])
    if store is not None and getattr(args, "report", None) and summary.get("job_id"):
        from .plots import write_report
        path = write_report(store, summary["job_id"], args.report)
        print(f"report written to {path}")
    return 0 if summary.get("status") == "done" else 1


# ---------------------------------------------------------------- commands
def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    report = run_doctor(quick=not args.full)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["checks"]:
            print(f"[{item['status']:4}] {item['name']:<28} {item['detail']}")
        print(f"\noverall: {report['overall']}")
    return 0 if report["overall"] != "FAIL" else 1


def cmd_pdk(args) -> int:
    from .pdks import get_pdk, list_pdks
    if args.action == "list":
        for name in list_pdks():
            try:
                p = get_pdk(name)
                avail = {b: p.available(b)[0] for b in p.models}
                print(f"{name:<22} {p.description}  {avail}")
            except Exception as exc:  # pragma: no cover
                print(f"{name:<22} (invalid: {exc})")
        return 0
    if args.action == "install":
        if args.name != "sky130":
            raise SystemExit("only 'sky130' can be installed automatically")
        from .pdks.sky130 import install_sky130
        install_sky130(wheel=args.wheel)
        return 0
    if args.action == "info":
        print(json.dumps(get_pdk(args.name).info(), indent=2))
        return 0
    return 1


def cmd_designs(args) -> int:
    from .designs import get_design, list_designs
    if args.name:
        info = get_design(args.name).info()
        if args.json:
            print(json.dumps(info, indent=2))
            return 0
        print(f"{info['name']}: {info['title']} [{info['kind']}]\n")
        print(info["description"][:1500])
        print("\nparameters:")
        for p in info["parameters"]:
            rng = f"[{format_si(p['min'], p['unit'])} .. {format_si(p['max'], p['unit'])}]" if p["min"] is not None else ""
            print(f"  {p['name']:<10} {format_si(p['default'], p['unit']):>12} {rng:<28} {p['description']}")
        print("\nmetrics:")
        for m in info["metrics"]:
            print(f"  {m['name']:<20} {m['unit']:<8} {m['description']}")
        print("\noptions:", info["options"])
        return 0
    for name in list_designs():
        d = get_design(name)
        print(f"{name:<20} {d.kind:<11} {d.title}")
    return 0


def _testbenches(args):
    from .backends.base import get_backend
    from .core.design import DesignContext
    from .designs import get_design
    from .pdks import get_pdk
    design = get_design(args.design)
    pdk = get_pdk(args.pdk)
    bopts = _kv(getattr(args, "backend_opt", None))
    if isinstance(bopts.get("cellview"), str):
        bopts["cellview"] = tuple(bopts["cellview"].split("/"))
    be = get_backend(args.backend, **bopts)
    opts = dict(design.options)
    opts.update(_kv(args.option))
    ctx = DesignContext(pdk, be, args.corner or pdk.default_corner, args.temp, args.vdd_scale, opts)
    p = design.resolve(_kv(args.param))
    tbs = design.testbenches(p, ctx)
    for tb in tbs:
        tb.corner = ctx.corner
        tb.temperature = args.temp
    return design, pdk, be, tbs


def cmd_netlist(args) -> int:
    design, pdk, be, tbs = _testbenches(args)
    out = Path(args.out) if args.out else None
    for tb in tbs:
        if args.testbench and tb.name != args.testbench:
            continue
        if out:
            files = be.write_inputs(tb, pdk, out)
            for f in files:
                print(f"wrote {f}")
        else:
            print(f"{'*' if be.name == 'ngspice' else ';' if be.name == 'ads' else '//'} ===== testbench {tb.name}")
            print(be.netlist(tb, pdk))
    return 0


def cmd_simulate(args) -> int:
    from .designs import get_design
    from .flows.job import run_job
    store = _store(args)
    summary = run_job(_base_cfg(args, "simulate"), store)
    if not args.json:
        design = get_design(args.design)
        print(f"{design.name} on {args.backend}/{args.pdk} ({args.corner or 'default corner'}, {args.temp} C)")
        _print_metrics(design, summary.get("metrics", {}))
        for w in summary.get("warnings", []):
            print("warning:", w)
        for e in summary.get("errors", []):
            print("ERROR:", e)
    return _finish(args, summary, store)


def cmd_sweep(args) -> int:
    from .flows.job import run_job
    cfg = _base_cfg(args, "sweep")
    variables = {}
    for spec in args.var:
        k, v = spec.split("=", 1)
        variables[k] = _var_spec(v)
    cfg["sweep"] = {"variables": variables, "mode": args.mode}
    store = _store(args)
    summary = run_job(cfg, store, progress=_progress(args))
    _print_table(store, summary)
    return _finish(args, summary, store)


def cmd_corners(args) -> int:
    from .flows.job import run_job
    cfg = _base_cfg(args, "corners")
    cfg["corners"] = {"process": _list(args.process), "voltage": _list(args.voltage, float),
                      "temperature": _list(args.temperature, float)}
    store = _store(args)
    summary = run_job(cfg, store, progress=_progress(args))
    if not args.json:
        for k, v in (summary.get("worst_case") or {}).items():
            if v.get("goal"):
                print(f"  {k:<20} min {v['min']:.5g}  max {v['max']:.5g}  worst {v['worst']:.5g} at {v['worst_at']}")
    return _finish(args, summary, store)


def cmd_optimize(args) -> int:
    from .flows.job import run_job
    cfg = _base_cfg(args, "optimize")
    variables = None
    if args.var:
        variables = {}
        for spec in args.var:
            k, _, rng = spec.partition("=")
            variables[k] = rng.split(":") if rng else None
    cfg["optimize"] = {"method": args.method, "variables": variables, "objective": args.objective,
                       "constraints": args.constraint or [], "budget": args.budget, "seed": args.seed}
    store = _store(args)
    summary = run_job(cfg, store, progress=_progress(args))
    if not args.json and summary.get("best"):
        b = summary["best"]
        print(f"best (feasible={b['feasible']}):")
        for k, v in b["params"].items():
            print(f"  {k:<10} {v:.5g}")
        from .designs import get_design
        _print_metrics(get_design(args.design), b["metrics"])
    return _finish(args, summary, store)


def cmd_run(args) -> int:
    import yaml
    from .flows.job import run_job
    cfg = yaml.safe_load(Path(args.file).read_text())
    for k, v in _kv(args.set).items():
        cfg[k] = v
    store = _store(args)
    summary = run_job(cfg, store, progress=_progress(args))
    if not args.json and summary.get("metrics"):
        from .designs import get_design
        _print_metrics(get_design(cfg["design"]), summary["metrics"])
    _print_table(store, summary)
    return _finish(args, summary, store)


def cmd_jobs(args) -> int:
    from .store import Store
    st = Store(args.data)
    for j in st.list_jobs(args.limit):
        print(f"{j['id']}  {j['kind']:<9} {j['status']:<11} {j['done'] or 0:>4}/{j['total'] or 0:<4} "
              f"{(j['config'] or {}).get('design', '')}  {j['name']}")
    return 0


def cmd_runs(args) -> int:
    from .store import Store
    st = Store(args.data)
    for r in st.list_runs(args.job_id):
        ms = ", ".join(f"{k}={v:.4g}" for k, v in list(r["metrics"].items())[:6] if v is not None)
        print(f"#{r['id']:<5} idx={r['idx']:<4} ok={r['ok']!s:<5} {r['label']}  {ms}")
    return 0


def cmd_report(args) -> int:
    from .plots import write_report
    from .store import Store
    path = write_report(Store(args.data), args.job_id, args.out or f"report_{args.job_id}")
    print(f"report written to {path}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from .web.app import create_app
    app = create_app(data=args.data, max_jobs=args.max_jobs)
    print(f"AnalogForge web UI on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _progress(args):
    if args.json:
        return None

    def cb(done, total):
        print(f"\r  progress {done}/{total}", end="" if done < total else "\n", flush=True)
    return cb


def _print_table(store, summary):
    if store is None or not summary.get("job_id") or summary.get("kind") not in ("sweep", "corners", "optimize"):
        return
    runs = store.list_runs(summary["job_id"])
    if not runs:
        return
    metrics = [k for k, v in runs[0]["metrics"].items() if v is not None][:6]
    keys = list(runs[0]["label"]) or ["idx"]
    print("  " + " | ".join(f"{k:>11}" for k in keys + metrics))
    for r in runs:
        cells = [r["label"].get(k, r["idx"]) for k in keys] + [r["metrics"].get(m) for m in metrics]
        print("  " + " | ".join(f"{c:>11.4g}" if isinstance(c, (int, float)) and c is not None else f"{str(c):>11}"
                                for c in cells))


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="analogforge", description="Simulator-agnostic analog/RF IC design automation.")
    p.add_argument("--version", action="version", version=f"analogforge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, design=True):
        if design:
            sp.add_argument("design", help="built-in design, YAML design name or path to a .yaml file")
        sp.add_argument("--backend", default="ngspice", choices=["ngspice", "spectre", "ads"])
        sp.add_argument("--pdk", default="sky130", help="PDK profile name or YAML path")
        sp.add_argument("--backend-opt", action="append", dest="backend_opt",
                        help="backend option KEY=VALUE, e.g. mode=ocean, cellview=lib/cell/view")
        sp.add_argument("--corner", default=None)
        sp.add_argument("--temp", type=float, default=27.0, help="temperature in C")
        sp.add_argument("--vdd-scale", type=float, default=1.0, dest="vdd_scale")
        sp.add_argument("-p", "--param", action="append", help="parameter override NAME=VALUE (SPICE units ok)")
        sp.add_argument("-o", "--option", action="append", help="design option NAME=VALUE")
        sp.add_argument("--workers", type=int, default=None)
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--data", default=None, help="data directory (default ~/.analogforge)")
        sp.add_argument("--no-store", action="store_true", help="do not record in the SQLite database")
        sp.add_argument("--report", default=None, help="write a Markdown/PNG report into this directory")

    sp = sub.add_parser("doctor", help="check dependencies, simulators and PDKs")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--full", action="store_true", help="also run a SKY130 VCO smoke simulation")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("pdk", help="list/install/inspect PDK profiles")
    sp.add_argument("action", choices=["list", "install", "info"])
    sp.add_argument("name", nargs="?", default="sky130")
    sp.add_argument("--wheel", default=None, help="offline install from a downloaded sky130 wheel")
    sp.set_defaults(func=cmd_pdk)

    sp = sub.add_parser("designs", help="list designs or show one")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_designs)

    sp = sub.add_parser("netlist", help="print or write netlists (and OCEAN scripts for --backend spectre)")
    common(sp)
    sp.add_argument("--out", default=None, help="write input files into this directory")
    sp.add_argument("--testbench", default=None)
    sp.set_defaults(func=cmd_netlist)

    sp = sub.add_parser("simulate", help="evaluate one design point")
    common(sp)
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("sweep", help="parameter sweep, e.g. --var IBIAS=1m:4m:7 --var corner=tt,ss")
    common(sp)
    sp.add_argument("--var", action="append", required=True, help="NAME=start:stop:num[:log] or NAME=a,b,c")
    sp.add_argument("--mode", choices=["grid", "zip"], default="grid")
    sp.set_defaults(func=cmd_sweep)

    sp = sub.add_parser("corners", help="PVT corner sweep")
    common(sp)
    sp.add_argument("--process", default=None, help="e.g. tt,ff,ss (default: all PDK corners)")
    sp.add_argument("--voltage", default=None, help="supply scale factors, e.g. 0.9,1,1.1")
    sp.add_argument("--temperature", default=None, help="e.g. -40,27,125")
    sp.set_defaults(func=cmd_corners)

    sp = sub.add_parser("optimize", help="optimize a metric under constraints")
    common(sp)
    sp.add_argument("--objective", required=True, help="max:METRIC or min:METRIC")
    sp.add_argument("--var", action="append", help="NAME or NAME=low:high (default: all tunable parameters)")
    sp.add_argument("--constraint", action="append", help="e.g. 'phase_margin >= 60'")
    sp.add_argument("--method", default="bayes", choices=["bayes", "de", "nelder-mead", "random"])
    sp.add_argument("--budget", type=int, default=30)
    sp.add_argument("--seed", type=int, default=0)
    sp.set_defaults(func=cmd_optimize)

    sp = sub.add_parser("run", help="run a YAML job file (simulate/sweep/corners/optimize)")
    sp.add_argument("file")
    sp.add_argument("--set", action="append", help="override top-level job keys KEY=VALUE")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--data", default=None)
    sp.add_argument("--no-store", action="store_true")
    sp.add_argument("--report", default=None)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("jobs", help="list stored jobs")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--data", default=None)
    sp.set_defaults(func=cmd_jobs)

    sp = sub.add_parser("runs", help="list runs of a job")
    sp.add_argument("job_id")
    sp.add_argument("--data", default=None)
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("report", help="Markdown + PNG report of a stored job")
    sp.add_argument("job_id")
    sp.add_argument("--out", default=None)
    sp.add_argument("--data", default=None)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("serve", help="start the web UI / REST API")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--data", default=None)
    sp.add_argument("--max-jobs", type=int, default=2, dest="max_jobs")
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except (KeyError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
