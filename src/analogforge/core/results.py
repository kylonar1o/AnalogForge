"""Normalized simulation results shared by all backends.

Signal naming (always lower case):

* ``v(node)``      node voltage
* ``i(source)``    current through a voltage source, positive *into* its + terminal
* ``dev:param``    device operating-point parameter, e.g. ``m1:gm``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..expr import evaluate


def norm_name(name: str) -> str:
    return name.strip().lower()


@dataclass
class Dataset:
    kind: str
    sweep_name: str = ""
    sweep: np.ndarray = field(default_factory=lambda: np.zeros(0))
    signals: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self):
        self.signals = {norm_name(k): np.asarray(v) for k, v in self.signals.items()}

    # ------------------------------------------------------------ access
    def has(self, name: str) -> bool:
        return self._resolve(name) is not None

    def _resolve(self, name: str) -> str | None:
        key = norm_name(name)
        if key in self.signals:
            return key
        alts = []
        if key.startswith("v(") and key.endswith(")"):
            alts.append(key[2:-1])
        elif key.startswith("i(") and key.endswith(")"):
            alts += [key[2:-1] + "#branch", key[2:-1] + ":p"]
        else:
            alts += [f"v({key})", f"i({key})"]
        for a in alts:
            if a in self.signals:
                return a
        return None

    def get(self, name: str) -> np.ndarray:
        key = self._resolve(name)
        if key is None:
            raise KeyError(f"signal {name!r} not in {self.kind} dataset "
                           f"(available: {', '.join(sorted(self.signals)[:30])})")
        return self.signals[key]

    __getitem__ = get

    def v(self, node: str) -> np.ndarray:
        return self.get(f"v({node})")

    def i(self, source: str) -> np.ndarray:
        return self.get(f"i({source})")

    def scalar(self, name: str) -> float:
        arr = self.get(name)
        x = arr.reshape(-1)[0]
        return float(np.real(x))

    def op(self, device: str, param: str) -> float:
        return self.scalar(f"{device}:{param}")

    def eval(self, expression: str) -> Any:
        """Evaluate a signal expression such as ``v(outp)-v(outn)`` or ``db20(v(out))``."""
        funcs = {
            "v": lambda n: self.get(f"v({n})"),
            "i": lambda n: self.get(f"i({n})"),
            "op": lambda d, p: self.op(d, p),
        }
        # turn bare node names inside v()/i() into string literals
        expr = re.sub(r"\b([vi])\(\s*([A-Za-z0-9_#.:\-+]+?)\s*\)",
                      lambda m: f"{m.group(1)}('{m.group(2)}')", expression)
        expr = re.sub(r"\bop\(\s*([A-Za-z0-9_.]+)\s*,\s*([A-Za-z0-9_]+)\s*\)",
                      lambda m: f"op('{m.group(1)}', '{m.group(2)}')", expr)
        ns = {"x": self.sweep, self.sweep_name.lower() or "sweep": self.sweep,
              "time": self.sweep, "freq": self.sweep, "frequency": self.sweep}
        return evaluate(expr, ns, funcs)

    def to_dict(self, max_points: int | None = None) -> dict:
        idx = np.arange(len(self.sweep))
        if max_points and len(idx) > max_points:
            idx = np.unique(np.linspace(0, len(idx) - 1, max_points).astype(int))

        def ser(a):
            a = np.asarray(a)
            if a.ndim == 0 or len(a) != len(self.sweep):
                a = a.reshape(-1)
                sel = a
            else:
                sel = a[idx]
            if np.iscomplexobj(sel):
                return {"re": sel.real.tolist(), "im": sel.imag.tolist()}
            return sel.astype(float).tolist()
        return {"kind": self.kind, "sweep_name": self.sweep_name,
                "sweep": np.asarray(self.sweep)[idx].real.astype(float).tolist() if len(self.sweep) else [],
                "signals": {k: ser(v) for k, v in self.signals.items()}}


@dataclass
class SimResult:
    ok: bool = False
    datasets: dict[str, Dataset] = field(default_factory=dict)
    log: str = ""
    netlist: str = ""
    backend: str = ""
    version: str | None = None
    diagnoses: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    fixes_applied: list[str] = field(default_factory=list)
    reduced_accuracy: bool = False
    elapsed_s: float = 0.0
    error: str = ""

    def __getitem__(self, analysis: str) -> Dataset:
        if analysis not in self.datasets:
            raise KeyError(f"analysis {analysis!r} produced no data (have: {list(self.datasets)}); "
                           f"error: {self.error or 'n/a'}")
        return self.datasets[analysis]

    def get(self, analysis: str) -> Dataset | None:
        return self.datasets.get(analysis)

    def summary(self) -> dict:
        return {
            "ok": self.ok, "backend": self.backend, "version": self.version,
            "analyses": sorted(self.datasets), "fixes_applied": self.fixes_applied,
            "reduced_accuracy": self.reduced_accuracy, "attempts": self.attempts,
            "diagnoses": self.diagnoses, "elapsed_s": round(self.elapsed_s, 4), "error": self.error,
        }
