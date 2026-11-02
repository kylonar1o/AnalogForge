"""ngspice backend (reference implementation, fully tested)."""

from __future__ import annotations

import re

import numpy as np

from ...core.circuit import Device
from ...core.pdk import PDK, PDKError
from ...core.results import Dataset, SimResult
from ...core.testbench import Testbench
from ..base import Backend, register_backend
from .netlist import PLOTNAMES, render_deck
from .rawfile import RawPlot
from .runner import Expected, NgspiceNotFound, NgspiceRunner, find_ngspice, ngspice_version, set_max_processes

_BRANCH = re.compile(r"^(.*)#branch$")


def _normalize(plot: RawPlot, kind: str, op_reverse: dict[str, str]) -> Dataset:
    signals: dict[str, np.ndarray] = {}
    for name in plot.names[1:] if kind not in ("op",) else plot.names:
        arr = plot.data[name]
        key = name.lower()
        m = _BRANCH.match(key)
        if m:
            key = f"i({m.group(1)})"
        elif key in op_reverse:
            key = op_reverse[key]
        elif not (key.startswith("v(") or key.startswith("i(") or key.startswith("@")) and \
                kind not in ("noise", "noise_integrated") and plot.types[plot.names.index(name)] == "voltage":
            key = f"v({key})"
        signals[key] = arr
    if kind == "op":
        return Dataset("op", "", np.zeros(1), signals)
    scale = plot.data[plot.names[0]]
    if kind in ("ac", "noise", "pss"):
        scale = np.real(scale)
    return Dataset(kind, plot.names[0].lower(), np.asarray(scale), signals)


@register_backend
class NgspiceBackend(Backend):
    name = "ngspice"
    capabilities = frozenset({"op", "dc", "ac", "tran", "noise", "pss"})
    verification = "fully implemented and tested (pytest runs real ngspice + SKY130)"
    netlist_suffix = "cir"

    def __init__(self, executable: str | None = None, timeout: float = 600.0, keep_files: bool = False,
                 auto_fix: bool = True, max_attempts: int = 4):
        self.runner = NgspiceRunner(executable, timeout=timeout, keep_files=keep_files,
                                    auto_fix=auto_fix, max_attempts=max_attempts)

    def available(self) -> tuple[bool, str]:
        exe = find_ngspice(self.runner.executable)
        if exe is None:
            return False, "ngspice executable not found (install ngspice or set ANALOGFORGE_NGSPICE)"
        return True, f"{exe} (ngspice-{ngspice_version(exe)})"

    def version(self) -> str | None:
        return ngspice_version(self.runner.executable)

    # -------------------------------------------------------------- netlist
    @staticmethod
    def _device_vector(tb: Testbench, pdk: PDK, dev_name: str, std: str | None):
        """ngspice vector(s) for a device parameter (all standard params if ``std`` is None)."""
        try:
            dev = tb.circuit[dev_name]
        except KeyError:
            raise PDKError(f"device {dev_name!r} (requested for saving) not in circuit") from None
        if not isinstance(dev, Device):
            return []
        path = pdk.op_path("ngspice", dev)
        if path is None:
            return []
        path = path.lower()
        params = pdk.op_params("ngspice", dev.kind)
        items = params.items() if std is None else [(std, params.get(std, std))]
        return [(f"@{path}[{native}]", f"{dev.name.lower()}:{s}") for s, native in items]

    def _op_vectors(self, tb: Testbench, pdk: PDK) -> tuple[list[str], dict[str, str]]:
        vectors: list[str] = []
        reverse: dict[str, str] = {}
        for dev_name in tb.save_op:
            for vec, neutral in self._device_vector(tb, pdk, dev_name, None):
                vectors.append(vec)
                reverse[vec.lower()] = neutral
        return vectors, reverse

    def _analysis_saves(self, tb: Testbench, pdk: PDK, reverse: dict[str, str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for an in tb.analyses:
            names = getattr(an, "save", None) or []
            vecs = []
            for item in names:
                dev, _, param = item.partition(":")
                for vec, neutral in self._device_vector(tb, pdk, dev, param or None):
                    vecs.append(vec)
                    reverse[vec.lower()] = neutral
                    reverse[f"i({vec.lower()})"] = neutral
            if vecs:
                out[an.key()] = vecs
        return out

    def netlist(self, tb: Testbench, pdk: PDK) -> str:
        self.check_supported(tb)
        vectors, reverse = self._op_vectors(tb, pdk)
        return render_deck(tb, pdk, vectors, None, self._analysis_saves(tb, pdk, reverse))

    # ------------------------------------------------------------------ run
    def run(self, tb: Testbench, pdk: PDK) -> SimResult:
        res = SimResult(backend=self.name, version=self.version())
        try:
            self.check_supported(tb)
            vectors, reverse = self._op_vectors(tb, pdk)
            saves = self._analysis_saves(tb, pdk, reverse)
            res.netlist = render_deck(tb, pdk, vectors, None, saves)
        except Exception as exc:  # unsupported analysis, PDK problems, template errors
            res.error = f"{type(exc).__name__}: {exc}"
            return res
        expected: list[Expected] = []
        for an in tb.analyses:
            key = an.key()
            expected.append(Expected(key, PLOTNAMES.get(an.kind, "")))
            if an.kind == "noise":
                expected.append(Expected(f"{key}_integrated", PLOTNAMES["noise_integrated"], optional=True))
            if an.kind == "pss":
                expected.append(Expected(f"{key}_td", PLOTNAMES["pss_td"], optional=True))
        defines = list(pdk.backend_options.get("ngspice", {}).get("defines", []))
        try:
            outcome = self.runner.run(lambda extra: render_deck(tb, pdk, vectors, extra, saves), expected, defines)
        except NgspiceNotFound as exc:
            res.error = str(exc)
            return res
        kinds = {an.key(): an.kind for an in tb.analyses}
        for key, plot in outcome.plots.items():
            kind = kinds.get(key)
            if kind is None:
                kind = "noise_integrated" if key.endswith("_integrated") else ("pss_td" if key.endswith("_td") else "")
            res.datasets[key] = _normalize(plot, kind, reverse)
        res.ok = outcome.ok
        res.log = outcome.log
        res.netlist = outcome.deck or res.netlist
        res.attempts = outcome.attempts
        res.diagnoses = [d.to_dict() for d in outcome.diagnoses]
        res.fixes_applied = outcome.fixes_applied
        res.reduced_accuracy = outcome.reduced_accuracy
        res.elapsed_s = outcome.elapsed_s
        if not outcome.ok:
            msgs = "; ".join(d.message for d in outcome.diagnoses) or "analysis produced no output"
            res.error = f"ngspice failed after {len(outcome.attempts)} attempt(s): {msgs}"
        return res


__all__ = ["NgspiceBackend", "NgspiceRunner", "find_ngspice", "ngspice_version", "set_max_processes"]
