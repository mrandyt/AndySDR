"""Channel bandwidth selection: the filter really changes width, defaults are unchanged, per-mode memory works."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from andysdr.dsp import Receiver, BANDWIDTHS, MODES, default_bandwidth

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    print(("  ok   " if cond else "  FAIL ") + name, extra if not cond else "")
    ok += cond; fail += (not cond)

FS = 2400000
T = np.arange(FS // 10)
def level(r, off):
    iq = np.exp(2j * np.pi * off * T / FS).astype(np.complex64)
    for _ in range(3):
        r.process(iq, 0)
    return r.level_db

for m, lst in BANDWIDTHS.items():
    d = [h for h, t in lst if "default" in t]
    check(f"{m}: one default in the list equal to the mode's own width", m == "WFM" or d == [default_bandwidth(m)], d)

r = Receiver(FS); r.set_mode("NFM")
check("NFM default width 12 kHz", r.bandwidth() == 12000)
check("NFM 12k passes a carrier 5 kHz off", level(r, 5000) > -3)
r.set_bandwidth(6000)
check("NFM 6k blocks it", level(r, 5000) < -40, level(r, 5000))
r.set_bandwidth(0)
check("0 = back to default", r.bandwidth() == 12000)

r.set_mode("USB"); r.set_bandwidth(2400)
check("USB 2.4k passes +1 kHz, attenuates +2.8 kHz", level(r, 1000) > -3 and level(r, 2800) < -10)
r.set_bandwidth(3600)
check("USB 3.6k passes +2.8 kHz", level(r, 2800) > -3)
r.set_mode("LSB"); r.set_bandwidth(2400)
check("LSB mirrors: -1 kHz passes, +1 kHz does not", level(r, -1000) > -3 and level(r, 1000) < -30)

r.set_mode("CW"); r.set_bandwidth(100)
check("CW 100 Hz: passes 0, blocks 300 Hz", level(r, 0) > -3 and level(r, 300) < -40)

r.set_mode("WFM"); r.set_bandwidth(5000)
check("WFM width is fixed", r.bandwidth() == 200000)

# narrow widths 0.5 / 1 / 2 / 3 kHz exist for the voice and digital modes, and each one really filters
for m in ("NFM", "FM", "AM", "USB", "LSB", "DIG"):
    have = {h for h, t in BANDWIDTHS[m]}
    check(f"{m}: 0.5, 1, 2 and 3 kHz are selectable", {500, 1000, 2000, 3000} <= have, sorted(have))
for m in ("NFM", "AM", "USB"):
    rr = Receiver(FS); rr.set_mode(m)
    for w in (500, 1000, 2000, 3000):
        rr.set_bandwidth(w)
        lo, hi = (200, w + 1500) if m == "USB" else (w * 0.25, w * 0.5 + 1500)
        check(f"{m} {w} Hz: passes a tone inside, blocks one {w // 2 + 1500} Hz out",
              level(rr, lo) > -6 and level(rr, hi) < -25, (level(rr, lo), level(rr, hi)))

mem = {"AM": 5000}
r2 = Receiver(FS); r2.bw_lookup = lambda m: mem.get(m, 0)
r2.set_mode("AM"); check("remembered width is applied when the mode is selected", r2.bandwidth() == 5000)
r2.set_mode("NFM"); check("a mode with nothing remembered uses its default", r2.bandwidth() == 12000)
r2.set_bandwidth(10**7); check("absurd width is clamped", r2.bandwidth() <= 44000)

print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
