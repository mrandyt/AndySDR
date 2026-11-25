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
                              freq=167_925_000, level_db=-80.0, ag_cur=None, ag_freq=None, ag_over=0, ag_t=0.0, ag_log_t=0.0, job_gain=None,
                              ag_quiet_t=None, ag_raised_t=0.0, ag_hold_t=0.0, ag_ovl_seen=0, ag_ovl_t=0.0)
    r._knob = lambda: E.Radio._knob(r)
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

# --- climbing back after the strong signal has gone
r5 = radio(gain=90.0); t = 1000.0
r5.level_db = -30.0
for _ in range(40): t += 0.05; tick(r5, t)
eased = r5.dev.sets[-1]
check("strong signal: eased", eased < 90.0, r5.dev.sets)
r5.level_db = -80.0
n = len(r5.dev.sets)
for _ in range(int(4 / 0.05)): t += 0.05; tick(r5, t)
check("a few quiet seconds are not enough to climb back", len(r5.dev.sets) == n, r5.dev.sets[n:])
for _ in range(int(4 / 0.05)): t += 0.05; tick(r5, t)
check("after 6 quiet seconds it climbs one step", r5.dev.sets[n:] == [eased + 6.0], r5.dev.sets[n:])
for _ in range(int(60 / 0.05)): t += 0.05; tick(r5, t)
check("and carries on up to the knob, never past it", r5.dev.sets[-1] == 90.0 and max(r5.dev.sets) <= 90.0, r5.dev.sets)
# hunting guard: pushed down again soon after a climb -> hold
r6 = radio(gain=90.0); t = 2000.0
r6.level_db = -30.0
for _ in range(40): t += 0.05; tick(r6, t)
r6.level_db = -80.0
for _ in range(int(7.5 / 0.05)): t += 0.05; tick(r6, t)           # one climb
r6.level_db = -30.0
for _ in range(60): t += 0.05; tick(r6, t)                        # strong again at once
low = r6.dev.sets[-1]
r6.level_db = -80.0
n = len(r6.dev.sets)
for _ in range(int(30 / 0.05)): t += 0.05; tick(r6, t)
check("pushed down again soon after a climb: it waits a minute before climbing again", len(r6.dev.sets) == n, r6.dev.sets[n:])
for _ in range(int(40 / 0.05)): t += 0.05; tick(r6, t)
check("... then climbs again", r6.dev.sets[-1] > low, r6.dev.sets)

# --- the receiver's own overload report eases the gain even when the channel itself is quiet
r7 = radio(gain=100.0); r7.dev.overloads = 0; t = 3000.0
r7.level_db = -90.0
for _ in range(20): t += 0.05; tick(r7, t)
check("no overload, quiet channel: nothing", r7.dev.sets == [])
r7.dev.overloads = 2
t += 0.05; tick(r7, t)
check("an overload report eases the gain at once", r7.dev.sets == [94.0], r7.dev.sets)
for _ in range(int(2 / 0.05)):
    t += 0.05; r7.dev.overloads += 1; tick(r7, t)
check("repeated overloads keep easing, but not below half the range from the knob", min(r7.dev.sets) >= 50.0 and len(r7.dev.sets) >= 2, r7.dev.sets)
n = len(r7.dev.sets)
for _ in range(int(5 / 0.05)): t += 0.05; tick(r7, t)
check("it does not climb back while overloads are recent", len(r7.dev.sets) == n, r7.dev.sets[n:])
print("ALL PASS" if not fails else "FAILED: " + "; ".join(fails))
sys.exit(1 if fails else 0)
