"""Write your own design in Python: a source-follower buffer with an SKY130 NMOS."""
import numpy as np

from analogforge.core.analyses import AC, Op
from analogforge.core.circuit import Circuit
from analogforge.core.design import Design, Measurement, MetricSpec, ParamSpec, evaluate
from analogforge.core.testbench import Testbench
from analogforge.metrics import amplifier as amp


class SourceFollower(Design):
    name = "source_follower"
    kind = "amplifier"
    parameters = [ParamSpec("W", 10e-6, 1e-6, 50e-6, "m", log=True), ParamSpec("IB", 100e-6, 10e-6, 1e-3, "A", log=True)]
    metrics = [MetricSpec("gain", "V/V"), MetricSpec("f3db", "Hz", goal="max")]

    def testbenches(self, p, ctx):
        c = Circuit(self.name)
        c.vsource("VDD", "vdd", dc=1.8 * ctx.vdd_scale)
        c.vsource("VIN", "in", dc=1.2, ac=1.0)
        c.nmos("M1", "vdd", "in", "out", "0", p["W"], 0.15e-6)
        c.isource("IB", "out", "0", dc=p["IB"])
        c.capacitor("CL", "out", "0", 1e-12)
        return [Testbench("main", c, [Op("op"), AC("ac", 1e3, 1e11, 20)], save_op=["M1"])]

    def measure(self, p, results, ctx):
        ds = results["main"]["ac"]
        h = ds.v("out")
        return Measurement({"gain": amp.dc_gain(ds.sweep, h), "f3db": amp.bandwidth_3db(ds.sweep, h)})


r = evaluate(SourceFollower(), {"W": "20u"})
print(r.errors, r.metrics)
