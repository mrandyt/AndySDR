"""Bias-T watcher: baseline, drop detection, re-baseline on change, on/off verdicts."""
import os, sys, tempfile
os.environ["APPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr.biasmon import BiasMonitor, judge
from andysdr.engine import Radio
fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else "")); fails += 0 if ok else 1

m = BiasMonitor(); t = 1000.0; K = ("c", 1, "Antenna B", 2e6, True)
for i in range(20):
    m.update(-80.0, K, t + i)
check("baseline taken after settling", m.baseline is not None and abs(m.baseline + 80) < 0.5, m.baseline)
check("ok while steady", m.state(True, True, t + 20) == "ok")
for i in range(20, 40):
    m.update(-90.0, K, t + i)
check("drop flagged after a sustained fall", m.state(True, True, t + 40) == "drop", m.snapshot(True, True, "Antenna B", 0, t + 40))
m.update(-90.0, ("c2", 1, "Antenna B", 2e6, True), t + 41)
check("retune re-baselines (no false alarm)", m.baseline is None and m.state(True, True, t + 41) == "settling")
check("off state", m.state(False, True) == "off")
check("wrong port state", m.state(True, False) == "no_path")
check("not supported", m.state(None, False) == "unsupported")
check("verdict: powered LNA", judge(-70, -80)[0] == "ok")
check("verdict: nothing powered", judge(-80, -80.5)[0] == "none")
check("verdict: worse", judge(-86, -80)[0] == "worse")
m2 = BiasMonitor()
for i in range(100):
    m2.update(-80, K, t + i, overloads=i // 5)
check("overload rate ~12/min", 9 < m2.overload_rate(t + 99) < 15, m2.overload_rate(t + 99))

class D:
    def __init__(s, hw, ant, bias): s.hw, s.antenna, s.flags = hw, ant, {"bias_t": bias}
class R:
    _bias_state = Radio._bias_state
    def __init__(s, d): s.dev = d
check("RSP2 antenna A: not effective", R(D(2, "Antenna A", True))._bias_state() == (True, False))
check("RSP2 antenna B: effective", R(D(2, "Antenna B", True))._bias_state() == (True, True))
class RT:
    def __init__(s): s.bias = True
    def write_setting(s, k, v): pass
check("RTL bias state", R(RT())._bias_state() == (True, True))
class Plain: pass
check("device without bias-T", R(Plain())._bias_state() == (None, False))
print("FAILED" if fails else "ALL OK"); sys.exit(1 if fails else 0)
