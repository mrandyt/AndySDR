"""Choosing the RF gain from a noise-floor sweep, against a simulated receiver.

The model is the standard one: what the receiver measures is the antenna's noise amplified by the gain, plus the
receiver's own noise, which the gain does not change.  Where those two are equal is the bend the sweep is looking
for; the right answer is a little above it, where the aerial is what you are hearing.
"""
import math, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import gaincal

fails = 0


def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n, "" if ok else extra)
    fails += 0 if ok else 1


def sweep(knee_db, lo=0.0, hi=49.6, n=25, noise=0.0, seed=1, compress_db=None):
    """(gain, floor) readings from a receiver whose antenna noise equals its own noise at `knee_db` of gain."""
    import random
    r = random.Random(seed)
    out = []
    for i in range(n):
        g = lo + (hi - lo) * i / (n - 1)
        ext = 10 ** ((g - knee_db) / 10.0)              # antenna noise, relative to the receiver's own
        f = 10 * math.log10(ext + 1.0) + g * 0 - 70.0   # dBFS, the receiver's own noise as the reference
        out.append((round(g, 1), round(f + r.uniform(-noise, noise), 2)))
    if compress_db is not None:                         # above this the front end saturates: the floor stops rising
        base = next(f for g, f in out if g >= compress_db)
        out = [(g, f if g <= compress_db else round(base + 0.2 * (f - base), 2)) for g, f in out]
    return out


# --- a normal aerial: the bend part way up the range -----------------------------------------------
for knee in (10.0, 20.0, 30.0):
    r = gaincal.choose(sweep(knee))
    # the chosen gain should be above the bend (hearing the aerial) but not wastefully far above it
    check(f"bend at {knee:.0f} dB -> gain just above it", r and knee + 5 <= r["gain"] <= knee + 16,
          r and (r["gain"], r["slope"]))
    check(f"bend at {knee:.0f} dB -> the aerial is what you hear", r and r["slope"] >= 0.88, r and r["slope"])

# --- a preamplifier in front: the aerial is already louder than the receiver at every gain ------------
# Seen for real on a 137 MHz dongle with a powered LNA: the floor rises one-for-one from 0 dB, so the bend is below
# the range.  Nothing in the sweep says one gain beats another, so the answer must be to keep what is in use, not
# to drop the gain to the bottom (which is what it used to do).
r = gaincal.choose(sweep(-15.0), keep=20.0)
check("with a preamp the gain in use is kept", r and abs(r["gain"] - 20.0) < 0.01, r and r["gain"])
check("...and it explains that gain is not the limit here", r and "preamplifier" in r["why"], r and r["why"])
check("...and does not claim a bend it did not see", r and "follows the gain from" not in r["why"], r and r["why"])
r = gaincal.choose(sweep(-15.0))
check("with nothing to keep it falls back to the lowest tried", r and r["gain"] <= 6.0, r and r["gain"])

# --- a very quiet aerial: the receiver is still the limit at full gain ------------------------------
r = gaincal.choose(sweep(70.0))
check("a quiet aerial gets all the gain there is", r and r["gain"] >= 45.0, r and r["gain"])
check("...and says it cannot tell quiet from disconnected", r and "very quiet or" in r["why"], r and r["why"])

# --- nothing connected: the floor never moves --------------------------------------------------------
flat = [(g, -70.0) for g in range(0, 50, 2)]
r = gaincal.choose(flat)
check("a dead aerial is called out, not quietly given a number", r and "checking the aerial" in r["why"], r and r["why"])
check("...and it still uses full gain so a quiet aerial is not crippled", r and r["gain"] >= 45, r and r["gain"])

# --- the receiver reports it is being overloaded above a point ----------------------------------------
r = gaincal.choose(sweep(30.0), overload_from=28.0)
check("overload caps the answer", r and r["gain"] <= 26.0, r and r["gain"])
check("...and the reason says so", r and "overload" in r["why"].lower(), r and r["why"])

# --- measurement noise must not change the answer much -------------------------------------------------
clean = gaincal.choose(sweep(20.0))["gain"]
spread = [gaincal.choose(sweep(20.0, noise=0.8, seed=s))["gain"] for s in range(8)]
check("a noisy sweep still lands in the same place", max(abs(g - clean) for g in spread) <= 6.0,
      (clean, spread))

# --- a front end that starts to compress ------------------------------------------------------------------
# This is the case that cost a real pass: a dongle behind a mast preamplifier, where the floor follows the gain from
# the bottom and then stops following it near the top.  The bend at the top is the front end running out of headroom,
# not the aerial going quiet, and gain past it loses strong signals for nothing.
real = [(0.0, -70.0), (0.9, -67.6), (2.7, -63.0), (3.7, -61.4), (7.7, -58.6), (12.5, -56.2), (14.4, -56.5),
        (15.7, -53.5), (19.7, -52.2), (20.7, -51.1), (22.9, -49.0), (28.0, -45.4), (29.7, -44.3), (32.8, -42.0),
        (36.4, -39.5), (37.2, -38.7), (38.6, -38.1), (42.1, -37.0), (43.4, -36.7), (43.9, -36.0), (48.0, -35.7),
        (49.6, -35.4)]                      # measured, Nooelec SMArt v5 on 137.900 MHz with a preamplifier
c = gaincal.ceiling(real)
check("a real compressing front end is spotted", c is not None and 34.0 <= c <= 42.0, c)
r = gaincal.choose(real, keep=20.0)
check("...and the reason says not to go above it", "compress" in r["why"] and "do not go above" in r["why"], r["why"])
check("...and the gain in use is kept, being below it", r["gain"] == 20.0, r["gain"])
check("...and the headroom is to the ceiling, not the top of the range", 10.0 <= r["headroom"] <= 24.0, r["headroom"])
check("a sweep still climbing at the top has no ceiling", gaincal.ceiling(sweep(20.0)) is None)
check("...nor does a quiet aerial that never moved", gaincal.ceiling([(g, -70.0) for g in range(0, 50, 4)]) is None)
check("...nor does a noisy one", all(gaincal.ceiling(sweep(20.0, noise=0.8, seed=s)) is None for s in range(8)))
sim = sweep(8.0, compress_db=36.0)
c = gaincal.ceiling(sim)
# A real RSP2 sweep, which goes haywire above the gain where it reported overload: the floor drops 40 dB, climbs
# back, and ends higher than it started.  None of that is a measurement, so none of it may set a ceiling.
rsp = [(0.0, -99.0), (4.8, -99.3), (9.5, -98.3), (14.3, -98.2), (19.0, -98.6), (23.8, -98.0), (28.6, -97.1),
       (33.3, -89.9), (38.1, -88.3), (42.9, -79.9), (47.6, -78.4), (52.4, -76.8), (57.1, -72.7), (61.9, -70.0),
       (66.7, -68.5), (71.4, -62.5), (76.2, -60.5), (81.0, -58.6), (85.7, -97.0), (90.5, -98.1), (95.2, -60.0),
       (100.0, -40.1)]                     # measured, SDRplay RSP2 on 159.725 MHz
r = gaincal.choose(rsp, overload_from=85.7)
check("an overloading receiver gets no invented ceiling", r["ceiling"] is None, r["ceiling"])
check("...and the gain is still the one at the bend", 32.0 <= r["gain"] <= 35.0, r["gain"])
check("...and the headroom stops at the overload point, not the top of the dial", 48.0 <= r["headroom"] <= 52.0,
      r["headroom"])

check("a simulated compressing sweep is spotted too", c is not None and 30.0 <= c <= 40.0, c)
r = gaincal.choose(sim)
check("...and the gain chosen is never above the ceiling", r["gain"] <= c + 0.01, (r["gain"], c))

# --- the sweep plan ---------------------------------------------------------------------------------------
p = gaincal.plan(0, 49.6)
check("a plan covers the whole range, lowest first", p[0] == 0.0 and abs(p[-1] - 49.6) < 0.05 and p == sorted(p), p)
steps = [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8,
         33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6]      # a real R820T2 dongle
p = gaincal.plan(0, 49.6, steps)
check("the receiver's own gain steps are used", len(p) <= 22 and p[0] == 0.0 and p[-1] == 49.6, p)
check("...and they are a subset of what the receiver offers", set(p) <= set(float(s) for s in steps), p)
p = gaincal.plan(20, 30, steps)
check("a narrow range still gives enough points", len(p) >= gaincal.MIN_POINTS and min(p) >= 20 and max(p) <= 30, p)

# --- not enough to go on ----------------------------------------------------------------------------------
check("too few readings gives no answer", gaincal.choose([(0, -70), (10, -60)]) is None)
check("readings that failed are ignored", gaincal.choose([(g, None) for g in range(0, 50, 5)]) is None)

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
