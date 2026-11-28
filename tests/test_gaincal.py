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


def sweep(knee_db, lo=0.0, hi=49.6, n=25, noise=0.0, seed=1):
    """(gain, floor) readings from a receiver whose antenna noise equals its own noise at `knee_db` of gain."""
    import random
    r = random.Random(seed)
    out = []
    for i in range(n):
        g = lo + (hi - lo) * i / (n - 1)
        ext = 10 ** ((g - knee_db) / 10.0)              # antenna noise, relative to the receiver's own
        f = 10 * math.log10(ext + 1.0) + g * 0 - 70.0   # dBFS, the receiver's own noise as the reference
        out.append((round(g, 1), round(f + r.uniform(-noise, noise), 2)))
    return out


# --- a normal aerial: the bend part way up the range -----------------------------------------------
for knee in (10.0, 20.0, 30.0):
    r = gaincal.choose(sweep(knee))
    # the chosen gain should be above the bend (hearing the aerial) but not wastefully far above it
    check(f"bend at {knee:.0f} dB -> gain just above it", r and knee + 5 <= r["gain"] <= knee + 16,
          r and (r["gain"], r["slope"]))
    check(f"bend at {knee:.0f} dB -> the aerial is what you hear", r and r["slope"] >= 0.88, r and r["slope"])

# --- a busy band: the aerial is already louder than the receiver at minimum gain --------------------
r = gaincal.choose(sweep(-15.0))
check("a noisy band needs very little gain", r and r["gain"] <= 6.0, r and r["gain"])
check("...and it says why", r and "follows the gain" in r["why"], r and r["why"])

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
