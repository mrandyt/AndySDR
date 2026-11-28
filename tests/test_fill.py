"""Picture fill: how much of a satellite picture's scan lines hold data, and how it reaches the pass report."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from PIL import Image
from andysdr import imgcheck, passlog

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1

d = tempfile.mkdtemp()
rng = np.random.default_rng(1)
def smooth(shape):                          # a picture-like image: neighbouring pixels look alike
    base = np.cumsum(rng.normal(0, 6, shape[:2]), axis=1)
    base = (base - base.min(axis=1, keepdims=True)) / (np.ptp(base, axis=1, keepdims=True) + 1e-9) * 200 + 20
    return np.repeat(base[:, :, None], 3, axis=2).astype(np.uint8)
a = smooth((1000, 300, 3))
a[:30] = 0; a[970:] = 0                      # the weak start and end of a pass: not counted
a[100:160] = 0; a[400:520] = 0               # 180 lines missed in the middle
Image.fromarray(a).save(os.path.join(d, "msu_mr_rgb_MSA.png"))
Image.fromarray(a).save(os.path.join(d, "msu_mr_rgb_MSA_map.png"))
Image.fromarray(np.zeros((1000, 300, 3), np.uint8)).save(os.path.join(d, "msu_mr_rgb_MCIR_corrected.png"))   # must be ignored
pct, lines = imgcheck.scanline_fill(os.path.join(d, "msu_mr_rgb_MSA.png"))
check("fill counts only the lines between the first and last with data", lines == 940 and abs(pct - 80.9) < 0.2, (pct, lines))
ff = imgcheck.folder_fill(d)
check("a folder's figure ignores map and corrected pictures", ff and abs(ff["pct"] - 80.9) < 0.2, ff)
b = smooth(a.shape)
Image.fromarray(b).save(os.path.join(d, "msu_mr_rgb_MSA.png"))
check("a complete picture is 100%", imgcheck.scanline_fill(os.path.join(d, "msu_mr_rgb_MSA.png"))[0] == 100.0)
n = a.copy(); n[200:700] = (rng.random(n[200:700].shape) * 255).astype(np.uint8)   # 500 lines of static
Image.fromarray(n).save(os.path.join(d, "msu_mr_rgb_NOISY.png"))
pn, ln = imgcheck.scanline_fill(os.path.join(d, "msu_mr_rgb_NOISY.png"))
check("static does not count as picture", abs(pn - 40.4) < 1.0 and ln == 940, (pn, ln))
check("an empty folder has no figure", imgcheck.folder_fill(tempfile.mkdtemp()) is None)
Image.fromarray(np.zeros((1000, 300, 3), np.uint8)).save(os.path.join(d, "msu_mr_rgb_EMPTY.png"))
check("an all-black picture is 0%, not an error", imgcheck.scanline_fill(os.path.join(d, "msu_mr_rgb_EMPTY.png"))[0] == 0.0)

pl = passlog.PassLog(os.path.join(tempfile.mkdtemp(), "p.jsonl"))
pl.add({"key": "k1", "sat": "meteor_m24", "name": "Meteor-M N2-4 (LRPT)", "start": 1, "end": 2, "action": "lrpt", "max_el": 60})
pl.set_images("k1", 19, 19, "", {"pct": 72.5, "lines": 800})
v = pl.view(1)[0]
check("the pass report carries the fill figure", v["fill"] == 72.5 and v["merged_fill"] is None, v)
pl.set_merged("k1", {"frames": 10, "gained": 3, "gained_pct": 30, "fill": {"pct": 88.0, "lines": 900}}, 19)
v = pl.view(1)[0]
check("and the figure after merging both receivers", v["fill"] == 72.5 and v["merged_fill"] == 88.0, v)
pl.add({"key": "k2", "sat": "x", "name": "x", "start": 3, "end": 4})
pl.set_images("k2", 0, 0, "", None)
check("a pass with no measurement shows none", pl.view(1)[0]["fill"] is None)
pl.add({"key": "k3", "sat": "x", "name": "x", "start": 5, "end": 6})
check("a note is saved on its pass", pl.set_user_note("k3", "  antenna   turned 20 deg east ") and pl.view(1)[0]["user_note"] == "antenna turned 20 deg east")
check("it survives a restart", passlog.PassLog(pl.path).view(1)[0]["user_note"] == "antenna turned 20 deg east")
check("a long note is cut to 200 characters", pl.set_user_note("k3", "x" * 500) and len(pl.view(1)[0]["user_note"]) == 200)
check("an empty note removes it", pl.set_user_note("k3", "") and pl.view(1)[0]["user_note"] is None)
check("a note for a pass that does not exist is refused", pl.set_user_note("nope", "hello") is False)
# ---- the antenna map
import math, time
sky = lambda ts: (40.0, 1500.0, (ts * 2.0) % 360)                    # elevation, range, azimuth
m = passlog.PassMeter(sky)
t = 1000.0
for i in range(60):
    m.add(t + i * 1.1, 6.0, 20)
sm = m.summary()
check("a pass leaves a track: azimuth, elevation, strength", "track" in sm and len(sm["track"]) >= 15 and all(len(p) == 3 for p in sm["track"]), sm.get("track", [])[:2])
m2 = passlog.PassMeter(lambda ts: (40.0, 1500.0))                    # the old two-value form still works
for i in range(30):
    m2.add(t + i * 1.1, 6.0, 20)
check("a look-up without azimuth gives no track and no error", "track" not in m2.summary() and m2.summary()["samples"] == 30)

pm = passlog.PassLog(os.path.join(tempfile.mkdtemp(), "m.jsonl"))
rng2 = np.random.default_rng(5)
for k in range(6):
    tr = []
    for az in range(0, 360, 6):
        strength = 8.0 - (6.0 if 270 <= az <= 330 else 0.0) + rng2.normal(0, 0.3)       # weak toward the north-west
        tr.append([az, 45, round(float(strength), 1)])
    pm.add({"key": f"p{k}", "sat": "x", "name": "x", "start": k, "end": k + 1, "track": tr})
pm.add({"key": "old", "sat": "x", "name": "x", "start": 0, "end": 1})        # a pass from before tracks were kept
am = pm.antenna_map(12)
check("the map uses only passes that have a track", am["passes"] == 6 and len(am["points"]) == 6 * 60, am["passes"])
check("it finds the weak side", am["sectors"]["NW"] < am["sectors"]["N"] - 4 and "weakest toward NW" in am["text"], am["text"])
check("it says when every side is the same", "even" in passlog.PassLog(os.path.join(tempfile.mkdtemp(), "e.jsonl")).__class__.antenna_map(type("L", (), {"rows": [{"track": [[a, 40, 5.0] for a in range(0, 360, 10)]}]})(), 5)["text"])
check("with nothing recorded it is empty, not an error", passlog.PassLog(os.path.join(tempfile.mkdtemp(), "z.jsonl")).antenna_map(5)["passes"] == 0)
# ---- setting poor pictures aside
import types, shutil
os.environ["APPDATA"] = os.environ["HOME"] = tempfile.mkdtemp()
from andysdr import decoders
out = os.path.join(tempfile.mkdtemp(), "images", "lrpt_20261127_141519")
os.makedirs(os.path.join(out, "MSU-MR"))
files = []
for nme in ("MSU-MR/a.png", "MSU-MR/b.png"):
    p = os.path.join(out, nme); open(p, "wb").write(b"png"); files.append((p, nme))
ns = types.SimpleNamespace(log=lambda *a, **k: None)
n = decoders.DecoderManager._set_aside(ns, files, out)
check("poor pictures are moved, not deleted", n == 2 and not any(os.path.exists(f) for f, _ in files))
dest = os.path.join(decoders.data_dir(), "images_poor", "lrpt_20261127_141519", "MSU-MR", "a.png")
check("they land outside the gallery folder", os.path.exists(dest) and "images_poor" in dest and os.sep + "images" + os.sep not in dest.replace("images_poor", ""))
pq = passlog.PassLog(os.path.join(tempfile.mkdtemp(), "q.jsonl"))
pq.add({"key": "q", "sat": "x", "name": "x", "start": 1, "end": 2, "action": "lrpt"})
pq.set_images("q", 0, 19, "", {"pct": 41.0, "poor": True})
check("the pass report says the pictures were set aside", pq.view(1)[0]["result"] == "poor (41.0% filled) – set aside", pq.view(1)[0]["result"])
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
