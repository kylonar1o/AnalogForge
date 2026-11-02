"""Cadence Spectre backend (adapter): Spectre netlists, OCEAN scripts, PSF/nutmeg parsing.

Run modes
---------
``direct`` (default)
    ``spectre input.scs +escchars =log spectre.out -format psfascii -raw psf``,
    results parsed from the PSF-ASCII directory.  Only needs ``spectre`` on PATH.
``ocean``
    writes a circuit-only netlist + OCEAN script and runs ``ocean -nograph -restore``;
    with ``cellview=(lib, cell, view)`` an existing Virtuoso schematic is simulated
    and design variables are set via ``desVar``.

Neither tool exists in AnalogForge's development environment, so this backend
is tested with synthetic result files only - see docs/cadence_spectre.md.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

from ...core.circuit import Device
from ...core.pdk import PDK
from ...core.results import Dataset, SimResult
from ...core.testbench import Testbench
from ..base import Backend, register_backend
from ..ngspice.rawfile import read_raw
from .netlist import render_netlist, sanitize
from .ocean import OceanSpec, generate_ocean, parse_measurements, parse_ocnprint
from .psf import PSFError, read_psf_ascii, signal_key

_SUFFIXES = {
    "op": [".dc"], "dc": [".dc"], "ac": [".ac"], "tran": [".tran.tran", ".tran"],
    "noise": [".noise"], "pss": [".fd.pss"], "pss_td": [".td.pss"], "pnoise": [".pnoise"],
}


def _device_saves(tb: Testbench, pdk: PDK) -> tuple[list[str], dict[str, str]]:
    saves, reverse = [], {}
    wanted = {d: None for d in tb.save_op}
    for an in tb.analyses:
        for item in getattr(an, "save", []) or []:
            dev, _, par = item.partition(":")
            wanted.setdefault(dev, set())
            if isinstance(wanted[dev], set):
                wanted[dev].add(par)
    for dev_name, params in wanted.items():
        try:
            dev = tb.circuit[dev_name]
        except KeyError:
            continue
        if not isinstance(dev, Device):
            continue
        path = pdk.op_path("spectre", dev) or sanitize(dev.name)
        mapping = pdk.op_params("spectre", dev.kind) or {"gm": "gm", "gds": "gds", "id": "ids"}
        for std, native in mapping.items():
            if params is not None and std not in params:
                continue
            saves.append(f"{path}:{native}")
            reverse[f"{path}:{native}".lower()] = f"{dev.name.lower()}:{std}"
    return saves, reverse


def psf_to_dataset(path: Path, kind: str, reverse: dict[str, str]) -> Dataset:
    data = read_psf_ascii(path)
    trace_types = dict(data.traces)
    if data.sweep is None:
        sig = {}
        for name, val in data.scalars.items():
            if isinstance(val, str):
                continue
            key = reverse.get(name.lower(), signal_key(name, ""))
            sig[key] = np.array([val])
        return Dataset("op" if kind == "op" else kind, "", np.zeros(1), sig)
    sig = {reverse.get(name.lower(), signal_key(name, trace_types.get(name, ""))): arr
           for name, arr in data.signals.items()}
    return Dataset(kind, (data.sweep_name or "").lower(), data.sweep, sig)


def parse_psf_dir(psf_dir: str | Path, tb: Testbench, reverse: dict[str, str] | None = None) -> dict[str, Dataset]:
    psf_dir = Path(psf_dir)
    out: dict[str, Dataset] = {}
    for an in tb.analyses:
        key = sanitize(an.key())
        wanted = [(an.key(), an.kind)]
        if an.kind == "pss":
            wanted.append((f"{an.key()}_td", "pss_td"))
        for out_key, kind in wanted:
            for suf in _SUFFIXES.get(kind, []):
                f = psf_dir / f"{key}{suf}"
                if f.is_file():
                    try:
                        out[out_key] = psf_to_dataset(f, kind if kind != "pss_td" else "pss_td", reverse or {})
                    except PSFError:
                        pass
                    break
    return out


def parse_nutmeg(raw_file: str | Path, tb: Testbench) -> dict[str, Dataset]:
    """Map plots of a Spectre nutmeg file (``-format nutascii/nutbin``) to analyses by name."""
    plots = read_raw(raw_file)
    out: dict[str, Dataset] = {}
    for an in tb.analyses:
        key = sanitize(an.key()).lower()
        for pl in plots:
            if f"`{key}'" in pl.plotname.lower() or f"'{key}'" in pl.plotname.lower() \
                    or pl.plotname.lower().startswith(key):
                sig = {}
                for nm in pl.names[1:]:
                    low = nm.lower()
                    k = f"i({low.split(':')[0]})" if low.endswith(":p") else (low if "(" in low else f"v({low})")
                    sig[k] = pl.data[nm]
                out[an.key()] = Dataset(an.kind, pl.names[0].lower(), np.real(pl.data[pl.names[0]]), sig)
                break
    return out


@register_backend
class SpectreBackend(Backend):
    name = "spectre"
    capabilities = frozenset({"op", "dc", "ac", "tran", "noise", "pss", "pnoise"})
    verification = ("adapter: netlist/OCEAN generation and PSF parsing unit-tested with synthetic files; "
                    "not executed against a real Spectre installation")
    netlist_suffix = "scs"

    def __init__(self, mode: str = "direct", executable: str | None = None, ocean_executable: str | None = None,
                 output_format: str = "psfascii", cellview: tuple[str, str, str] | None = None,
                 timeout: float = 3600.0, keep_files: bool = False, extra_args: list[str] | None = None):
        self.mode = mode
        self.executable = executable or os.environ.get("ANALOGFORGE_SPECTRE") or "spectre"
        self.ocean_executable = ocean_executable or os.environ.get("ANALOGFORGE_OCEAN") or "ocean"
        self.output_format = output_format
        self.cellview = cellview
        self.timeout = timeout
        self.keep_files = keep_files
        self.extra_args = list(extra_args or [])

    def available(self) -> tuple[bool, str]:
        exe = self.ocean_executable if self.mode == "ocean" else self.executable
        path = shutil.which(exe)
        if path is None:
            return False, (f"'{exe}' not found on PATH - Spectre/OCEAN are only available on Cadence machines "
                           f"(netlists and OCEAN scripts can still be generated)")
        return True, path

    def version(self) -> str | None:
        path = shutil.which(self.executable)
        if not path:
            return None
        try:
            out = subprocess.run([path, "-W"], capture_output=True, text=True, timeout=30)
            m = re.search(r"(\d+\.\d+[\w.\-]*)", out.stdout + out.stderr)
            return m.group(1) if m else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            return None

    # --------------------------------------------------------------- inputs
    def netlist(self, tb: Testbench, pdk: PDK) -> str:
        self.check_supported(tb)
        saves, _ = _device_saves(tb, pdk)
        return render_netlist(tb, pdk, saves)

    def ocean_script(self, tb: Testbench, pdk: PDK, workdir: str | Path, netlist_path: str | None = None) -> str:
        corner = pdk.check_corner(tb.corner)
        models = [(st.path, st.section) for st in pdk.model_statements("spectre", corner, tb.circuit.device_kinds())]
        probes: dict[str, list[str]] = {}
        for an in tb.analyses:
            probes[an.kind] = list(getattr(tb, "probes", []) or [])
        spec = OceanSpec(mode="cellview" if self.cellview else "netlist", corner=corner,
                         netlist_path=netlist_path or "",
                         results_dir=str(Path(workdir) / "ocean_results"), model_files=models,
                         design_vars=dict(tb.variables), temperature=tb.temperature, probes=probes)
        if self.cellview:
            spec.lib, spec.cell, spec.view = self.cellview
        return generate_ocean(tb, spec)

    def write_inputs(self, tb: Testbench, pdk: PDK, workdir: str | Path) -> list[Path]:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        full = workdir / f"{tb.name}.scs"
        full.write_text(self.netlist(tb, pdk))
        circuit_only = workdir / f"{tb.name}_circuit.scs"
        saves, _ = _device_saves(tb, pdk)
        circuit_only.write_text(render_netlist(tb, pdk, saves, include_analyses=False, include_models=False))
        ocn = workdir / f"{tb.name}.ocn"
        ocn.write_text(self.ocean_script(tb, pdk, workdir, str(circuit_only)))
        return [full, circuit_only, ocn]

    # ------------------------------------------------------------------ run
    def run(self, tb: Testbench, pdk: PDK) -> SimResult:
        res = SimResult(backend=self.name)
        ok, why = self.available()
        try:
            self.check_supported(tb)
            res.netlist = self.netlist(tb, pdk)
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {exc}"
            return res
        if not ok:
            res.error = why
            return res
        res.version = self.version()
        workdir = Path(tempfile.mkdtemp(prefix="afspectre_"))
        t0 = time.perf_counter()
        try:
            files = self.write_inputs(tb, pdk, workdir)
            _, reverse = _device_saves(tb, pdk)
            if self.mode == "ocean":
                cmd = [self.ocean_executable, "-nograph", "-restore", str(files[2])]
            else:
                raw = workdir / ("psf" if self.output_format.startswith("psf") else "out.raw")
                cmd = [self.executable, str(files[0]), "+escchars", f"=log", str(workdir / "spectre.out"),
                       "-format", self.output_format, "-raw", str(raw), *self.extra_args]
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=self.timeout)
            res.log = proc.stdout + proc.stderr
            log_file = workdir / "spectre.out"
            if log_file.exists():
                res.log += "\n" + log_file.read_text(errors="replace")
            if self.mode == "ocean":
                res.datasets = self._parse_ocean(workdir / "ocean_results", tb)
            elif self.output_format.startswith("psf"):
                res.datasets = parse_psf_dir(workdir / "psf", tb, reverse)
            else:
                res.datasets = parse_nutmeg(workdir / "out.raw", tb)
            missing = [a.key() for a in tb.analyses if a.key() not in res.datasets]
            res.ok = not missing and proc.returncode == 0
            if not res.ok:
                res.error = f"spectre exit code {proc.returncode}; missing results: {missing}"
        except subprocess.TimeoutExpired:
            res.error = f"spectre timed out after {self.timeout} s"
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {exc}"
        finally:
            res.elapsed_s = time.perf_counter() - t0
            if not self.keep_files:
                shutil.rmtree(workdir, ignore_errors=True)
        return res

    @staticmethod
    def _parse_ocean(results: Path, tb: Testbench) -> dict[str, Dataset]:
        out: dict[str, Dataset] = {}
        meas = results / "analogforge_measurements.txt"
        scalars = parse_measurements(meas) if meas.exists() else {}
        for an in tb.analyses:
            wave = results / f"wave_{an.kind}.txt"
            if wave.exists():
                labels, arr = parse_ocnprint(wave)
                probes = list(getattr(tb, "probes", []) or [])
                if arr.size:
                    sig = {}
                    # ocnPrint repeats the x column per signal: x y1 [x y2 ...] or x y1 y2 ...
                    ycols = [c for c in range(1, arr.shape[1]) if not np.allclose(arr[:, c], arr[:, 0])]
                    for name, c in zip(probes, ycols):
                        sig[name.lower()] = arr[:, c]
                    out[an.key()] = Dataset(an.kind, "x", arr[:, 0], sig)
        if scalars:
            out["ocean_measurements"] = Dataset("op", "", np.zeros(1),
                                                {k: np.array([v]) for k, v in scalars.items()})
        return out
