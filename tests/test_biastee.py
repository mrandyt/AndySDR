"""Bias-T: warn when it cannot reach the antenna (RSP2 Port B only) and remember it between starts."""
import os, sys, tempfile
os.environ["APPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr.engine import Radio
fails = 0
def check(n, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + n); fails += 0 if ok else 1
class Dev:
    def __init__(self, hw, ant, bias): self.hw, self.antenna, self.flags, self.key = hw, ant, {"bias_t": bias}, "rsp:1"
class Fake:
    _bias_check = Radio._bias_check
    def __init__(self, dev): self.dev, self.ev = dev, []
    def _emit(self, k, v): self.ev.append(v)
f = Fake(Dev(2, "Antenna A", True)); f._bias_check(); check("warns: bias-T on, Antenna A", len(f.ev) == 1 and "Antenna B" in f.ev[0]["text"])
f = Fake(Dev(2, "", True)); f._bias_check(); check("warns: default input is A", len(f.ev) == 1)
f = Fake(Dev(2, "Hi-Z", True)); f._bias_check(); check("warns: Hi-Z", len(f.ev) == 1)
f = Fake(Dev(2, "Antenna B", True)); f._bias_check(); check("quiet on Antenna B", not f.ev)
f = Fake(Dev(2, "Antenna A", False)); f._bias_check(); check("quiet when bias-T is off", not f.ev)
f = Fake(Dev(6, "Antenna", True)); f._bias_check(); check("quiet on RSP1A (single port)", not f.ev)
print("FAILED" if fails else "ALL OK"); sys.exit(1 if fails else 0)
