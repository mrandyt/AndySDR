"""Satellite pass prediction and the auto-capture planner (offline, synthetic and real TLEs)."""
import math, os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import satpass as sp

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1

def tle_fix(l):
    l = l[:68]
    c = sum(int(ch) if ch.isdigit() else (1 if ch == "-" else 0) for ch in l)
    return l + str(c % 10)

check("sgp4 available", sp.HAVE_SGP4)
L1 = tle_fix("1 59051U 24039A   26167.62069696  .00000001  00000-0  19950-4 0  999")
L2 = tle_fix("2 59051  98.7010 126.9258 0007253 162.5453 197.5976 14.22429458119188")
sat = sp.Satrec.twoline2rv(L1, L2, sp.WGS72)
epoch = (sat.jdsatepoch - 2440587.5) * 86400 + sat.jdsatepochF * 86400
obs = (51.5, -0.12, 30)
ps = sp.find_passes(sat, *obs, epoch + 3600, hours=48, min_el=15)
check("Meteor passes found over 48 h", 4 <= len(ps) <= 16, len(ps))
check("passes sane", all(0 < p["los"] - p["aos"] < 1100 and p["max_el"] >= 15 and p["aos"] < p["tca"] < p["los"] for p in ps))
# a polar orbiter at 14.2 rev/day gives ~28 passes/48h above the horizon; min_el 0 should give many more than 15 deg
ps0 = sp.find_passes(sat, *obs, epoch + 3600, hours=48, min_el=0)
check("lower mask gives more passes", len(ps0) > len(ps), (len(ps0), len(ps)))
# look angle at TCA of a pass is near its max
p = ps[0]
la = sp.look_angles(sat, *obs, p["tca"])
check("elevation at TCA matches peak", abs(la[0] - p["max_el"]) < 0.01)
el_aos = sp.look_angles(sat, *obs, p["aos"])[0]
check("AOS time is on the mask", abs(el_aos - 15) < 0.5, el_aos)

# geostationary: build a synthetic GEO TLE and find where it sits
g1 = tle_fix("1 60133U 24119A   26167.50000000  .00000010  00000-0  00000-0 0  999")
g2 = tle_fix("2 60133   0.0300 100.0000 0001000  90.0000 100.0000  1.00271000  1000")
gs = sp.Satrec.twoline2rv(g1, g2, sp.WGS72)
gepoch = (gs.jdsatepoch - 2440587.5) * 86400 + gs.jdsatepochF * 86400
jd, fr = gs.jdsatepoch, gs.jdsatepochF
e, r, v = gs.sgp4(jd, fr)
lon = math.degrees(math.atan2(r[1], r[0]) - sp._gmst(jd + fr))
lon = (lon + 180) % 360 - 180
check("synthetic GEO is near the equator and 42164 km out", e == 0 and abs(math.hypot(*r) - 42164) < 300)
el_under = sp.look_angles(gs, 0.0, lon, 0, gepoch)[0]
check("observer under the GEO sat sees ~90 deg", el_under > 85, el_under)
check("far side of the world sees it below the horizon", sp.look_angles(gs, 51.5, lon + 150, 0, gepoch)[0] < 0)
# a UK observer 75 W away: at the horizon edge at best
uk = sp.look_angles(gs, 51.5, lon + 74.0, 0, gepoch)
check("UK-to-75W style geometry is at/below the horizon", uk[0] < 3, uk[0])

# planner
class S(dict):
    def save(self): pass
d = tempfile.mkdtemp()
s = S(obs_lat=51.5, obs_lon=-0.12)
calls = []
def fake(url):
    calls.append(url)
    n = int(url.split("CATNR=")[1].split("&")[0])
    if n == 59051: return f"METEOR-M2 4\n{L1}\n{L2}\n"
    if n == 60133: return f"GOES 19\n{g1}\n{g2}\n"
    return "No GP data found"
pl = sp.SatPlanner(s, fetch=fake, path=os.path.join(d, "t.json"))
check("nothing on by default", not any(c["enabled"] for c in pl.cfg().values()))
pl.set_cfg("meteor_m24", enabled=True)
pl.refresh_tles(sync=True)
check("only enabled satellites fetched", len(calls) == 1 and "59051" in calls[0], calls)
check("TLE stored on disk", os.path.getsize(os.path.join(d, "t.json")) > 100)
pl2 = sp.SatPlanner(s, fetch=fake, path=os.path.join(d, "t.json"))
check("TLE cache reloaded", pl2.tle_age(59051) is not None and pl2.tle_age(59051) < 60)
now = epoch + 3600
up = pl.upcoming(now)
check("upcoming list populated, sorted", len(up) >= 4 and all(up[i]["aos"] <= up[i + 1]["aos"] for i in range(len(up) - 1)))
first = pl.passes("meteor_m24", now)[0]
check("not due before lead time", pl.due(first["aos"] - 600) is None)
dv = pl.due(first["aos"] + 5)
check("due during the pass", dv is not None and dv[0] == "meteor_m24")
sid, p, key, job, until = dv
check("job: LRPT 137.9 MHz DIG25", job["action"] == "lrpt" and job["nominal"] == 137900000 and abs(job["freq"] - 137900000) < 4000 and job["mode"] == "DIG25")
check("job ends shortly after LOS", abs(until - (first["los"] + 20)) < 1 and job["duration"] >= 1)
pl.done.add(key)
check("same pass not started twice", pl.due(first["aos"] + 10) is None)
pl.set_cfg("meteor_m24", freq=137100000)
check("frequency switch honoured", pl.due(pl.passes("meteor_m24", now)[1]["aos"] + 5)[3]["nominal"] == 137100000)
pl.set_cfg("meteor_m24", **{"from": "2099-01-01"})
check("event window blocks capture", pl.due(first["aos"] + 5) is None)
pl.set_cfg("meteor_m24", **{"from": "", "min_el": 89.9})
check("high mask removes passes", pl.passes("meteor_m24", now) == [])
# geo: UK can't see it, visible observer gets hourly blocks
pl.set_cfg("goes19", enabled=True)
pl.refresh_tles(sync=True)
st = pl.status(gepoch)
g = next(x for x in st["sats"] if x["id"] == "goes19")
check("GOES from UK reported not visible", g["visible"] is False)
check("no GOES job from UK", pl.due(gepoch) is None)
s["obs_lat"], s["obs_lon"] = 10.0, lon
pl.cache.clear()
dv = pl.due(gepoch)
check("GOES job when visible", dv is not None and dv[3]["action"] == "goes" and dv[3]["freq"] == 1694100000, dv)
check("GOES block is about an hour", dv and 55 <= dv[3]["duration"] <= 62, dv and dv[3]["duration"])
check("observer falls back to ADS-B position", sp.SatPlanner({"adsb_lat": 52.0, "adsb_lon": -1.0}, fetch=fake, path=os.path.join(d, "x.json")).observer()[:2] == (52.0, -1.0))
check("no position -> no passes", sp.SatPlanner({}, fetch=fake, path=os.path.join(d, "y.json")).upcoming(now) == [])
# --- Doppler tracking: shift goes + -> - through the pass, within physical limits
s["obs_lat"], s["obs_lon"] = 52.0, lon
pl.cache.clear()
pl.set_cfg("meteor_m24", enabled=True, min_el=15, track=True, days=[], freq=137900000)
_p = pl.passes("meteor_m24", now)[0]
_f0 = pl.tuned_freq("meteor_m24", _p["aos"] + 5)
_f1 = pl.tuned_freq("meteor_m24", _p["tca"])
_f2 = pl.tuned_freq("meteor_m24", _p["los"] - 5)
check("Doppler: higher on approach, lower on recession", _f0 > 137900000 > _f2 and _f0 > _f1 > _f2, (_f0, _f1, _f2))
check("Doppler within +-4 kHz at 137.9 MHz", abs(_f0 - 137900000) < 4000 and abs(_f2 - 137900000) < 4000, (_f0, _f2))
pl.set_cfg("meteor_m24", track=False)
check("track off -> nominal frequency", pl.tuned_freq("meteor_m24", _p["aos"] + 5) == 137900000)
pl.set_cfg("meteor_m24", track=True)
_job = pl.due(_p["aos"] + 5)
check("job starts on Doppler-corrected freq", _job and _job[3]["freq"] != 137900000 and _job[3]["nominal"] == 137900000, _job and _job[3])
_up = pl.upcoming(now)
check("upcoming lists passes up to 4 days with state", len(_up) > 4 and all(r["state"] in ("waiting", "recording") for r in _up))
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)

# --- weekday filter: only the ticked days are captured
import time as _t
_mon = _t.mktime((2030, 1, 7, 12, 0, 0, 0, 0, -1))   # a Monday
pl.set_cfg("meteor_m24", days=[2])                      # Wednesdays only
assert not pl.in_window("meteor_m24", _mon) and pl.in_window("meteor_m24", _mon + 2 * 86400)
pl.set_cfg("meteor_m24", days=[])
assert pl.in_window("meteor_m24", _mon)
