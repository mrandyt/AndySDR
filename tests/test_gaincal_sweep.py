"""The gain-calibration sweep as the radio runs it: stepping, settling, reading, choosing and saving.

The receiver is simulated the same way as in test_gaincal: what it measures is the aerial's noise amplified by the
gain, plus its own noise, which the gain does not change.
"""
import math, os, sys, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
from andysdr import engine as E

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


class Dev:
    agc = False
    key = "rtl:0"

    def __init__(self, knee=20.0, steps=None, overload_above=None):
        self.gain = 0.0
        self.knee = knee
        self.steps = steps
        self.overloads = 0
        self.overload_above = overload_above
        self.sets = []

    def gain_range(self):
        return (0.0, 49.6)

    @property
    def gain_steps(self):
        return self.steps

    def set_gain(self, g):
        self.gain = float(g)
        self.sets.append(float(g))
        if self.overload_above is not None and self.gain >= self.overload_above:
            self.overloads += 1

    def floor(self):
        return 10 * math.log10(10 ** ((self.gain - self.knee) / 10.0) + 1.0) - 70.0


def radio(dev, job=None, scan="manual", agc=False):
    r = types.SimpleNamespace(dev=dev, s={"rf_gain": 30.0, "agc": agc, "gain_cal": {}}, scan={"state": scan},
                              freq=137_900_000, job=job, job_gain=None, ag_cur=None, gcal=None, events=[], emitted=[])
    r.s = dict(r.s)
    r.s["save"] = None
    class S(dict):
        def save(self):
            pass
    sd = S(r.s)
    sd.pop("save", None)
    r.s = sd
    r._knob = lambda: E.Radio._knob(r)
    r._emit = lambda k, v: r.emitted.append((k, v))
    r._push_state = lambda *a, **k: None
    for name in ("gaincal_start", "gaincal_stop", "_gcal_next", "_gaincal_tick", "_gcal_finish", "_gcal_save",
                 "gaincal_saved"):
        setattr(r, name, getattr(E.Radio, name).__get__(r, E.Radio))
    r.GCAL_SETTLE = E.Radio.GCAL_SETTLE
    r.GCAL_READ = E.Radio.GCAL_READ
    return r


def run(r, dev, t0=None, limit=4000):
    """Drive the sweep the way the radio's own loop does: one 50 ms block at a time."""
    t = start = time.time() if t0 is None else t0
    for _ in range(limit):
        if r.gcal is None:
            break
        t += 0.05
        spec = np.full(256, dev.floor()) + np.random.default_rng(1).normal(0, 0.2, 256)
        r._gaincal_tick(spec, t)
    return t - start


# --- a normal sweep ------------------------------------------------------------------------------
dev = Dev(knee=20.0)
r = radio(dev)
ok, why = r.gaincal_start(band="2 m")
check("the sweep starts", ok and r.gcal is not None, why)
secs = run(r, dev)
done = [v for k, v in r.emitted if k == "gaincal" and v.get("done")]
check("it finishes and gives an answer", len(done) == 1, [k for k, _ in r.emitted][-3:])
res = done[0]
check("the gain is just above the bend", 25 <= res["gain"] <= 36, res["gain"])
check("the receiver was actually set to it", abs(dev.gain - res["gain"]) < 0.05, dev.gain)
check("and the knob was saved", abs(float(r.s["rf_gain"]) - res["gain"]) < 0.05, r.s["rf_gain"])
check("it took a sensible length of time", 8 < secs < 40, round(secs, 1))
check("every step was tried once", len(res["points"]) == len(set(p[0] for p in res["points"])), len(res["points"]))
check("progress was reported as it went", sum(1 for k, v in r.emitted if k == "gaincal" and v.get("running")) >= 5)

# --- it is remembered against the band and receiver ---------------------------------------------
saved = r.gaincal_saved()
check("the answer is saved for this band", len(saved) == 1 and saved[0]["band"] == "2 m", saved)
check("...with the receiver it was measured on", saved and saved[0]["dev"] == "rtl:0", saved)

# a second band is kept alongside the first
r.freq = 446_000_000
r.gaincal_start(band="PMR446")
run(r, dev)
check("a second band does not overwrite the first", len(r.gaincal_saved()) == 2, r.gaincal_saved())

# --- the receiver's own gain steps are used when it has them --------------------------------------
steps = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7,
         32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6]
dev2 = Dev(knee=20.0, steps=steps)
r2 = radio(dev2)
r2.gaincal_start()
run(r2, dev2)
res2 = [v for k, v in r2.emitted if k == "gaincal" and v.get("done")][0]
check("only gains the receiver really has are used", all(p[0] in steps for p in res2["points"]),
      [p[0] for p in res2["points"]][:6])
check("and the one chosen is a real step", res2["gain"] in steps, res2["gain"])

# --- overload stops it climbing -------------------------------------------------------------------
dev3 = Dev(knee=35.0, overload_above=24.0)
r3 = radio(dev3)
r3.gaincal_start()
run(r3, dev3)
res3 = [v for k, v in r3.emitted if k == "gaincal" and v.get("done")][0]
check("overload holds the gain down", res3["gain"] <= 24.0, res3["gain"])
check("...and the reason mentions it", "overload" in res3["why"].lower(), res3["why"])

# --- it refuses when it would get in the way ---------------------------------------------------------
for name, kw, word in (("a capture is running", {"job": {"name": "Meteor"}}, "capture"),
                       ("the radio is scanning", {"scan": "scan"}, "scanning"),
                       ("hardware AGC is on", {"agc": True}, "AGC")):
    rr = radio(Dev(), **kw)
    ok, why = rr.gaincal_start()
    check("it refuses while " + name, not ok and word.lower() in why.lower(), why)
rr = radio(Dev())
rr.gaincal_start()
ok, why = rr.gaincal_start()
check("it refuses to start twice", not ok and "already running" in why, why)

# --- stopping half way puts the gain back --------------------------------------------------------------
dev4 = Dev(knee=20.0)
r4 = radio(dev4)
r4.s["rf_gain"] = 31.0
r4.gaincal_start()
t = time.time()
for _ in range(30):
    t += 0.05
    r4._gaincal_tick(np.full(256, dev4.floor()), t)
r4.gaincal_stop()
check("stopping puts the old gain back", abs(dev4.gain - 31.0) < 0.05, dev4.gain)
check("...and nothing is saved", r4.gaincal_saved() == [], r4.gaincal_saved())

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
