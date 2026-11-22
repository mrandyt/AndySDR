"""Satellite pass prediction and automatic capture planning (ISS SSTV, Meteor-M LRPT, GOES HRIT).

* orbits come from CelesTrak TLEs (cached in %APPDATA%\\AndySDR\\tle_cache.json, refreshed every 12 h)
* SGP4/SDP4 (the `sgp4` package) gives the position; passes above your minimum elevation are found
  for the next 48 h and handed to the Scheduler, which tunes the radio and starts the decoder at
  acquisition of signal and stops it at loss of signal.
* geostationary satellites (GOES) never "pass": they are either above your horizon (captured in
  one-hour blocks, repeating) or they are not (and AndySDR says so instead of recording noise).
"""
import json
import logging
import math
import os
import threading
import time
import urllib.request

from .config import data_dir

log = logging.getLogger("andysdr.satpass")

try:                                   # pure python/C wheel; optional so the rest of the program still works
    from sgp4.api import Satrec, WGS72, jday
    HAVE_SGP4 = True
except Exception:                      # noqa: BLE001
    HAVE_SGP4 = False

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=%d&FORMAT=TLE"
TLE_MAX_AGE = 12 * 3600
LOOKAHEAD = 96 * 3600
GEO_BLOCK = 3600

# id -> definition.  `freqs` are the selectable downlinks (Hz); the first is the default.
SATELLITES = {
    "iss": {"name": "ISS SSTV (ARISS)", "norad": 25544, "kind": "leo", "action": "sstv", "mode": "NFM",
            "freqs": [145800000], "min_el": 10, "lead": 60, "tail": 30,
            "note": "ISS SSTV runs only during ARISS event windows (announced a few weeks ahead). Tick it on "
                    "and AndySDR listens on every pass; set the event dates below to limit it. Pictures that "
                    "decode appear in Weather images."},
    "meteor_m23": {"name": "Meteor-M N2-3 (LRPT)", "norad": 57166, "kind": "leo", "action": "lrpt", "mode": "DIG25",
                   "freqs": [137900000, 137100000], "min_el": 15, "lead": 30, "tail": 20,
                   "note": "72 kbit/s LRPT, usually 137.900 MHz. Needs SatDump (Help > Decoders)."},
    "meteor_m24": {"name": "Meteor-M N2-4 (LRPT)", "norad": 59051, "kind": "leo", "action": "lrpt", "mode": "DIG25",
                   "freqs": [137900000, 137100000], "min_el": 15, "lead": 30, "tail": 20,
                   "note": "Shares 137.900 / 137.100 MHz with N2-3; if both are up at once only one is decoded. "
                           "Switch the frequency here if its transmitter moves."},
    "metopb": {"name": "Metop-B (AHRPT)", "norad": 38771, "kind": "leo", "action": "metop", "mode": "DIG25",
               "freqs": [1701300000], "min_el": 10, "lead": 30, "tail": 20,
               "note": "1701.3 MHz, about 4.5 MHz wide: needs a tracking 1 m-class dish, an L-band LNA, the 6 or 10 MS/s "
                       "sample rate and SatDump. (The pass is followed for Doppler automatically.)"},
    "metopc": {"name": "Metop-C (AHRPT)", "norad": 43689, "kind": "leo", "action": "metop", "mode": "DIG25",
               "freqs": [1701300000], "min_el": 10, "lead": 30, "tail": 20,
               "note": "Same 1701.3 MHz channel and hardware as Metop-B; if both are overhead at once only one is decoded."},
    "goes19": {"name": "GOES-19 East (HRIT)", "norad": 60133, "kind": "geo", "action": "goes", "mode": "DIG25",
               "freqs": [1694100000], "min_el": 3, "lead": 0, "tail": 0,
               "note": "1694.1 MHz, 927 ksym/s BPSK. Needs a 1 m-class dish, an L-band LNA and a path to the "
                       "satellite (it is at 75.2 W). Needs SatDump."},
    "goes18": {"name": "GOES-18 West (HRIT)", "norad": 51850, "kind": "geo", "action": "goes", "mode": "DIG25",
               "freqs": [1694100000], "min_el": 3, "lead": 0, "tail": 0,
               "note": "1694.1 MHz. Same hardware as GOES-19; the satellite is at 137 W."},
}

_GOES_NOTE_GEO = "Not above your horizon from this location"


# --------------------------------------------------------------------------- geometry
def _gmst(jd):
    t = (jd - 2451545.0) / 36525.0
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t ** 3 / 38710000.0
    return math.radians(g % 360.0)


def look_angles(sat, lat, lon, alt_m, ts):
    """(elevation deg, azimuth deg, range km) of an sgp4 Satrec seen from the observer at unix time ts."""
    tm = time.gmtime(ts)
    jd, fr = jday(tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec + (ts % 1))
    e, r, _v = sat.sgp4(jd, fr)
    if e:
        return None
    th = _gmst(jd + fr)
    c, s = math.cos(th), math.sin(th)
    x, y, z = c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2]          # TEME -> ECEF (polar motion ignored)
    a, f = 6378.137, 1 / 298.257223563
    la, lo = math.radians(lat), math.radians(lon)
    e2 = f * (2 - f)
    n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    h = alt_m / 1000.0
    ox, oy, oz = (n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo), (n * (1 - e2) + h) * math.sin(la)
    dx, dy, dz = x - ox, y - oy, z - oz
    sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    south = sl * co * dx + sl * so * dy - cl * dz
    east = -so * dx + co * dy
    zen = cl * co * dx + cl * so * dy + sl * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    el = math.degrees(math.asin(max(-1, min(1, zen / rng))))
    az = math.degrees(math.atan2(east, -south)) % 360
    return el, az, rng


C_KMS = 299792.458


def doppler_freq(sat, lat, lon, alt_m, ts, freq):
    """Frequency (Hz) you must tune to hear a satellite that transmits on `freq` (range-rate Doppler)."""
    a = look_angles(sat, lat, lon, alt_m, ts - 0.5)
    b = look_angles(sat, lat, lon, alt_m, ts + 0.5)
    if not a or not b:
        return freq
    rr = b[2] - a[2]                                   # km/s, positive = moving away
    return int(round(freq * (1 - rr / C_KMS)))


def find_passes(sat, lat, lon, alt_m, start, hours=48, min_el=10.0, step=30):
    """Passes whose peak elevation >= min_el.  Returns [{'aos','los','max_el','tca','az_aos','az_los'}], times unix s.
    AOS/LOS are the moments the satellite crosses `min_el` (that's when the radio should be listening)."""
    out = []
    t = start
    end = start + hours * 3600
    prev = look_angles(sat, lat, lon, alt_m, t)
    cur = None
    while t < end:
        t2 = t + step
        la = look_angles(sat, lat, lon, alt_m, t2)
        if la is None or prev is None:
            prev, t = la, t2
            continue
        up0, up1 = prev[0] >= min_el, la[0] >= min_el
        if up1 and not up0:                                    # rising through min_el: refine
            lo_t, hi_t = t, t2
            for _ in range(8):
                mid = (lo_t + hi_t) / 2
                m = look_angles(sat, lat, lon, alt_m, mid)
                if m and m[0] >= min_el:
                    hi_t = mid
                else:
                    lo_t = mid
            cur = {"aos": hi_t, "max_el": la[0], "tca": t2, "az_aos": look_angles(sat, lat, lon, alt_m, hi_t)[1]}
        elif up0 and cur is None and t == start:               # already in a pass when we started
            cur = {"aos": start, "max_el": prev[0], "tca": start, "az_aos": prev[1]}
        if cur is not None:
            if la[0] > cur["max_el"]:
                cur["max_el"], cur["tca"] = la[0], t2
            if up0 and not up1:                                # setting through min_el
                lo_t, hi_t = t, t2
                for _ in range(8):
                    mid = (lo_t + hi_t) / 2
                    m = look_angles(sat, lat, lon, alt_m, mid)
                    if m and m[0] >= min_el:
                        lo_t = mid
                    else:
                        hi_t = mid
                cur["los"] = lo_t
                cur["az_los"] = look_angles(sat, lat, lon, alt_m, lo_t)[1]
                out.append(cur)
                cur = None
        prev, t = la, t2
    return out


def parse_tle(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i in range(len(lines) - 1):
        if lines[i].startswith("1 ") and lines[i + 1].startswith("2 "):
            return lines[i], lines[i + 1]
    return None


# --------------------------------------------------------------------------- planner
def _http_text(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "AndySDR (TLE)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


class SatPlanner:
    def __init__(self, settings, fetch=None, path=None, notify=None):
        self.s = settings
        self.fetch = fetch or _http_text
        self.path = path or os.path.join(data_dir(), "tle_cache.json")
        self.notify = notify or (lambda: None)
        self.lock = threading.RLock()
        self.tle = {}                 # norad(str) -> {"l1","l2","t"}
        self.cache = {}               # sat id -> (key, passes, computed_at)
        self.done = set()
        self.last_error = ""
        self._busy = False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.tle = json.load(f)
        except (OSError, ValueError):
            pass

    # ---- settings
    def cfg(self):
        c = self.s.get("sat_auto") or {}
        out = {}
        for sid, d in SATELLITES.items():
            u = c.get(sid) or {}
            freq = int(u.get("freq") or d["freqs"][0])
            out[sid] = {"enabled": bool(u.get("enabled", False)), "freq": freq,
                        "min_el": float(u.get("min_el") if u.get("min_el") not in (None, "") else d["min_el"]),
                        "track": bool(u.get("track", True)),
                        "from": str(u.get("from") or ""), "to": str(u.get("to") or ""),
                        "days": sorted({int(x) for x in (u.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})}
        return out

    def set_cfg(self, sid, **kw):
        if sid not in SATELLITES:
            return
        c = dict(self.s.get("sat_auto") or {})
        u = dict(c.get(sid) or {})
        for k in ("enabled", "freq", "min_el", "from", "to", "days", "track"):
            if k in kw:
                u[k] = kw[k]
        c[sid] = u
        self.s["sat_auto"] = c
        self.s.save()
        self.cache.pop(sid, None)

    def observer(self):
        """(lat, lon, alt_m) – its own setting, else the ADS-B position."""
        for a, b in (("obs_lat", "obs_lon"), ("adsb_lat", "adsb_lon")):
            try:
                la, lo = self.s.get(a), self.s.get(b)
                if la in (None, "") or lo in (None, ""):
                    continue
                la, lo = float(la), float(lo)
                if -90 <= la <= 90 and -180 <= lo <= 180:
                    return la, lo, float(self.s.get("obs_alt") or 30)
            except (TypeError, ValueError):
                pass
        return None

    def set_observer(self, lat, lon, alt=None):
        try:
            la, lo = float(lat), float(lon)
            if not (-90 <= la <= 90 and -180 <= lo <= 180):
                raise ValueError
        except (TypeError, ValueError):
            return False
        self.s["obs_lat"], self.s["obs_lon"] = round(la, 5), round(lo, 5)
        if alt not in (None, ""):
            try:
                self.s["obs_alt"] = float(alt)
            except ValueError:
                pass
        self.s.save()
        self.cache.clear()
        return True

    # ---- TLEs
    def tle_age(self, norad):
        e = self.tle.get(str(norad))
        return time.time() - e["t"] if e else None

    def refresh_tles(self, force=False, sync=False):
        """Download missing / old TLEs for the satellites the user switched on (in a thread unless sync)."""
        if self._busy:
            return
        cfg = self.cfg()
        want = [d["norad"] for sid, d in SATELLITES.items() if force or cfg[sid]["enabled"]]
        want = [n for n in want if force or self.tle_age(n) is None or self.tle_age(n) > TLE_MAX_AGE]
        if not want:
            return

        def run():
            self._busy = True
            try:
                for n in want:
                    try:
                        t = parse_tle(self.fetch(TLE_URL % n))
                        if t:
                            with self.lock:
                                self.tle[str(n)] = {"l1": t[0], "l2": t[1], "t": time.time()}
                            self.last_error = ""
                        else:
                            self.last_error = f"CelesTrak had no data for {n}"
                    except Exception as e:                       # noqa: BLE001 – offline etc.
                        self.last_error = f"TLE download failed: {e}"
                try:
                    with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(self.tle, f)
                    os.replace(self.path + ".tmp", self.path)
                except OSError:
                    pass
                self.cache.clear()
            finally:
                self._busy = False
                self.notify()
        if sync:
            run()
        else:
            threading.Thread(target=run, name="tle", daemon=True).start()

    def satrec(self, norad):
        e = self.tle.get(str(norad))
        if not e or not HAVE_SGP4:
            return None
        try:
            return Satrec.twoline2rv(e["l1"], e["l2"], WGS72)
        except Exception:                                        # noqa: BLE001
            return None

    # ---- passes
    def passes(self, sid, now=None):
        """Upcoming passes (next 4 days) for a satellite using the user's settings."""
        now = now or time.time()
        obs = self.observer()
        d = SATELLITES[sid]
        c = self.cfg()[sid]
        sat = self.satrec(d["norad"])
        if not obs or sat is None:
            return []
        key = (obs, c["min_el"], self.tle[str(d["norad"])]["t"], int(now // 600))
        hit = self.cache.get(sid)
        if hit and hit[0] == key:
            return hit[1]
        if d["kind"] == "geo":
            la = look_angles(sat, obs[0], obs[1], obs[2], now)
            ps = []
            if la and la[0] >= c["min_el"]:
                ps = [{"aos": now, "los": now + GEO_BLOCK, "max_el": la[0], "tca": now, "az_aos": la[1], "az_los": la[1], "geo": True}]
            self.cache[sid] = (key, ps)
            return ps
        ps = find_passes(sat, obs[0], obs[1], obs[2], now - 600, LOOKAHEAD // 3600, c["min_el"])
        ps = [p for p in ps if p["los"] > now]
        self.cache[sid] = (key, ps)
        return ps

    def tuned_freq(self, sid, now=None):
        """Doppler-corrected frequency for a satellite right now (nominal frequency if no orbit/position)."""
        c = self.cfg()[sid]
        d = SATELLITES[sid]
        obs, sat = self.observer(), self.satrec(d["norad"])
        if d["kind"] == "geo" or not c["track"] or not obs or sat is None:
            return c["freq"]
        return doppler_freq(sat, obs[0], obs[1], obs[2], now or time.time(), c["freq"])

    def geo_visible(self, sid, now=None):
        obs, sat = self.observer(), self.satrec(SATELLITES[sid]["norad"])
        if not obs or sat is None:
            return None
        return look_angles(sat, obs[0], obs[1], obs[2], now or time.time())

    def in_window(self, sid, now):
        c = self.cfg()[sid]
        if c["days"] and time.localtime(now).tm_wday not in c["days"]:      # Mon=0 .. Sun=6; none ticked = every day
            return False
        if not (c["from"] or c["to"]):
            return True
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        return (not c["from"] or day >= c["from"]) and (not c["to"] or day <= c["to"])

    def due(self, now=None):
        """The enabled pass we should be recording right now: (sid, pass, job dict) or None."""
        now = now or time.time()
        best = None
        for sid, c in self.cfg().items():
            if not c["enabled"] or not self.in_window(sid, now):
                continue
            d = SATELLITES[sid]
            for p in self.passes(sid, now):
                if p["aos"] - d["lead"] <= now < p["los"] + d["tail"]:
                    key = f"sat:{sid}:{int(p['aos'] // 60)}" if not p.get("geo") else f"sat:{sid}:{int(now // GEO_BLOCK)}"
                    if key in self.done:
                        continue
                    if best is None or p["max_el"] > best[1]["max_el"]:
                        best = (sid, p, key)
        if not best:
            return None
        sid, p, key = best
        d, c = SATELLITES[best[0]], self.cfg()[best[0]]
        until = p["los"] + d["tail"]
        job = {"id": key, "name": f"{d['name']} pass (max {p['max_el']:.0f}°)" if not p.get("geo") else d["name"],
               "freq": self.tuned_freq(sid, now), "nominal": c["freq"], "sat": sid, "mode": d["mode"], "action": d["action"],
               "duration": max(1, int((until - now) / 60) + 1)}
        if not p.get("geo") and "az_aos" in p and "az_los" in p:
            # heading north (ends further north than it began): Meteor pictures come out upside down
            job["northbound"] = math.cos(math.radians(p["az_los"])) > math.cos(math.radians(p["az_aos"]))
        return sid, p, key, job, until

    def upcoming(self, now=None, limit=60):
        now = now or time.time()
        rows = []
        for sid, c in self.cfg().items():
            if not c["enabled"]:
                continue
            for p in self.passes(sid, now):
                if self.in_window(sid, p["aos"]):
                    rows.append({"sat": sid, "name": SATELLITES[sid]["name"], "aos": p["aos"], "los": p["los"],
                                 "max_el": round(p["max_el"]), "az": round(p["az_aos"]), "geo": bool(p.get("geo")),
                                 "freq": c["freq"], "track": c["track"] and not p.get("geo"),
                                 "state": "recording" if (p["aos"] - SATELLITES[sid]["lead"] <= now < p["los"] + SATELLITES[sid]["tail"]) else "waiting"})
        rows.sort(key=lambda r: r["aos"])
        return rows[:limit]

    def status(self, now=None):
        now = now or time.time()
        obs = self.observer()
        cfg = self.cfg()
        sats = []
        for sid, d in SATELLITES.items():
            age = self.tle_age(d["norad"])
            row = {"id": sid, "name": d["name"], "kind": d["kind"], "action": d["action"], "freqs": d["freqs"],
                   "note": d["note"], "tle_age_h": None if age is None else round(age / 3600, 1), **cfg[sid]}
            if d["kind"] == "geo" and obs and self.satrec(d["norad"]):
                la = self.geo_visible(sid, now)
                row["el"] = None if la is None else round(la[0], 1)
                row["visible"] = bool(la and la[0] >= cfg[sid]["min_el"])
                row["above_horizon"] = bool(la and la[0] > 0)
            sats.append(row)
        return {"have_sgp4": HAVE_SGP4, "observer": obs, "sats": sats, "passes": self.upcoming(now) if obs else [],
                "error": self.last_error, "busy": self._busy}
