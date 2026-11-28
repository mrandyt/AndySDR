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
    r = types.SimpleNamespace(dev=dev, s={"rf_gain": 30.0, "agc": agc, "gain_cal": {}, "squelch": -60.0},
                              scan={"state": scan}, rx=None,
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
                 "gaincal_saved", "set_gain_with_squelch", "_gcal_watch"):
        setattr(r, name, getattr(E.Radio, name).__get__(r, E.Radio))
    r.GCAL_SETTLE = E.Radio.GCAL_SETTLE
    r.GCAL_READ = E.Radio.GCAL_READ
    r.GCAL_STUCK = E.Radio.GCAL_STUCK
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

# --- the squelch follows the gain -------------------------------------------------------------------------
# Squelch is a level in dBFS, so it only means anything against a particular gain.  A test that drops the gain 30 dB
# and leaves the squelch where it was makes the scanner deaf: nothing is loud enough to open it any more.
dev5 = Dev(knee=5.0)                      # a noisy band: the answer will be far below the 30 dB it started at
r5 = radio(dev5)
r5.s["squelch"] = -60.0
r5.gaincal_start(band="Marine VHF")
run(r5, dev5)
res5 = [v for k, v in r5.emitted if k == "gaincal" and v.get("done")][0]
drop = res5["gain"] - 30.0
check("the test did lower the gain a lot", drop < -10, (res5["gain"], drop))
check("the squelch moved by the same amount", abs((float(r5.s["squelch"]) - -60.0) - drop) < 0.01,
      (r5.s["squelch"], drop))
check("...and the result says so", res5.get("squelch") is not None and res5.get("squelch_was") == -60.0, res5.get("squelch"))
check("...and so does the message", any(k == "event" and "Squelch moved" in v.get("text", "") for k, v in r5.emitted),
      [v.get("text") for k, v in r5.emitted if k == "event"])

# an open squelch is left open
dev6 = Dev(knee=5.0)
r6 = radio(dev6)
r6.s["squelch"] = -120.0
r6.gaincal_start()
run(r6, dev6)
check("a wide-open squelch is left wide open", float(r6.s["squelch"]) == -120.0, r6.s["squelch"])

# putting the gain back puts the squelch back with it
r7 = radio(Dev(knee=20.0))
r7.s["squelch"] = -60.0
r7.set_squelch = lambda db: r7.s.__setitem__("squelch", float(db))
r7.set_gain = lambda g: r7.s.__setitem__("rf_gain", float(g))
r7.set_gain_with_squelch = E.Radio.set_gain_with_squelch.__get__(r7, E.Radio)
r7.set_gain_with_squelch(29.0, 60.0)
check("using a saved gain carries the squelch", abs(float(r7.s["squelch"]) - -91.0) < 0.01, r7.s["squelch"])
r7.set_gain_with_squelch(60.0, 29.0)
check("...and putting it back puts the squelch back", abs(float(r7.s["squelch"]) - -60.0) < 0.01, r7.s["squelch"])

# --- the receiver stops delivering part-way through -----------------------------------------------
# This happened for real: another copy of the program grabbed the receiver, the driver errored, and the sweep sat at
# step 1 of 22 with the gain left at the bottom of its range - the radio deaf, and nothing on screen saying why.
devw = Dev(knee=20.0)
rw = radio(devw)
rw.s["rf_gain"] = 33.3
rw.gaincal_start(band="2 m")
t = time.time()
for _ in range(50):                                  # a few steps' worth of readings, then silence
    t += 0.05
    rw._gaincal_tick(np.full(256, devw.floor()), t)
mid = rw.gcal["i"] if rw.gcal else None
check("the sweep got going first", mid is not None and mid >= 2, mid)
for _ in range(400):                                 # the loop keeps turning, but no samples arrive
    if rw.gcal is None:
        break
    t += 0.05
    rw._gcal_watch(t)
check("a stalled sweep is given up on", rw.gcal is None, mid)
check("...before it has waited an absurd length of time", t - time.time() < 30.0, round(t - time.time(), 1))
stop = [v for k, v in rw.emitted if k == "gaincal" and v.get("running") is False]
check("...and it says why on screen", stop and "stopped sending samples" in stop[-1].get("why", ""), stop[-1:])
check("...and the gain goes back where it was", abs(devw.gain - 33.3) < 0.05, devw.gain)
check("...and nothing is saved from a part-finished sweep", rw.gaincal_saved() == [], rw.gaincal_saved())
dev2 = Dev(knee=20.0)
r2 = radio(dev2)
r2.gaincal_start(band="2 m")
t = time.time()
for _ in range(40):
    t += 0.05
    r2._gcal_watch(t)                                # called every pass of the loop, as the radio does
    r2._gaincal_tick(np.full(256, dev2.floor()), t)
check("...and keeps going", r2.gcal is not None and r2.gcal["i"] > 0, r2.gcal and r2.gcal["i"])

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
