"""Satellite pass report: SNR meter, the file, and the gain learning."""
import math
import os
import random
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import passlog  # noqa: E402

ok = True


def check(name, cond, info=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name, info)
    ok = ok and bool(cond)


# --- SNR from a spectrum frame
rng = np.random.default_rng(1)
n, fs, center = 2048, 2.4e6, 137.5e6
spec = -80 + rng.normal(0, 1.0, n)
f_sig = 137.9e6
k = int((f_sig - (center - fs / 2)) / (fs / n))
w = int(60e3 / (fs / n))
spec[k - w:k + w + 1] += 14
s = passlog.band_snr(spec, center, fs, f_sig, 60e3)
check("band_snr sees a 14 dB signal", s is not None and 12.5 < s < 15.5, round(s or 0, 1))
check("band_snr of plain noise is about 0", abs(passlog.band_snr(-80 + rng.normal(0, 1, n), center, fs, f_sig, 60e3)) < 1.5)
check("band_snr outside the view is None", passlog.band_snr(spec, center, fs, 150e6, 60e3) is None)
spec2 = -80 + rng.normal(0, 1.0, n)
spec2[100:140] += 30                                     # a strong signal elsewhere must not raise the noise floor
spec2[k - w:k + w + 1] += 14
check("a neighbouring strong signal does not fool it", 12 < passlog.band_snr(spec2, center, fs, f_sig, 60e3) < 16)

# --- meter: needs time between samples; distance-corrected score only from higher elevations
m = passlog.PassMeter(lambda ts: (45.0, 1000.0))
for i in range(40):
    m.add(100.0 + i * 1.1, 10.0 + (i % 3), 60.0)
sm = m.summary()
check("meter summary", sm["samples"] == 40 and sm["gain"] == 60.0 and sm["snr_peak"] == 12.0 and sm.get("score") == 11.0, sm)
m2 = passlog.PassMeter(lambda ts: (8.0, 2500.0))
for i in range(40):
    m2.add(100.0 + i * 1.1, 10.0, 60.0)
check("a low pass gives no score", "score" not in m2.summary())
m3 = passlog.PassMeter(lambda ts: (60.0, 500.0))
for i in range(30):
    m3.add(100.0 + i * 1.1, 10.0, 60.0)
check("range correction (closer = stronger = scaled down)", abs(m3.summary()["score"] - (10 - 6.0)) < 0.1, m3.summary().get("score"))
m4 = passlog.PassMeter()
m4.add(5.0, 3.0, 50.0)
m4.add(5.2, 3.0, 50.0)
check("samples closer than a second are dropped", m4.summary()["samples"] == 1)

# --- the file
d = tempfile.mkdtemp()
path = os.path.join(d, "passes.jsonl")
pl = passlog.PassLog(path)
pl.add({"key": "sat:a:1", "sat": "meteor_m23", "name": "Meteor-M N2-3", "action": "lrpt", "start": 1, "end": 2, "max_el": 55, "gain": 60, "snr_peak": 12, "score": 9})
pl.add({"key": "sat:b:2", "sat": "iss", "name": "ISS", "action": "sstv", "start": 3, "end": 4, "max_el": 30})
pl2 = passlog.PassLog(path)
check("passes survive a restart", len(pl2.rows) == 2 and pl2.rows[0]["gain"] == 60)
import time
now = time.time()
pl.add({"key": "sat:c:3", "sat": "meteor_m24", "name": "Meteor-M N2-4", "action": "lrpt", "start": now - 900, "end": now - 60, "max_el": 70})
check("lrpt just finished: processing", pl.view(5, now)[0]["result"] == "processing…")
pl.set_images("sat:c:3", 3, 5)
v = pl.view(5, now)[0]
check("pictures counted", v["result"] == "3 pictures" and v["images"] == 3, v["result"])
pl.set_images("sat:a:1", 0, 0, "SatDump not found")
check("no SatDump is explained", pl.result(pl.find("sat:a:1")) == "no picture – SatDump not found")
check("old pass without pictures", pl.result(pl.find("sat:b:2"), now) == "no picture")
pl.open_key = "sat:b:2"
pl.add_image("sat:b:2")
check("an image during a pass is added", pl.find("sat:b:2")["images"] == 1)
check("for_sat filters", len(pl.for_sat("iss")) == 1)
for i in range(400):
    pl.add({"key": f"k{i}", "sat": "x"})
check("file is capped", len(pl.rows) == passlog.KEEP and len(passlog.PassLog(path).rows) == passlog.KEEP)

# --- gain learning
LO, HI, KNOB = 0.0, 100.0, 80.0
step = max(1.0, round(0.08 * (HI - LO)))


def run(peak_fn, passes, seed=3):
    r = random.Random(seed)
    rows, tried = [], []
    for i in range(passes):
        g = passlog.suggest_gain(rows, KNOB, LO, HI)
        tried.append(g)
        peak = peak_fn(i)
        rows.append({"sat": "x", "gain": g, "score": 20 - 0.012 * (g - peak) ** 2 + r.gauss(0, 0.4), "gain_mode": "auto"})
    return rows, tried


rows, tried = run(lambda i: 56, 30)
b = passlog.best_gain(rows, step)
check("starts from the knob", tried[0] == KNOB)
check("stays inside the allowed range", all(KNOB - 0.40 * (HI - LO) - 1e-6 <= g <= min(HI, KNOB + 0.25 * (HI - LO)) + 1e-6 for g in tried), (min(tried), max(tried)))
check("finds the best gain", b and abs(b[0] - 56) <= step, b)
check("settles there", sum(1 for g in tried[-10:] if abs(g - 56) <= step) >= 7, tried[-10:])
rows, tried = run(lambda i: 56 if i < 20 else 44, 55)
b = passlog.best_gain(rows, step)
check("follows a change in conditions", b and abs(b[0] - 44) <= step, (b, tried[-8:]))
check("no scored passes: use the knob", passlog.suggest_gain([{"gain": 50}], 33.0, 0, 50) == 33.0)
check("narrow gain range still works", 0 <= passlog.suggest_gain([{"gain": 20, "score": 5}], 20.0, 0.0, 49.6) <= 49.6)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
