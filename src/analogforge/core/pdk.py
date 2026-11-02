"""PDK profiles: map abstract devices to per-backend netlist syntax and model loading.

A profile is a YAML file (see ``analogforge/pdks/sky130.yaml`` and
``template_institute.yaml``).  Device lines are templates rendered with the
safe expression evaluator, e.g.::

    ngspice: 'X{name} {d} {g} {s} {b} sky130_fd_pr__nfet_01v8 W={w*1e6:.6g} L={l*1e6:.6g}'

Model loading per backend is either declarative (``include``/``lib`` +
``section``) or a named Python ``loader`` hook (used for SKY130 on ngspice).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from ..expr import render_template
from .circuit import Device


class PDKError(RuntimeError):
    pass


@dataclass
class ModelStatement:
    kind: str          # "include" or "lib"
    path: str
    section: str | None = None


@dataclass
class DeviceSpec:
    kind: str
    pins: list[str]
    defaults: dict[str, float] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    op_path: dict[str, str] = field(default_factory=dict)
    op_params: dict[str, dict[str, str]] = field(default_factory=dict)
    description: str = ""
    limits: dict[str, list[float]] = field(default_factory=dict)


#: loader name -> callable(pdk, backend, corner, kinds) -> list[ModelStatement]
MODEL_LOADERS: dict[str, Callable] = {}
#: availability hooks: loader name -> callable() -> (ok, reason)
AVAILABILITY_HOOKS: dict[str, Callable] = {}


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


@dataclass
class PDK:
    name: str
    description: str = ""
    license: str = ""
    nominal_vdd: float = 1.8
    corners: list[str] = field(default_factory=lambda: ["tt"])
    default_corner: str = "tt"
    temperatures: list[float] = field(default_factory=lambda: [-40.0, 27.0, 125.0])
    devices: dict[str, DeviceSpec] = field(default_factory=dict)
    models: dict[str, dict] = field(default_factory=dict)
    backend_options: dict[str, dict] = field(default_factory=dict)
    source: str = ""

    # ---------------------------------------------------------------- load
    @classmethod
    def from_dict(cls, data: dict, source: str = "") -> "PDK":
        devices = {}
        for kind, spec in (data.get("devices") or {}).items():
            templates = {b: spec[b] for b in ("ngspice", "spectre", "ads") if b in spec}
            devices[kind] = DeviceSpec(
                kind=kind,
                pins=list(spec.get("pins", [])),
                defaults={k: float(v) for k, v in (spec.get("defaults") or {}).items()},
                templates=templates,
                op_path=dict(spec.get("op_path") or {}),
                op_params={b: dict(v) for b, v in (spec.get("op_params") or {}).items()},
                description=spec.get("description", ""),
                limits={k: list(v) for k, v in (spec.get("limits") or {}).items()},
            )
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            license=data.get("license", ""),
            nominal_vdd=float(data.get("nominal_vdd", 1.8)),
            corners=list(data.get("corners", ["tt"])),
            default_corner=data.get("default_corner", (data.get("corners") or ["tt"])[0]),
            temperatures=[float(t) for t in data.get("temperatures", [-40, 27, 125])],
            devices=devices,
            models=dict(data.get("models") or {}),
            backend_options=dict(data.get("backend_options") or {}),
            source=source,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PDK":
        path = Path(path)
        with open(path) as fh:
            data = yaml.safe_load(fh)
        return cls.from_dict(data, str(path))

    # ------------------------------------------------------------ queries
    def device_spec(self, kind: str) -> DeviceSpec:
        if kind not in self.devices:
            raise PDKError(f"PDK {self.name!r} has no device kind {kind!r} "
                           f"(available: {', '.join(self.devices)})")
        return self.devices[kind]

    def check_corner(self, corner: str | None) -> str:
        c = (corner or self.default_corner)
        if c not in self.corners:
            raise PDKError(f"PDK {self.name!r}: unknown corner {c!r}; choose from {self.corners}")
        return c

    def available(self, backend: str) -> tuple[bool, str]:
        spec = self.models.get(backend)
        if spec is None:
            return False, f"PDK {self.name!r} has no model configuration for backend {backend!r}"
        loader = spec.get("loader")
        if loader:
            hook = AVAILABILITY_HOOKS.get(loader)
            return hook() if hook else (True, f"loader {loader}")
        path = spec.get("include") or spec.get("lib")
        if not path:
            return False, "no include/lib path configured"
        p = _expand(str(path))
        if "<" in p or "{" in p.replace("{corner}", ""):
            return False, f"model path placeholder not filled in: {path}"
        if not Path(p).exists():
            return False, f"model file not found: {p}"
        return True, p

    # --------------------------------------------------------- rendering
    def render_device(self, backend: str, dev: Device) -> str:
        spec = self.device_spec(dev.kind)
        tpl = spec.templates.get(backend)
        if tpl is None:
            raise PDKError(f"PDK {self.name!r}: device kind {dev.kind!r} has no {backend} template")
        missing = [p for p in spec.pins if p not in dev.pins]
        if missing:
            raise PDKError(f"device {dev.name}: missing pins {missing} for kind {dev.kind}")
        ns: dict = dict(spec.defaults)
        ns.update(dev.params)
        ns.update(dev.pins)
        ns["name"] = dev.name
        return render_template(tpl, ns)

    def op_path(self, backend: str, dev: Device) -> str | None:
        spec = self.device_spec(dev.kind)
        tpl = spec.op_path.get(backend)
        if tpl is None:
            return None
        # keep the case: Spectre is case-sensitive (the ngspice backend lowercases itself)
        return render_template(tpl, {"name": dev.name, "name_lower": dev.name.lower()})

    def op_params(self, backend: str, kind: str) -> dict[str, str]:
        return dict(self.device_spec(kind).op_params.get(backend, {}))

    def model_statements(self, backend: str, corner: str | None, kinds: set[str]) -> list[ModelStatement]:
        corner = self.check_corner(corner)
        spec = self.models.get(backend)
        if spec is None:
            raise PDKError(f"PDK {self.name!r} has no model configuration for backend {backend!r}")
        loader = spec.get("loader")
        if loader:
            fn = MODEL_LOADERS.get(loader)
            if fn is None:
                raise PDKError(f"unknown model loader {loader!r}")
            return fn(self, backend, corner, kinds)
        out = []
        section = spec.get("section")
        section = section.replace("{corner}", corner) if isinstance(section, str) else None
        if spec.get("lib"):
            out.append(ModelStatement("lib", _expand(str(spec["lib"])), section or corner))
        if spec.get("include"):
            inc = spec["include"]
            for path in (inc if isinstance(inc, list) else [inc]):
                out.append(ModelStatement("include", _expand(str(path)).replace("{corner}", corner), section))
        return out

    def info(self) -> dict:
        return {
            "name": self.name, "description": self.description, "license": self.license,
            "nominal_vdd": self.nominal_vdd, "corners": self.corners,
            "default_corner": self.default_corner, "temperatures": self.temperatures,
            "devices": {k: {"pins": v.pins, "description": v.description,
                            "backends": sorted(v.templates)} for k, v in self.devices.items()},
            "backends": {b: dict(zip(("available", "status"), self.available(b))) for b in self.models},
            "source": self.source,
        }
