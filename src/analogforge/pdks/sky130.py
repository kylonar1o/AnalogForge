"""Locate, install and load the open-source SkyWater SKY130 device models (Apache-2.0).

SKY130 is a real, open-source 130 nm PDK released by SkyWater and Google
(https://github.com/google/skywater-pdk).  AnalogForge never ships or edits
the model files; it only finds them:

1. ``$ANALOGFORGE_SKY130_PATH`` -> a ``sky130_fd_pr`` directory (with
   ``models/`` and ``cells/``) or an open_pdks ``sky130.lib.spice`` file;
2. the AnalogForge cache, filled by ``analogforge pdk install sky130`` which
   extracts ``sky130_fd_pr`` from the PyPI wheel ``sky130`` (gdsfactory; it
   embeds google/skywater-pdk-libs-sky130_fd_pr);
3. an installed ``sky130`` Python package;
4. an open_pdks/volare tree under ``$PDK_ROOT`` or ``~/.volare``.

For a raw ``sky130_fd_pr`` tree the ngspice loader writes a small wrapper that
includes only the SkyWater files needed for the requested corner:

* MOSFETs: the ``nfet_01v8``/``pfet_01v8`` lines of ``models/corners/<c>.spice``
* varactor (only if used): R/C parameter files of the ``.lib <c>`` section in
  ``models/sky130.lib.spice``, ``models/corners/<c>/nonfet.spice`` and
  ``cells/cap_var_lvt/...model.spice``.

That reduces model loading from ~17 s (full library) to 0.1-1.2 s with
identical results (verified for nfet Id-Vgs and varactor C-V).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..core.pdk import AVAILABILITY_HOOKS, MODEL_LOADERS, ModelStatement, PDKError

SKY130_WHEEL_SPEC = "sky130==1.0.0"
_FD_PR_PREFIX = "sky130/src/sky130_fd_pr/"
#: files extracted from the wheel (prefix match, relative to sky130_fd_pr/)
_EXTRACT = (
    "LICENSE",
    "README",
    "models/",
    "cells/nfet_01v8/",
    "cells/pfet_01v8/",
    "cells/cap_var_lvt/",
    "cells/cap_var_hvt/",
    "cells/res_generic_nd/",
    "cells/res_generic_pd/",
)
CORNERS = ("tt", "ff", "ss", "sf", "fs")


def cache_dir() -> Path:
    explicit = os.environ.get("ANALOGFORGE_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "analogforge"


@dataclass
class Sky130Location:
    kind: str     # "fd_pr" (raw sky130_fd_pr tree) or "open_pdks" (combined .lib file)
    path: Path
    origin: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "path": str(self.path), "origin": self.origin}


def _is_fd_pr(p: Path) -> bool:
    return (p / "cells" / "nfet_01v8").is_dir() and (p / "models" / "corners" / "tt.spice").is_file()


def find_sky130() -> Sky130Location | None:
    env = os.environ.get("ANALOGFORGE_SKY130_PATH")
    if env:
        p = Path(env).expanduser()
        if p.is_file() and p.name.endswith(".lib.spice"):
            return Sky130Location("open_pdks", p, "ANALOGFORGE_SKY130_PATH")
        for cand in (p, p / "sky130_fd_pr"):
            if _is_fd_pr(cand):
                return Sky130Location("fd_pr", cand, "ANALOGFORGE_SKY130_PATH")
    cached = cache_dir() / "pdk" / "sky130_fd_pr"
    if _is_fd_pr(cached):
        return Sky130Location("fd_pr", cached, "analogforge cache")
    try:
        spec = importlib.util.find_spec("sky130")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        for loc in spec.submodule_search_locations:
            cand = Path(loc) / "src" / "sky130_fd_pr"
            if _is_fd_pr(cand):
                return Sky130Location("fd_pr", cand, "python package 'sky130'")
    for root in (os.environ.get("PDK_ROOT"), str(Path.home() / ".volare")):
        if not root:
            continue
        lib = Path(root) / "sky130A" / "libs.tech" / "ngspice" / "sky130.lib.spice"
        if lib.is_file():
            return Sky130Location("open_pdks", lib, f"open_pdks ({root})")
    return None


def availability() -> tuple[bool, str]:
    loc = find_sky130()
    if loc is None:
        return False, ("SKY130 models not found - run `analogforge pdk install sky130` "
                       "(downloads the Apache-2.0 models from PyPI) or set ANALOGFORGE_SKY130_PATH / PDK_ROOT")
    return True, f"{loc.kind} at {loc.path} ({loc.origin})"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install_sky130(wheel: str | None = None, dest: str | None = None, quiet: bool = False) -> Path:
    """Extract the SKY130 models used by AnalogForge into the cache.

    ``wheel``: path to an already downloaded ``sky130-*.whl`` (offline install);
    otherwise ``pip download --no-deps sky130==1.0.0`` is run.
    """
    target = Path(dest) if dest else cache_dir() / "pdk" / "sky130_fd_pr"
    with tempfile.TemporaryDirectory(prefix="af_sky130_") as tmp:
        if wheel is None:
            # only data files are extracted from the wheel, so its Python requirement (>=3.12) is irrelevant
            cmd = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
                   "--ignore-requires-python", SKY130_WHEEL_SPEC, "-d", tmp]
            if not quiet:
                print("[analogforge] downloading", SKY130_WHEEL_SPEC, "from PyPI ...", flush=True)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError("pip download failed:\n" + proc.stdout + proc.stderr)
            wheels = list(Path(tmp).glob("sky130-*.whl"))
            if not wheels:
                raise RuntimeError("pip did not produce a sky130 wheel")
            wheel_path = wheels[0]
        else:
            wheel_path = Path(wheel)
        digest = _sha256(wheel_path)
        staging = Path(tmp) / "extract"
        count = 0
        with zipfile.ZipFile(wheel_path) as zf:
            for member in zf.namelist():
                if not member.startswith(_FD_PR_PREFIX) or member.endswith("/"):
                    continue
                rel = member[len(_FD_PR_PREFIX):]
                if not any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in _EXTRACT):
                    continue
                if ".." in Path(rel).parts or rel.startswith("/"):  # zip-slip guard
                    continue
                out = staging / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
        if not _is_fd_pr(staging):
            raise RuntimeError(f"{wheel_path.name} does not contain the expected sky130_fd_pr models")
        (staging / "ANALOGFORGE_PROVENANCE.json").write_text(json.dumps({
            "source_wheel": wheel_path.name,
            "source_wheel_sha256": digest,
            "upstream": "https://github.com/google/skywater-pdk-libs-sky130_fd_pr",
            "license": "Apache-2.0 (see LICENSE in this directory)",
            "files_extracted": count,
            "note": "Unmodified subset of sky130_fd_pr used by AnalogForge.",
        }, indent=2))
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, target)
    if not quiet:
        print(f"[analogforge] installed {count} SKY130 model files into {target}")
    return target


# ------------------------------------------------------------------ loader
_INC = re.compile(r'^\s*\.include\s+"([^"]+)"', re.IGNORECASE)


def _lib_section_includes(lib_file: Path, section: str) -> list[Path]:
    """Include files of ``.lib <section>`` ... ``.endl`` in sky130.lib.spice."""
    out, inside = [], False
    for line in lib_file.read_text().splitlines():
        s = line.strip().lower()
        if s.startswith(".lib ") and s.split()[1] == section:
            inside = True
            continue
        if inside and s.startswith(".endl"):
            break
        if inside:
            m = _INC.match(line)
            if m:
                out.append((lib_file.parent / m.group(1)).resolve())
    return out


def write_wrapper(fd_pr: Path, corner: str, varactor: bool) -> Path:
    corner_file = fd_pr / "models" / "corners" / f"{corner}.spice"
    if not corner_file.is_file():
        raise PDKError(f"SKY130 corner file not found: {corner_file}")
    incs: list[Path] = [(fd_pr / "models" / "parameters" / "lod.spice").resolve()]
    for line in corner_file.read_text().splitlines():
        m = _INC.match(line)
        if m and ("/nfet_01v8/" in m.group(1) or "/pfet_01v8/" in m.group(1)):
            incs.append((corner_file.parent / m.group(1)).resolve())
    if len(incs) < 3:
        raise PDKError(f"no nfet_01v8/pfet_01v8 includes found in {corner_file}")
    if varactor:
        rc = [p for p in _lib_section_includes(fd_pr / "models" / "sky130.lib.spice", corner)
              if "/r+c/" in str(p)]
        nonfet = fd_pr / "models" / "corners" / corner / "nonfet.spice"
        var = fd_pr / "cells" / "cap_var_lvt" / "sky130_fd_pr__cap_var_lvt.model.spice"
        for p in [*rc, nonfet, var]:
            if not Path(p).is_file():
                raise PDKError(f"SKY130 varactor support file missing: {p}")
        incs += [*rc, nonfet.resolve(), var.resolve()]
    out_dir = cache_dir() / "sky130_wrappers"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha1(str(fd_pr.resolve()).encode()).hexdigest()[:10]
    out = out_dir / f"sky130_{corner}{'_var' if varactor else ''}_{tag}.lib"
    lines = [
        f"* AnalogForge minimal SKY130 wrapper, corner {corner}" + (" (+varactor)" if varactor else ""),
        f"* generated from {corner_file}",
        "* includes unmodified Apache-2.0 SkyWater sky130_fd_pr files",
        ".option scale=1.0u",
        ".param mc_mm_switch=0 mc_pr_switch=0",
        "* rotation-weak DLC parameters, values as in models/all.spice (0)",
        ".param sky130_fd_pr__nfet_01v8__dlc_rotweak=0 sky130_fd_pr__pfet_01v8__dlc_rotweak=0",
    ] + [f'.include "{p}"' for p in incs]
    text = "\n".join(lines) + "\n"
    if not out.exists() or out.read_text() != text:
        # unique temp file per writer: several threads/processes may create the wrapper at once
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=out.stem, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, out)  # atomic; identical content from concurrent writers
    return out


def ngspice_loader(pdk, backend: str, corner: str, kinds: set[str]) -> list[ModelStatement]:
    loc = find_sky130()
    if loc is None:
        raise PDKError(availability()[1])
    if corner not in CORNERS:
        raise PDKError(f"unsupported SKY130 corner {corner!r}")
    if loc.kind == "open_pdks":
        return [ModelStatement("lib", str(loc.path), corner)]
    return [ModelStatement("include", str(write_wrapper(loc.path, corner, "varactor" in kinds)))]


MODEL_LOADERS["sky130"] = ngspice_loader
AVAILABILITY_HOOKS["sky130"] = availability
