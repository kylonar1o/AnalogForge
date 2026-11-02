"""Thin wrapper around the Python API shipped with Keysight ADS (2024 and later).

Every call into Keysight modules is in this file and imported lazily, so the
rest of AnalogForge works without ADS.  Module/function names follow
Keysight's ADS Python API documentation:

* ``keysight.edatoolbox.ads.CircuitSimulator().run_netlist(netlist, output_dir=...)``
* ``keysight.ads.dataset.open(path)`` -> dataset with variable blocks that
  convert to pandas via ``to_dataframe()``
* ``keysight.ads.de`` for workspace/design access (netlist from a schematic)

Because ADS is not installed in the development environment these calls
are exercised only against a fake module in the tests.  If your ADS version
names something differently, adapt this file only (docs/keysight_ads.md).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


class ADSAPIUnavailable(RuntimeError):
    pass


def import_api():
    try:
        from keysight.edatoolbox import ads as eda_ads  # type: ignore
        import keysight.ads.dataset as dataset  # type: ignore
    except ImportError as exc:
        raise ADSAPIUnavailable(
            "Keysight ADS Python API not importable. Run AnalogForge with the Python interpreter "
            "bundled with ADS (e.g. $HPEESOF_DIR/tools/python/bin/python3) or add ADS's site-packages "
            f"to PYTHONPATH. ({exc})") from exc
    return eda_ads, dataset


def api_available() -> tuple[bool, str]:
    try:
        import_api()
    except ADSAPIUnavailable as exc:
        return False, str(exc)
    return True, "keysight.edatoolbox / keysight.ads.dataset importable"


def run_netlist(netlist: str, output_dir: str | Path) -> list[Path]:
    """Simulate an ADS netlist; return the dataset files produced."""
    eda_ads, _ = import_api()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sim = eda_ads.CircuitSimulator()
    sim.run_netlist(netlist, output_dir=str(output_dir))
    return sorted(output_dir.glob("*.ds"))


def _block_names(ds) -> list[str]:
    for attr in ("varblock_names", "varblocks", "keys"):
        obj = getattr(ds, attr, None)
        if obj is None:
            continue
        names = obj() if callable(obj) else obj
        names = list(names)
        if names and not isinstance(names[0], str):
            names = [getattr(b, "name", str(b)) for b in names]
        return names
    raise ADSAPIUnavailable("cannot list variable blocks of the ADS dataset (API changed?)")


def read_dataset(path: str | Path) -> dict[str, dict]:
    """Return ``{block_name: {"x_name": str, "x": ndarray, "signals": {name: ndarray}}}``."""
    _, dataset = import_api()
    ds = dataset.open(Path(path))
    out = {}
    for name in _block_names(ds):
        block = ds[name]
        df = block.to_dataframe()
        if getattr(df.index, "name", None) is None and len(df.columns):
            # no named index: treat the first column as the independent variable
            x_name = str(df.columns[0])
            x = np.asarray(df[df.columns[0]])
            cols = list(df.columns[1:])
        else:
            x_name = str(df.index.name)
            x = np.asarray(df.index)
            cols = list(df.columns)
        out[str(name)] = {"x_name": x_name, "x": x,
                          "signals": {str(c): np.asarray(df[c]) for c in cols}}
    return out


def netlist_from_schematic(workspace: str, lib: str, cell: str, view: str = "schematic") -> str:
    """Let ADS generate the netlist of an existing schematic (recommended production path)."""
    try:
        from keysight.ads import de  # type: ignore
        from keysight.ads.de import db_uu as db  # type: ignore
    except ImportError as exc:
        raise ADSAPIUnavailable(f"keysight.ads.de not importable: {exc}") from exc
    if not de.workspace_is_open():
        de.open_workspace(workspace)
    design = db.open_design((lib, cell, view))
    return design.generate_netlist()
