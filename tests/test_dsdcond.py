import os, sys, tempfile
os.environ["HOME"] = os.environ["APPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from andysdr.decoders import DiscConditioner, DSD_MODES
fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n, "" if ok else extra)
    fails += 0 if ok else 1
rng = np.random.default_rng(1)
lv = np.array([-3, -1, 1, 3]) * 0.254 / 3            # DMR-like 4FSK levels in rad/sample at 48 kS/s
sym = rng.choice(lv, 4800 * 6)
sig = np.repeat(sym, 10).astype(np.float32)           # 10 samples per symbol
off = 0.13                                            # ~1 kHz tuning error
c = DiscConditioner()
out = np.concatenate([c.process(sig[i:i + 2400] + off) for i in range(0, len(sig), 2400)])
tail = out[-48000:]
check("offset removed (mean ~0)", abs(float(tail.mean())) < 0.01, tail.mean())
check("symbol levels preserved", abs(float(tail.std()) - float(sig.std())) < 0.01, (tail.std(), sig.std()))
c2 = DiscConditioner()
for i in range(0, 48000, 2400):
    c2.process(sig[i:i + 2400] + off)
before = c2.dc
c2.process(rng.normal(0, 1, 2400).astype(np.float32) + 5, signal_present=False)
check("estimate frozen while squelch closed", c2.dc == before)
check("new sub-modes present", DSD_MODES["dstar"] == ["-fd"] and DSD_MODES["dmr_inv"] == ["-fs", "-xr"] and DSD_MODES["dpmr_inv"] == ["-fm", "-xd"])
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
