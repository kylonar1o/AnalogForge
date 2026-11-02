"""Simulator-neutral circuit intermediate representation (IR).

All values are SI floats.  A :class:`Circuit` is flat (no hierarchy) and is
rendered into a concrete netlist by a backend netlister together with a PDK
profile (which turns abstract :class:`Device` instances such as ``nmos`` or
``varactor`` into PDK cells).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

GROUND = "0"


# ---------------------------------------------------------------- waveforms
@dataclass
class Sine:
    offset: float = 0.0
    amplitude: float = 0.0
    freq: float = 1e6
    delay: float = 0.0
    phase_deg: float = 0.0


@dataclass
class Pulse:
    v1: float = 0.0
    v2: float = 1.0
    delay: float = 0.0
    rise: float = 1e-9
    fall: float = 1e-9
    width: float = 1e-6
    period: float = 2e-6


@dataclass
class PWL:
    points: list[tuple[float, float]] = field(default_factory=list)


Waveform = Sine | Pulse | PWL


# ----------------------------------------------------------------- elements
@dataclass
class Element:
    name: str

    @property
    def nodes(self) -> list[str]:  # pragma: no cover - overridden
        return []


@dataclass
class Resistor(Element):
    n1: str = ""
    n2: str = ""
    r: float = 1e3

    @property
    def nodes(self):
        return [self.n1, self.n2]


@dataclass
class Capacitor(Element):
    n1: str = ""
    n2: str = ""
    c: float = 1e-12
    ic: float | None = None

    @property
    def nodes(self):
        return [self.n1, self.n2]


@dataclass
class Inductor(Element):
    n1: str = ""
    n2: str = ""
    l: float = 1e-9
    ic: float | None = None

    @property
    def nodes(self):
        return [self.n1, self.n2]


@dataclass
class MutualInductance(Element):
    l1: str = ""
    l2: str = ""
    k: float = 0.5


@dataclass
class TLine(Element):
    """Lossless transmission line (reserved for RTWO-type designs)."""

    p1: str = ""
    n1: str = ""
    p2: str = ""
    n2: str = ""
    z0: float = 50.0
    td: float = 1e-12

    @property
    def nodes(self):
        return [self.p1, self.n1, self.p2, self.n2]


@dataclass
class VSource(Element):
    p: str = ""
    n: str = GROUND
    dc: float = 0.0
    ac_mag: float = 0.0
    ac_phase: float = 0.0
    waveform: Waveform | None = None

    @property
    def nodes(self):
        return [self.p, self.n]


@dataclass
class ISource(Element):
    """Current source; positive ``dc`` flows from ``p`` through the source to ``n``."""

    p: str = ""
    n: str = GROUND
    dc: float = 0.0
    ac_mag: float = 0.0
    ac_phase: float = 0.0
    waveform: Waveform | None = None

    @property
    def nodes(self):
        return [self.p, self.n]


@dataclass
class VCVS(Element):
    p: str = ""
    n: str = ""
    cp: str = ""
    cn: str = ""
    gain: float = 1.0

    @property
    def nodes(self):
        return [self.p, self.n, self.cp, self.cn]


@dataclass
class VCCS(Element):
    p: str = ""
    n: str = ""
    cp: str = ""
    cn: str = ""
    gm: float = 1e-3

    @property
    def nodes(self):
        return [self.p, self.n, self.cp, self.cn]


@dataclass
class Device(Element):
    """A PDK device (``nmos``, ``pmos``, ``varactor``, ...) rendered via the PDK profile."""

    kind: str = "nmos"
    pins: dict[str, str] = field(default_factory=dict)
    params: dict[str, float] = field(default_factory=dict)

    @property
    def nodes(self):
        return list(self.pins.values())


@dataclass
class Raw(Element):
    """Backend-specific verbatim text, e.g. ``Raw('x', {'ngspice': 'B1 ...'})``."""

    text: dict[str, str] = field(default_factory=dict)
    node_list: list[str] = field(default_factory=list)

    @property
    def nodes(self):
        return list(self.node_list)


# ------------------------------------------------------------------ circuit
class CircuitError(ValueError):
    pass


@dataclass
class Circuit:
    name: str = "circuit"
    elements: list[Element] = field(default_factory=list)
    ics: dict[str, float] = field(default_factory=dict)
    nodesets: dict[str, float] = field(default_factory=dict)
    comments: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- generic
    def add(self, element: Element) -> Element:
        if any(e.name.lower() == element.name.lower() for e in self.elements):
            raise CircuitError(f"duplicate element name {element.name!r} in circuit {self.name!r}")
        self.elements.append(element)
        return element

    def __getitem__(self, name: str) -> Element:
        for e in self.elements:
            if e.name.lower() == name.lower():
                return e
        raise KeyError(name)

    def __contains__(self, name: str) -> bool:
        return any(e.name.lower() == name.lower() for e in self.elements)

    @property
    def devices(self) -> list[Device]:
        return [e for e in self.elements if isinstance(e, Device)]

    def device_kinds(self) -> set[str]:
        return {d.kind for d in self.devices}

    def nodes(self) -> set[str]:
        out: set[str] = set()
        for e in self.elements:
            out.update(e.nodes)
        return out

    # ------------------------------------------------------------- helpers
    def resistor(self, name, n1, n2, r):
        return self.add(Resistor(name, n1, n2, float(r)))

    def capacitor(self, name, n1, n2, c, ic=None):
        return self.add(Capacitor(name, n1, n2, float(c), ic))

    def inductor(self, name, n1, n2, l, ic=None):
        return self.add(Inductor(name, n1, n2, float(l), ic))

    def lossy_inductor(self, name: str, n1: str, n2: str, l: float, q: float, f0: float) -> None:
        """Inductor with series resistance R = 2*pi*f0*L/Q (Q specified at ``f0``)."""
        mid = f"{name.lower()}_int"
        self.inductor(name, n1, mid, l)
        self.resistor(f"{name}_rs", mid, n2, 2 * math.pi * f0 * l / q)

    def mutual(self, name, l1, l2, k):
        return self.add(MutualInductance(name, l1, l2, float(k)))

    def vsource(self, name, p, n=GROUND, dc=0.0, ac=0.0, waveform=None, ac_phase=0.0):
        return self.add(VSource(name, p, n, float(dc), float(ac), float(ac_phase), waveform))

    def isource(self, name, p, n=GROUND, dc=0.0, ac=0.0, waveform=None):
        return self.add(ISource(name, p, n, float(dc), float(ac), 0.0, waveform))

    def vcvs(self, name, p, n, cp, cn, gain):
        return self.add(VCVS(name, p, n, cp, cn, float(gain)))

    def vccs(self, name, p, n, cp, cn, gm):
        return self.add(VCCS(name, p, n, cp, cn, float(gm)))

    def device(self, name: str, kind: str, pins: dict[str, str], **params) -> Device:
        return self.add(Device(name, kind, dict(pins), {k: float(v) for k, v in params.items()}))

    def nmos(self, name, d, g, s, b, w, l, nf=1, m=1):
        return self.device(name, "nmos", {"d": d, "g": g, "s": s, "b": b}, w=w, l=l, nf=nf, m=m)

    def pmos(self, name, d, g, s, b, w, l, nf=1, m=1):
        return self.device(name, "pmos", {"d": d, "g": g, "s": s, "b": b}, w=w, l=l, nf=nf, m=m)

    def varactor(self, name, gate, well, w, l, m=1, bulk=GROUND):
        """MOS varactor; capacitance rises when V(gate) - V(well) increases."""
        return self.device(name, "varactor", {"g": gate, "nw": well, "b": bulk}, w=w, l=l, m=m)

    def raw(self, name: str, text: dict[str, str], nodes: Iterable[str] = ()):
        return self.add(Raw(name, dict(text), list(nodes)))

    def add_servo(self, name: str, sense: str, ref: str, out: str, gain: float = 1000.0) -> None:
        """DC operating-point servo for open-loop testbenches.

        Drives ``out`` with ``gain*(V(sense)-V(ref))`` through a 1 GH inductor and
        AC-grounds it with 1 F: at DC the loop forces V(sense) ~= V(ref); for any
        AC frequency >= 0.01 Hz the loop is open (|Z_L| > 6e7 Ohm, |Z_C| < 16 Ohm).
        """
        self.vcvs(f"E{name}", f"{name.lower()}_e", GROUND, sense, ref, gain)
        self.inductor(f"L{name}", f"{name.lower()}_e", out, 1e9)
        self.capacitor(f"C{name}", out, GROUND, 1.0)

    # ---------------------------------------------------------- validation
    def validate(self) -> list[str]:
        """Return a list of warnings (dangling nodes etc.); raise on hard errors."""
        if not self.elements:
            raise CircuitError("circuit is empty")
        counts: dict[str, int] = {}
        for e in self.elements:
            for n in e.nodes:
                if not n:
                    raise CircuitError(f"element {e.name} has an unconnected (empty) pin")
                counts[n] = counts.get(n, 0) + 1
        warnings = []
        if GROUND not in counts:
            warnings.append("no element is connected to ground node '0'")
        for n, c in counts.items():
            if c < 2 and n != GROUND:
                warnings.append(f"node {n!r} is connected to only one element")
        inductors = {e.name.lower() for e in self.elements if isinstance(e, Inductor)}
        for e in self.elements:
            if isinstance(e, MutualInductance):
                for ln in (e.l1, e.l2):
                    if ln.lower() not in inductors:
                        raise CircuitError(f"mutual inductance {e.name} references unknown inductor {ln}")
                if not 0 < abs(e.k) <= 1:
                    raise CircuitError(f"mutual inductance {e.name}: |k| must be in (0, 1]")
        return warnings
