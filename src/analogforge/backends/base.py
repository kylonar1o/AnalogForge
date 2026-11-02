"""Backend interface shared by ngspice, Spectre and ADS adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.analyses import UnsupportedAnalysis
from ..core.pdk import PDK
from ..core.results import SimResult
from ..core.testbench import Testbench


class BackendUnavailable(RuntimeError):
    """The simulator executable / API required by a backend is not installed."""


class Backend(ABC):
    name: str = "base"
    capabilities: frozenset[str] = frozenset()
    #: short human-readable statement of how well the backend is verified
    verification: str = ""

    def supports(self, analysis_kind: str) -> bool:
        return analysis_kind in self.capabilities

    def check_supported(self, tb: Testbench) -> None:
        missing = sorted(tb.analysis_kinds() - set(self.capabilities))
        if missing:
            raise UnsupportedAnalysis(f"backend {self.name!r} does not support analyses {missing} "
                                      f"(testbench {tb.name!r})")

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """``(True, info)`` if the simulator can be executed here."""

    def version(self) -> str | None:
        return None

    @abstractmethod
    def netlist(self, tb: Testbench, pdk: PDK) -> str:
        """Complete simulator input deck for ``tb``."""

    def write_inputs(self, tb: Testbench, pdk: PDK, workdir: str | Path) -> list[Path]:
        """Write all input files (netlist, scripts) into ``workdir``; return their paths."""
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / f"{tb.name}.{self.netlist_suffix}"
        path.write_text(self.netlist(tb, pdk))
        return [path]

    netlist_suffix = "cir"

    @abstractmethod
    def run(self, tb: Testbench, pdk: PDK) -> SimResult:
        """Simulate and return normalized results (never raises for simulation failures)."""

    def info(self) -> dict:
        ok, why = self.available()
        return {"name": self.name, "available": ok, "status": why, "version": self.version() if ok else None,
                "capabilities": sorted(self.capabilities), "verification": self.verification}


_REGISTRY: dict[str, type] = {}


def register_backend(cls):
    _REGISTRY[cls.name] = cls
    return cls


def get_backend(name: str | Backend = "ngspice", **options) -> Backend:
    if isinstance(name, Backend):
        return name
    # import lazily so the registry is populated
    from . import ads, ngspice, spectre  # noqa: F401
    key = str(name).lower()
    if key not in _REGISTRY:
        raise KeyError(f"unknown backend {name!r}; available: {', '.join(_REGISTRY)}")
    return _REGISTRY[key](**options)


def list_backends() -> list[str]:
    from . import ads, ngspice, spectre  # noqa: F401
    return list(_REGISTRY)
