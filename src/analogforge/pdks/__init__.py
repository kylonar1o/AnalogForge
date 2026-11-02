"""PDK profile registry.

Built-in profiles live next to this file (``sky130.yaml``,
``template_institute.yaml``).  Additional profiles are searched in the
directories listed in ``$ANALOGFORGE_PDK_PATH`` (``os.pathsep`` separated)
and in ``~/.analogforge/pdks``.  A path to a YAML file is accepted as well.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from ..core.pdk import PDK, PDKError
from . import sky130 as _sky130  # noqa: F401  (registers the ngspice loader)

BUILTIN_DIR = Path(__file__).parent


def _search_dirs() -> list[Path]:
    dirs = [BUILTIN_DIR]
    for d in os.environ.get("ANALOGFORGE_PDK_PATH", "").split(os.pathsep):
        if d:
            dirs.append(Path(d).expanduser())
    dirs.append(Path.home() / ".analogforge" / "pdks")
    return dirs


@lru_cache(maxsize=32)
def _load(path: str, mtime: float) -> PDK:
    return PDK.from_yaml(path)


def get_pdk(name_or_path: str | PDK = "sky130") -> PDK:
    if isinstance(name_or_path, PDK):
        return name_or_path
    p = Path(str(name_or_path)).expanduser()
    if p.suffix in (".yaml", ".yml") and p.is_file():
        return _load(str(p.resolve()), p.stat().st_mtime)
    for d in _search_dirs():
        for ext in (".yaml", ".yml"):
            cand = d / f"{name_or_path}{ext}"
            if cand.is_file():
                return _load(str(cand.resolve()), cand.stat().st_mtime)
    raise PDKError(f"PDK profile {name_or_path!r} not found (searched: {', '.join(map(str, _search_dirs()))})")


def list_pdks() -> list[str]:
    names: list[str] = []
    for d in _search_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.y*ml")):
                if f.stem not in names:
                    names.append(f.stem)
    return names


__all__ = ["get_pdk", "list_pdks", "PDK", "PDKError"]
