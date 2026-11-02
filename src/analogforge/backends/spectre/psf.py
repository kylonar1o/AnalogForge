"""Parser for Spectre PSF-ASCII result files (``spectre -format psfascii``).

Supported structure (PSF ASCII, as written by Spectre)::

    HEADER  "key" value ...
    TYPE    "name" FLOAT DOUBLE PROP( ... )   |  "name" COMPLEX DOUBLE ...  |  "name" STRUCT( ... )
    SWEEP   "time" "sweep" PROP( ... )                      (swept results only)
    TRACE   "out" "V"  ...  | "grp" GROUP n "a" "V" ...     (swept results only)
    VALUE   swept:      "time" 0.0 "out" 1.8 ...  (repeats per point)
            non-swept:  "out" "V" 1.8   "M1" "bsim4" (1e-3 2e-4 ...)
    END

Complex values are written as ``(re im)``.  This parser was validated on
files written to this documented layout (see tests); Spectre versions may add
sections it ignores.  For anything it cannot read, run Spectre with
``-format nutascii`` instead - that output is parsed by the SPICE raw reader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([()])|([^\s()"]+)')
SECTIONS = {"HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END"}


class PSFError(RuntimeError):
    pass


@dataclass
class _Tok:
    kind: str   # "str", "paren", "word"
    text: str


def tokenize(text: str) -> list[_Tok]:
    out = []
    for m in _TOKEN.finditer(text):
        if m.group(1) is not None:
            out.append(_Tok("str", m.group(1)))
        elif m.group(2) is not None:
            out.append(_Tok("paren", m.group(2)))
        else:
            out.append(_Tok("word", m.group(3)))
    return out


@dataclass
class PSFData:
    header: dict = field(default_factory=dict)
    types: dict = field(default_factory=dict)        # name -> {"kind": "float"/"complex"/"struct", "members": [...]}
    sweep_name: str | None = None
    traces: list[tuple[str, str]] = field(default_factory=list)
    sweep: np.ndarray | None = None
    signals: dict[str, np.ndarray] = field(default_factory=dict)
    scalars: dict[str, object] = field(default_factory=dict)


def _skip_prop(toks: list[_Tok], i: int) -> int:
    """Skip a parenthesized PROP(...) block starting at the word PROP."""
    if i < len(toks) and toks[i].kind == "word" and toks[i].text.upper() == "PROP":
        i += 1
        if i < len(toks) and toks[i].text == "(":
            depth = 0
            while i < len(toks):
                if toks[i].text == "(":
                    depth += 1
                elif toks[i].text == ")":
                    depth -= 1
                    if depth == 0:
                        return i + 1
                i += 1
    return i


def _parse_typedef(toks: list[_Tok], i: int) -> tuple[dict, int]:
    """Parse the part after a type name: FLOAT DOUBLE / COMPLEX DOUBLE / STRUCT( members ) [PROP(...)]."""
    kind = "float"
    members: list[str] = []
    while i < len(toks) and toks[i].kind == "word" and toks[i].text.upper() not in ("PROP", "STRUCT") \
            and toks[i].text.upper() not in SECTIONS:
        w = toks[i].text.upper()
        if w == "COMPLEX":
            kind = "complex"
        elif w in ("STRING", "INT", "BYTE", "LONG") and kind == "float":
            kind = "string" if w == "STRING" else "float"
        i += 1
    if i < len(toks) and toks[i].kind == "word" and toks[i].text.upper() == "STRUCT":
        kind = "struct"
        i += 1
        if toks[i].text != "(":
            raise PSFError("STRUCT without '('")
        i += 1
        while i < len(toks) and toks[i].text != ")":
            if toks[i].kind == "str":
                members.append(toks[i].text)
                i += 1
                sub, i = _parse_typedef(toks, i)  # member type
                continue
            i += 1
        i += 1  # ')'
    i = _skip_prop(toks, i)
    return {"kind": kind, "members": members}, i


def _read_value(toks: list[_Tok], i: int):
    """A number, a string, or a parenthesized tuple (complex value or struct members)."""
    t = toks[i]
    if t.text == "(":
        vals = []
        i += 1
        while toks[i].text != ")":
            v, i = _read_value(toks, i)
            vals.append(v)
        return vals, i + 1
    if t.kind == "str":
        return t.text, i + 1
    try:
        return float(t.text), i + 1
    except ValueError:
        return t.text, i + 1


def parse_psf_ascii(text: str) -> PSFData:
    toks = tokenize(text)
    if not toks or toks[0].text != "HEADER":
        raise PSFError("not a PSF ASCII file (missing HEADER)")
    data = PSFData()
    i = 1
    section = "HEADER"
    swept_rows: dict[str, list] = {}
    sweep_vals: list[float] = []
    trace_types: dict[str, str] = {}
    while i < len(toks):
        t = toks[i]
        if t.kind == "word" and t.text in SECTIONS:
            section = t.text
            i += 1
            if section == "END":
                break
            continue
        if section == "HEADER":
            key = t.text
            val, i = _read_value(toks, i + 1)
            data.header[key] = val
        elif section == "TYPE":
            name = t.text
            typedef, i = _parse_typedef(toks, i + 1)
            data.types[name] = typedef
        elif section == "SWEEP":
            data.sweep_name = t.text
            i += 2  # name + type
            i = _skip_prop(toks, i)
        elif section == "TRACE":
            name = t.text
            i += 1
            if i < len(toks) and toks[i].kind == "word" and toks[i].text.upper() == "GROUP":
                count = int(toks[i + 1].text)
                i += 2
                for _ in range(count):
                    data.traces.append((toks[i].text, toks[i + 1].text))
                    trace_types[toks[i].text] = toks[i + 1].text
                    i += 2
                i = _skip_prop(toks, i)
                continue
            typ = toks[i].text
            i += 1
            i = _skip_prop(toks, i)
            data.traces.append((name, typ))
            trace_types[name] = typ
        elif section == "VALUE":
            name = t.text
            i += 1
            if data.sweep_name is not None and name == data.sweep_name:
                v, i = _read_value(toks, i)
                sweep_vals.append(float(v))
                continue
            if data.sweep_name is not None and name in trace_types:
                v, i = _read_value(toks, i)
                swept_rows.setdefault(name, []).append(v)
                i = _skip_prop(toks, i)
                continue
            # non-swept: "name" "type" value
            typ = None
            if i < len(toks) and toks[i].kind == "str" and toks[i].text in data.types:
                typ = toks[i].text
                i += 1
            v, i = _read_value(toks, i)
            i = _skip_prop(toks, i)
            tdef = data.types.get(typ or "", {})
            if tdef.get("kind") == "struct" and isinstance(v, list):
                for mname, mval in zip(tdef["members"], v):
                    data.scalars[f"{name}:{mname}"] = mval
            elif isinstance(v, list) and len(v) == 2:
                data.scalars[name] = complex(v[0], v[1])
            else:
                data.scalars[name] = v
        else:  # pragma: no cover
            i += 1
    if data.sweep_name is not None:
        data.sweep = np.array(sweep_vals, float)
        for name, rows in swept_rows.items():
            if rows and isinstance(rows[0], list):
                arr = np.array([complex(r[0], r[1]) for r in rows])
            else:
                arr = np.array(rows, float)
            data.signals[name] = arr
    return data


def read_psf_ascii(path: str | Path) -> PSFData:
    return parse_psf_ascii(Path(path).read_text(errors="replace"))


def signal_key(name: str, typ: str = "") -> str:
    """Map PSF names to AnalogForge's normalized names."""
    low = name.lower()
    if ":" in low:
        inst, term = low.rsplit(":", 1)
        if term in ("p", "1", "plus"):
            return f"i({inst})"
        return f"{inst}:{term}"
    if typ.upper() in ("I", "A", "CURRENT"):
        return f"i({low})"
    return f"v({low})"
