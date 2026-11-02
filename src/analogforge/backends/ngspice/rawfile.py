"""Reader for SPICE3/ngspice raw files (binary and ASCII).

Format reference: the ngspice manual, chapter "Output file formats".  A raw
file is a sequence of plots; each plot has a text header followed by either
``Binary:`` (little-endian float64 values, complex values as two float64) or
``Values:`` (ASCII).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


class RawFileError(RuntimeError):
    """Raised when a raw file cannot be parsed."""


@dataclass
class RawPlot:
    title: str
    plotname: str
    flags: str
    names: list[str]
    types: list[str]
    data: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def is_complex(self) -> bool:
        return "complex" in self.flags

    @property
    def scale_name(self) -> str:
        return self.names[0]

    @property
    def scale(self) -> np.ndarray:
        s = self.data[self.names[0]]
        return s.real if np.iscomplexobj(s) else s

    def has(self, name: str) -> bool:
        return self._resolve(name) is not None

    def _resolve(self, name: str) -> str | None:
        key = name.lower().strip()
        if key in self.data:
            return key
        candidates = [f"v({key})", key.replace("#branch", ""), f"i({key})"]
        if key.startswith("i(") and key.endswith(")"):
            candidates.append(key[2:-1] + "#branch")
        if key.startswith("v(") and key.endswith(")"):
            candidates.append(key[2:-1])
        for c in candidates:
            if c in self.data:
                return c
        return None

    def get(self, name: str) -> np.ndarray:
        key = self._resolve(name)
        if key is None:
            raise KeyError(f"vector {name!r} not found in plot {self.plotname!r}; available: {', '.join(self.names)}")
        return self.data[key]

    def __getitem__(self, name: str) -> np.ndarray:
        return self.get(name)

    def scalar(self, name: str) -> float:
        """First value of a vector as a real float (handy for operating points)."""
        v = self.get(name)
        x = v[0]
        return float(x.real) if np.iscomplexobj(v) else float(x)

    def to_dict(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for k, v in self.data.items():
            if np.iscomplexobj(v):
                out[k] = {"re": v.real.tolist(), "im": v.imag.tolist()}  # type: ignore[assignment]
            else:
                out[k] = v.tolist()
        return out


def _parse_header(lines: list[str]) -> tuple[dict, list[str], list[str]]:
    meta: dict[str, str] = {}
    names: list[str] = []
    types: list[str] = []
    in_vars = False
    for line in lines:
        if in_vars:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[0].isdigit():
                names.append(parts[1].lower())
                types.append(parts[2].lower())
                continue
            in_vars = False
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            if key == "Variables":
                in_vars = True
                continue
            meta[key] = value.strip()
    return meta, names, types


def parse_raw_bytes(blob: bytes) -> list[RawPlot]:
    plots: list[RawPlot] = []
    pos = 0
    n = len(blob)
    while pos < n:
        # skip whitespace between plots
        while pos < n and blob[pos:pos + 1] in (b"\n", b"\r", b" ", b"\t"):
            pos += 1
        if pos >= n:
            break
        if plots and not blob.startswith(b"Title:", pos):
            break  # trailing partial record of an interrupted simulation - ignore
        idx_bin = blob.find(b"Binary:\n", pos)
        idx_val = blob.find(b"Values:\n", pos)
        candidates = [i for i in (idx_bin, idx_val) if i >= 0]
        if not candidates:
            raise RawFileError("raw file header without 'Binary:' or 'Values:' section")
        start = min(candidates)
        binary = start == idx_bin
        header = blob[pos:start].decode("latin-1")
        meta, names, types = _parse_header(header.splitlines())
        if not names:
            raise RawFileError("raw file plot has no variables")
        nvars = int(meta.get("No. Variables", len(names)))
        npoints = int(meta.get("No. Points", "0"))
        flags = meta.get("Flags", "real").lower()
        is_complex = "complex" in flags
        data_start = start + len("Binary:\n")
        if binary:
            width = 16 if is_complex else 8
            rec = width * nvars
            available = (n - data_start) // rec
            if npoints == 0 or npoints > available:
                npoints = available
            nbytes = npoints * rec
            arr = np.frombuffer(blob, dtype="<f8", count=nbytes // 8, offset=data_start)
            if is_complex:
                arr = arr.reshape(npoints, nvars, 2)
                values = arr[:, :, 0] + 1j * arr[:, :, 1]
            else:
                values = arr.reshape(npoints, nvars)
            pos = data_start + nbytes
        else:
            text_end = blob.find(b"\nTitle:", data_start)
            text = blob[data_start: text_end if text_end >= 0 else n].decode("latin-1")
            tokens = text.split()
            rows = []
            i = 0
            dtype = complex if is_complex else float
            while i < len(tokens):
                i += 1  # point index
                row = []
                for _ in range(nvars):
                    if i >= len(tokens):
                        break
                    tok = tokens[i]
                    i += 1
                    if is_complex:
                        re_s, _, im_s = tok.partition(",")
                        row.append(complex(float(re_s), float(im_s or 0.0)))
                    else:
                        row.append(float(tok))
                if len(row) == nvars:
                    rows.append(row)
            values = np.array(rows, dtype=dtype).reshape(-1, nvars)
            pos = text_end + 1 if text_end >= 0 else n
        plot = RawPlot(
            title=meta.get("Title", ""),
            plotname=meta.get("Plotname", ""),
            flags=flags,
            names=names[:nvars],
            types=types[:nvars],
        )
        for j, name in enumerate(plot.names):
            plot.data[name] = np.array(values[:, j])
        plots.append(plot)
    return plots


def read_raw(path: str | Path) -> list[RawPlot]:
    """Read every plot contained in ``path``."""
    blob = Path(path).read_bytes()
    if not blob:
        raise RawFileError(f"raw file {path} is empty")
    return parse_raw_bytes(blob)
