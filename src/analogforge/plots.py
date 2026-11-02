"""Matplotlib figures and Markdown reports for runs and jobs (the web UI uses Plotly client-side)."""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .units import format_si  # noqa: E402

UNITS = {"f_osc": "Hz", "f_fft": "Hz", "f_pss": "Hz", "f_min": "Hz", "f_max": "Hz", "power": "W",
         "amplitude_diff": "V", "kvco_max": "Hz/V", "kvco_nom": "Hz/V", "ugbw": "Hz", "f3db": "Hz",
         "slew_rate": "V/s", "settling_time": "s", "startup_time": "s"}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _curve_dicts(run) -> list[dict]:
    if hasattr(run, "curves"):
        return [c.to_dict() for c in run.curves]
    return list(run.get("curves", []))


def plot_curves(run, outdir: str | Path, prefix: str = "") -> list[Path]:
    """One PNG per curve of a run (EvaluationResult or stored run dict)."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for c in _curve_dicts(run):
        fig, ax = plt.subplots(figsize=(7, 4))
        x = np.array([np.nan if v is None else v for v in c["x"]], float)
        for label, ys in c["series"].items():
            y = np.array([np.nan if v is None else v for v in ys], float)
            ax.plot(x, y, label=label, lw=1.4)
        if c.get("xlog"):
            ax.set_xscale("log")
        if c.get("ylog"):
            ax.set_yscale("log")
        ax.set_xlabel(c.get("xlabel", ""))
        ax.set_ylabel(c.get("ylabel", ""))
        ax.set_title(c.get("title", c["name"]))
        ax.grid(True, which="both", alpha=0.3)
        if len(c["series"]) > 1:
            ax.legend(fontsize=8)
        fig.tight_layout()
        p = outdir / f"{prefix}{_safe(c['name'])}.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        paths.append(p)
    return paths


def _numeric_metrics(runs: list[dict], limit: int = 8, preferred: list[str] | None = None) -> list[str]:
    names = []
    for k in (preferred or []):
        if any(np.isfinite(r["metrics"].get(k) or np.nan) for r in runs):
            names.append(k)
    for r in runs:
        for k, v in r["metrics"].items():
            if k not in names and v is not None and np.isfinite(v):
                names.append(k)
    return names[:limit]


def plot_sweep(runs: list[dict], variables: list[str], outdir: str | Path, metrics: list[str] | None = None) -> list[Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    metrics = metrics or _numeric_metrics(runs)
    x_var = variables[0]
    group_var = variables[1] if len(variables) > 1 else None
    paths = []
    for m in metrics:
        fig, ax = plt.subplots(figsize=(6.5, 4))
        groups: dict = {}
        for r in runs:
            g = r["label"].get(group_var) if group_var else None
            groups.setdefault(g, []).append((r["label"].get(x_var), r["metrics"].get(m)))
        for g, pts in groups.items():
            pts = sorted((x, y if y is not None else np.nan) for x, y in pts if not isinstance(x, str))
            if not pts:
                continue
            xs, ys = zip(*pts)
            ax.plot(xs, ys, "o-", label=f"{group_var}={g}" if group_var else None)
        ax.set_xlabel(x_var)
        ax.set_ylabel(f"{m} [{UNITS.get(m, '')}]")
        ax.set_title(f"{m} vs {x_var}")
        ax.grid(True, alpha=0.3)
        if group_var:
            ax.legend(fontsize=8)
        fig.tight_layout()
        p = outdir / f"sweep_{_safe(m)}.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        paths.append(p)
    return paths


def plot_corners(runs: list[dict], outdir: str | Path, metrics: list[str] | None = None) -> list[Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    metrics = metrics or _numeric_metrics(runs, 6)
    labels = [f"{r['corner']}/{r['vdd_scale']:.2f}V/{r['temperature']:.0f}C" for r in runs]
    paths = []
    for m in metrics:
        vals = [r["metrics"].get(m) if r["metrics"].get(m) is not None else np.nan for r in runs]
        fig, ax = plt.subplots(figsize=(max(6, 0.35 * len(runs)), 4))
        ax.bar(range(len(vals)), vals, color="#4c78a8")
        ax.set_xticks(range(len(vals)))
        ax.set_xticklabels(labels, rotation=90, fontsize=7)
        ax.set_ylabel(f"{m} [{UNITS.get(m, '')}]")
        ax.set_title(f"PVT: {m}")
        ax.grid(True, axis="y", alpha=0.3)
        fig.tight_layout()
        p = outdir / f"pvt_{_safe(m)}.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        paths.append(p)
    return paths


def plot_optimization(summary: dict, runs: list[dict], outdir: str | Path) -> list[Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    obj = summary.get("objective", ":")
    metric = obj.split(":", 1)[1]
    vals = [r["metrics"].get(metric) for r in runs]
    fig, ax = plt.subplots(figsize=(6.5, 4))
    ax.plot(range(len(vals)), [np.nan if v is None else v for v in vals], "o", alpha=0.6, label=metric)
    goal = obj.split(":", 1)[0]
    best, curve = None, []
    for v in vals:
        if v is not None and np.isfinite(v) and (best is None or (v > best if goal == "max" else v < best)):
            best = v
        curve.append(np.nan if best is None else best)
    ax.plot(range(len(vals)), curve, "-", color="crimson", label="best so far (unconstrained)")
    ax.set_xlabel("evaluation")
    ax.set_ylabel(metric)
    ax.set_title(f"Optimization ({summary.get('method')}): {obj}")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = outdir / "optimization.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return [p]


def _fmt(v, unit="") -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "n/a"
    if isinstance(v, (int, float)):
        return format_si(v, unit, 5)
    return str(v)


def write_report(store, job_id: str, outdir: str | Path) -> Path:
    """Markdown report with tables and figures for any job kind."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(f"job {job_id} not found")
    runs = store.list_runs(job_id)
    full = [store.get_run(r["id"]) for r in runs]
    lines = [f"# AnalogForge report - job {job_id}", "",
             f"* kind: **{job['kind']}**, status: **{job['status']}**",
             f"* design: `{job['config'].get('design')}`, backend: `{job['config'].get('backend')}`, "
             f"PDK: `{job['config'].get('pdk')}`", f"* runs: {len(runs)}", ""]
    figs: list[Path] = []
    kind = job["kind"]
    summ = job.get("summary") or {}
    if kind == "simulate" and full:
        r = full[0]
        lines += ["## Metrics", "", "| metric | value |", "|---|---|"]
        lines += [f"| {k} | {_fmt(v, UNITS.get(k, ''))} |" for k, v in r["metrics"].items()]
        if r.get("extra", {}).get("phase_noise_method"):
            lines += ["", f"Phase-noise method: `{r['extra']['phase_noise_method']}`"]
        figs = plot_curves(r, outdir)
    elif kind == "sweep":
        variables = summ.get("variables") or list(runs[0]["label"]) if runs else []
        figs = plot_sweep(runs, variables, outdir)
    elif kind == "corners":
        figs = plot_corners(runs, outdir)
        wc = summ.get("worst_case", {})
        lines += ["## Worst case over PVT", "", "| metric | min | max | worst | at |", "|---|---|---|---|---|"]
        for k, v in wc.items():
            lines.append(f"| {k} | {_fmt(v['min'], UNITS.get(k, ''))} | {_fmt(v['max'], UNITS.get(k, ''))} | "
                         f"{_fmt(v['worst'], UNITS.get(k, ''))} | {v['worst_at'] or ''} |")
    elif kind == "optimize":
        figs = plot_optimization(summ, runs, outdir)
        b = summ.get("best") or {}
        lines += ["## Best point", "", f"feasible: {b.get('feasible')}", "", "| parameter | value |", "|---|---|"]
        lines += [f"| {k} | {_fmt(v)} |" for k, v in (b.get("params") or {}).items()]
        lines += ["", "| metric | value |", "|---|---|"]
        lines += [f"| {k} | {_fmt(v, UNITS.get(k, ''))} |" for k, v in (b.get("metrics") or {}).items()]
    if runs and kind != "simulate":
        metrics = _numeric_metrics(runs, 8)
        keys = list(runs[0]["label"])
        lines += ["", "## Runs", "", "| # | " + " | ".join(keys + metrics) + " |",
                  "|" + "---|" * (1 + len(keys) + len(metrics))]
        for r in runs:
            cells = [str(r["idx"])] + [_fmt(r["label"].get(k)) for k in keys] + \
                    [_fmt(r["metrics"].get(m), UNITS.get(m, "")) for m in metrics]
            lines.append("| " + " | ".join(cells) + " |")
    if figs:
        lines += ["", "## Figures", ""] + [f"![{p.stem}]({p.name})" for p in figs]
    path = outdir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path
