"""Auto RF gain: eases the gain down on a strong signal, never above the knob, restarts on a new frequency."""
import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import engine as E

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    if not ok: fails.append(name)

class Dev:
    agc = False
    def __init__(s): s.sets = []
    def gain_range(s): return (0.0, 100.0)
    def set_gain(s, g): s.sets.append(g)

def radio(auto=True, gain=90.0, agc=False):
    r = types.SimpleNamespace(dev=Dev(), s={"rf_gain": gain, "agc": agc, "auto_gain": auto}, scan={"state": "manual"},
                              freq=167_925_000, level_db=-80.0, ag_cur=None, ag_freq=None, ag_over=0, ag_t=0.0, ag_log_t=0.0)
    return r
tick = lambda r, t: E.Radio._autogain_tick(r, t)

r = radio()
t = 100.0
for _ in range(40):                       # quiet: nothing happens
    t += 0.05; tick(r, t)
check("a quiet channel never touches the gain", r.dev.sets == [])

r.level_db = -30.0                         # strong signal
for _ in range(5):
    t += 0.05; tick(r, t)
check("a short burst is ignored (needs 6 blocks)", r.dev.sets == [])
t += 0.05; tick(r, t)
check("a sustained strong signal lowers the gain one step", r.dev.sets == [84.0], r.dev.sets)
for _ in range(10):
    t += 0.05; tick(r, t)
check("it waits for the radio to settle before the next step", r.dev.sets == [84.0], r.dev.sets)
for _ in range(400):
    t += 0.05; tick(r, t)
check("steps continue but stop at the floor (35% of the range below the knob)", r.dev.sets[-1] == 55.0 and min(r.dev.sets) == 55.0, r.dev.sets)
check("it never goes above the knob", max(r.dev.sets) <= 90.0)

n = len(r.dev.sets)
r.freq = 168_000_000; r.level_db = -80.0
t += 0.05; tick(r, t)
check("a new frequency starts again from the knob", r.dev.sets[n:] == [90.0], r.dev.sets[n:])

r.level_db = -30.0
for _ in range(10):
    t += 0.05; tick(r, t)
check("and eases down again if that one is strong too", r.dev.sets[-1] == 84.0, r.dev.sets)

r.s["auto_gain"] = False
t += 0.05; tick(r, t)
check("switching it off puts the knob's value back", r.dev.sets[-1] == 90.0 and r.ag_cur is None, r.dev.sets)
n = len(r.dev.sets)
for _ in range(30):
    t += 0.05; tick(r, t)
check("and then does nothing", len(r.dev.sets) == n)

r2 = radio(auto=True, agc=True); r2.level_db = -30.0
for _ in range(30): t += 0.05; tick(r2, t)
check("with hardware AGC on it stays out of the way", r2.dev.sets == [])
r3 = radio(); r3.level_db = -30.0; r3.scan["state"] = "scan"
for _ in range(30): t += 0.05; tick(r3, t)
check("while scanning it stays out of the way", r3.dev.sets == [])
r4 = radio(); r4.dev = None
tick(r4, 1.0)
check("with no receiver it does nothing and does not fail", True)
print("ALL PASS" if not fails else "FAILED: " + "; ".join(fails))
sys.exit(1 if fails else 0)
