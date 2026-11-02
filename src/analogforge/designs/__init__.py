"""Design registry: built-in Python designs plus YAML designs found on the search path.

YAML designs are searched in ``examples/designs`` of a source checkout,
``$ANALOGFORGE_DESIGN_PATH`` (``os.pathsep`` separated) and
``~/.analogforge/designs``.  A path to a ``.yaml`` file is always accepted.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core.design import Design, DesignError
from ..core.yaml_design import load_yaml_design
from .lc_vco import LCVCO
from .two_stage_opamp import TwoStageOpamp

BUILTIN: dict[str, type[Design]] = {cls.name: cls for cls in (LCVCO, TwoStageOpamp)}
ALIASES = {"vco": "lc_vco", "opamp": "two_stage_opamp", "ota2": "two_stage_opamp"}


def _yaml_dirs() -> list[Path]:
    dirs = []
    for d in os.environ.get("ANALOGFORGE_DESIGN_PATH", "").split(os.pathsep):
        if d:
            dirs.append(Path(d).expanduser())
    repo_examples = Path(__file__).resolve().parents[3] / "examples" / "designs"
    dirs.append(repo_examples)
    dirs.append(Path.home() / ".analogforge" / "designs")
    return dirs


def _yaml_files() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for d in _yaml_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.y*ml")):
                found.setdefault(f.stem, f)
    return found


def get_design(name: str | Design) -> Design:
    if isinstance(name, Design):
        return name
    p = Path(str(name)).expanduser()
    if p.suffix in (".yaml", ".yml") and p.is_file():
        return load_yaml_design(p)
    key = ALIASES.get(str(name).lower(), str(name))
    if key in BUILTIN:
        return BUILTIN[key]()
    files = _yaml_files()
    if key in files:
        return load_yaml_design(files[key])
    raise DesignError(f"unknown design {name!r}; available: {', '.join(list_designs())}")


def list_designs() -> list[str]:
    names = list(BUILTIN)
    for stem, path in _yaml_files().items():
        try:
            d = load_yaml_design(path)
            if d.name not in names:
                names.append(d.name)
        except Exception:  # a broken user YAML must not break the catalogue
            continue
    return names


__all__ = ["get_design", "list_designs", "LCVCO", "TwoStageOpamp", "BUILTIN"]
