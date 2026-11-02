"""SI-prefix parsing and formatting following SPICE conventions.

SPICE scale factors are case-insensitive, so ``1M`` means *milli* (1e-3)
and ``1MEG`` means mega (1e6).  We follow that convention everywhere so that
values typed in the CLI/UI behave exactly as they would inside a netlist.
"""

from __future__ import annotations

import math
import re

_SPICE_SCALE = [
    ("meg", 1e6),
    ("mil", 25.4e-6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("µ", 1e-6),  # micro sign
    ("μ", 1e-6),  # greek mu
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
    ("a", 1e-18),
]

_NUM_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-zµμ]*)\s*$")


def parse_si(value: object) -> float:
    """Parse a number with an optional SPICE scale suffix.

    >>> parse_si("10u")
    1e-05
    >>> parse_si("2.2k")
    2200.0
    >>> parse_si("1meg")
    1000000.0
    >>> parse_si(3)
    3.0
    """
    if isinstance(value, bool):
        raise ValueError(f"cannot interpret boolean {value!r} as a number")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"cannot parse {value!r} as a number")
    m = _NUM_RE.match(value)
    if not m:
        raise ValueError(f"cannot parse {value!r} as a number (examples: 10u, 2.2k, 1meg, 1e-6)")
    number = float(m.group(1))
    suffix = m.group(2).lower()
    if not suffix:
        return number
    for prefix, scale in _SPICE_SCALE:
        if suffix.startswith(prefix):
            return number * scale
    # Unknown letters (e.g. a bare unit like "V" or "Ohm") are ignored, as in SPICE.
    return number


_ENG_PREFIX = {
    -18: "a",
    -15: "f",
    -12: "p",
    -9: "n",
    -6: "µ",
    -3: "m",
    0: "",
    3: "k",
    6: "M",
    9: "G",
    12: "T",
}


def format_si(value: float, unit: str = "", digits: int = 4) -> str:
    """Human-readable engineering notation, e.g. ``format_si(1.5e-6, 'A') -> '1.5 µA'``."""
    if value is None:
        return "n/a"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return ("-" if value < 0 else "") + "inf"
    if value == 0:
        return f"0 {unit}".strip()
    exp3 = int(math.floor(math.log10(abs(value)) / 3) * 3)
    exp3 = max(-18, min(12, exp3))
    scaled = value / 10**exp3
    text = f"{scaled:.{digits}g}"
    return f"{text} {_ENG_PREFIX[exp3]}{unit}".strip()


def spice_num(value: float) -> str:
    """Format a float for inclusion in a SPICE netlist (plain exponent notation)."""
    value = float(value)
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"
