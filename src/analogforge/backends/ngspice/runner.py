"""Execute ngspice decks with diagnosis and automatic convergence retries.

Each testbench runs in one ``ngspice -n -b`` process in a private temporary
directory.  Every analysis writes its own binary raw file; the plot name in
the file is checked so a failed analysis can never be mistaken for the
previous one.  A process-wide semaphore bounds the number of concurrent
ngspice processes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .diagnostics import Diagnosis, Fix, diagnose, plan_fixes
from .rawfile import RawFileError, RawPlot, read_raw


class NgspiceNotFound(RuntimeError):
    pass


def find_ngspice(explicit: str | None = None) -> str | None:
    """Locate ngspice (explicit path > ``$ANALOGFORGE_NGSPICE`` > ``PATH``)."""
    for cand in (explicit, os.environ.get("ANALOGFORGE_NGSPICE")):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which("ngspice")


_VERSION_CACHE: dict[str, str | None] = {}


def ngspice_version(executable: str | None = None) -> str | None:
    exe = find_ngspice(executable)
    if exe is None:
        return None
    if exe not in _VERSION_CACHE:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
            m = re.search(r"ngspice-(\S+)", out.stdout + out.stderr)
            _VERSION_CACHE[exe] = m.group(1) if m else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            _VERSION_CACHE[exe] = None
    return _VERSION_CACHE[exe]


_MAX_PROCS = int(os.environ.get("ANALOGFORGE_MAX_PROCS", "0")) or (os.cpu_count() or 2)
_PROC_SEM = threading.BoundedSemaphore(_MAX_PROCS)


def set_max_processes(n: int) -> None:
    """Change the global limit of concurrent ngspice processes."""
    global _PROC_SEM, _MAX_PROCS
    _MAX_PROCS = max(1, int(n))
    _PROC_SEM = threading.BoundedSemaphore(_MAX_PROCS)


@dataclass
class Expected:
    """An output raw file the deck is expected to write."""

    key: str
    plotname: str = ""
    optional: bool = False


@dataclass
class RunOutcome:
    ok: bool
    plots: dict[str, RawPlot] = field(default_factory=dict)
    log: str = ""
    deck: str = ""
    attempts: list[dict] = field(default_factory=list)
    diagnoses: list[Diagnosis] = field(default_factory=list)
    fixes_applied: list[str] = field(default_factory=list)
    reduced_accuracy: bool = False
    elapsed_s: float = 0.0


class NgspiceRunner:
    def __init__(self, executable: str | None = None, timeout: float = 600.0, keep_files: bool = False,
                 auto_fix: bool = True, max_attempts: int = 4, workdir_root: str | None = None):
        self.executable = find_ngspice(executable)
        self.timeout = timeout
        self.keep_files = keep_files
        self.auto_fix = auto_fix
        self.max_attempts = max_attempts
        self.workdir_root = workdir_root
        self.last_workdir: str | None = None

    def _exec(self, text: str, workdir: Path, defines: list[str]) -> str:
        if self.executable is None:
            raise NgspiceNotFound(
                "ngspice executable not found. Install it (Debian/Ubuntu: `sudo apt install ngspice`, "
                "macOS: `brew install ngspice`, conda: `conda install -c conda-forge ngspice`) "
                "or set ANALOGFORGE_NGSPICE.")
        (workdir / "circuit.cir").write_text(text)
        cmd = [self.executable, "-n", "-b"]
        for d in defines:
            cmd += ["-D", d]
        cmd.append("circuit.cir")
        env = dict(os.environ, LC_ALL="C", LANG="C")
        # ngspice is often built with OpenMP (libgomp).  Its idle worker threads busy-wait by
        # default, so several concurrent ngspice processes starve each other (measured: 4 parallel
        # VCO transients 40 s with the default policy vs 2.3 s with PASSIVE).  One thread per
        # process + passive waiting gives near-linear scaling across processes.
        env["OMP_NUM_THREADS"] = os.environ.get("ANALOGFORGE_OMP_THREADS", "1")
        env.setdefault("OMP_WAIT_POLICY", "PASSIVE")
        with _PROC_SEM:
            try:
                proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                                      timeout=self.timeout, env=env, errors="replace")
                log = proc.stdout + "\n" + proc.stderr
                if proc.returncode not in (0, 1):
                    log += f"\n[analogforge] fatal error: ngspice exit code {proc.returncode}"
            except subprocess.TimeoutExpired as exc:
                out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                log = f"{out}\n[analogforge] fatal error: ngspice timed out after {self.timeout} s"
        (workdir / "ngspice.log").write_text(log)
        return log

    @staticmethod
    def _collect(expected: list[Expected], workdir: Path) -> tuple[dict[str, RawPlot], list[str]]:
        plots: dict[str, RawPlot] = {}
        missing: list[str] = []
        for ex in expected:
            path = workdir / f"{ex.key}.raw"
            plot = None
            if path.exists():
                try:
                    found = read_raw(path)
                    plot = found[-1] if found else None
                except (RawFileError, ValueError):
                    plot = None
            if plot is not None and ex.plotname and not plot.plotname.lower().startswith(ex.plotname):
                plot = None  # a failed analysis makes `write` dump the previous plot
            if plot is None:
                if not ex.optional:
                    missing.append(ex.key)
                continue
            plots[ex.key] = plot
        return plots, missing

    def run(self, render: Callable[[dict], str], expected: list[Expected],
            defines: list[str] | None = None) -> RunOutcome:
        """Run the deck produced by ``render(extra_options)``, retrying with fixes on failure."""
        root = self.workdir_root
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="afng_", dir=root))
        out = RunOutcome(ok=False)
        t0 = time.perf_counter()
        queue: list[Fix | None] = [None]
        tried: set[str] = set()
        try:
            while queue and len(out.attempts) < self.max_attempts:
                fix = queue.pop(0)
                text = render(dict(fix.options) if fix else {})
                for f in workdir.glob("*.raw"):
                    f.unlink()
                ta = time.perf_counter()
                log = self._exec(text, workdir, list(defines or []))
                plots, missing = self._collect(expected, workdir)
                diags = diagnose(log)
                ok = not missing
                out.attempts.append({"fix": fix.name if fix else "baseline", "ok": ok,
                                     "missing": missing, "diagnoses": [d.category for d in diags],
                                     "elapsed_s": round(time.perf_counter() - ta, 4)})
                out.plots, out.log, out.deck, out.diagnoses = plots, log, text, diags
                if ok:
                    out.ok = True
                    if fix is not None:
                        out.fixes_applied.append(fix.name)
                        out.reduced_accuracy = fix.reduces_accuracy
                    break
                if not self.auto_fix:
                    break
                if fix is None:
                    queue = [f for f in plan_fixes(diags, missing) if f.name not in tried]
                else:
                    tried.add(fix.name)
        finally:
            out.elapsed_s = time.perf_counter() - t0
            if self.keep_files:
                self.last_workdir = str(workdir)
            else:
                shutil.rmtree(workdir, ignore_errors=True)
        return out
