"""UK amateur repeater list (RSGB / ukrepeater.net).

The list is downloaded by the PC itself the first time the REPEATERS tab is opened, kept in
%APPDATA%\\AndySDR\\repeaters\\repeaters.json, and refreshed when it is more than 14 days old or when you press
"Update list".  If a download fails the old copy is kept.  The phone / tablet app shows the PC's web page, so it gets
the list through the PC and needs nothing of its own.

In the RSGB file  txMHz  is what the repeater TRANSMITS (the frequency you listen on) and  rxMHz  is the frequency it
listens on (the frequency a radio transmits on).  AndySDR only receives, so it tunes txMHz.
"""
import csv
import io
import json
import logging
import math
import os
import threading
import time
import urllib.request

from .config import VERSION, data_dir

log = logging.getLogger("andysdr.repeaters")

URLS = ("https://ukrepeater.net/csvcreate_all.php",
        "https://ukrepeater.net/csvcreate3.php")
STALE_DAYS = 14
RETRY_HOURS = 6
MIN_ROWS = 150           # a list smaller than this is a broken download and is thrown away
MODES = ("ANALOG", "DMR", "DSTAR", "FUSION")
MODE_LABEL = {"ANALOG": "FM", "DMR": "DMR", "DSTAR": "D-STAR", "FUSION": "C4FM"}


# ---------------------------------------------------------------------------- helpers
def locator_to_latlon(loc):
    """Maidenhead locator (4 or 6 characters) -> (lat, lon) of the middle of the square, or None."""
    s = str(loc or "").strip().upper()
    if len(s) < 4 or not (s[0].isalpha() and s[1].isalpha() and s[2].isdigit() and s[3].isdigit()):
        return None
    try:
        lon = (ord(s[0]) - 65) * 20 - 180 + int(s[2]) * 2
        lat = (ord(s[1]) - 65) * 10 - 90 + int(s[3])
        if len(s) >= 6 and s[4].isalpha() and s[5].isalpha():
            lon += (ord(s[4]) - 65) * (2 / 24) + (1 / 24)
            lat += (ord(s[5]) - 65) * (1 / 24) + (1 / 48)
        else:
            lon += 1
            lat += 0.5
    except (ValueError, IndexError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 4), round(lon, 4)


def distance_bearing(lat1, lon1, lat2, lon2):
    """(km, degrees from north) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return km, (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg):
    return ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")[
        int((deg % 360) / 22.5 + 0.5) % 16]


def _num(v):
    try:
        x = float(str(v).strip())
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def parse_csv(text):
    """RSGB CSV text -> list of dicts.  Column names are matched without regard to case; the trailing comma on every
    line is ignored; rows without a callsign or a usable frequency are skipped."""
    rdr = csv.reader(io.StringIO(text.lstrip("﻿")))
    head, out = None, []
    for cells in rdr:
        if not cells or not any(c.strip() for c in cells):
            continue
        if head is None:
            low = [c.strip().lower() for c in cells]
            if "call" in low or "txmhz" in low:
                head = low
                continue
            head = ["call", "band", "chan", "txmhz", "rxmhz", "ctcss", "qthr", "where", "lat", "lon",
                    "analog", "dmr", "dstar", "fusion"]
        row = {head[i]: cells[i].strip() for i in range(min(len(head), len(cells))) if head[i]}
        call = row.get("call", "").upper()
        out_mhz, in_mhz = _num(row.get("txmhz")), _num(row.get("rxmhz"))
        if not call or not out_mhz or out_mhz < 1 or out_mhz > 6000:
            continue
        lat, lon = _num(row.get("lat")), _num(row.get("lon"))
        approx = False
        if lat is None or lon is None or (lat == 0 and lon == 0):
            ll = locator_to_latlon(row.get("qthr"))
            lat, lon = ll if ll else (None, None)
            approx = ll is not None
        modes = [MODE_LABEL[k] for k in MODES if row.get(k.lower(), "").strip().upper().startswith(("Y", "1", "T"))]
        out.append({"call": call, "band": row.get("band", "").upper(), "chan": row.get("chan", "").upper(),
                    "out": round(out_mhz, 6), "in": round(in_mhz, 6) if in_mhz else None,
                    "tone": _num(row.get("ctcss")), "loc": row.get("qthr", "").upper(),
                    "where": row.get("where", "").title(),
                    "lat": lat, "lon": lon, "approx": approx, "modes": modes or ["FM"]})
    return out


def memory_name(r):
    """Up to 16 characters for a memory channel name."""
    return (r["call"] + " " + (r.get("where") or ""))[:16].strip()


# ---------------------------------------------------------------------------- the list
class Repeaters:
    def __init__(self, settings, emit=None, folder=None, clock=time.time, opener=None):
        self.s = settings
        self.emit = emit or (lambda *a: None)
        self.dir = folder or os.path.join(data_dir(), "repeaters")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "repeaters.json")
        self.clock = clock
        self._open = opener or self._default_open        # (url) -> binary file-like object
        self._lock = threading.Lock()
        self._busy = False
        self._error = ""
        self._last_try = 0.0
        self.rows, self.updated, self.source = [], 0.0, ""
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            rows = d.get("rows") or []
            if isinstance(rows, list) and len(rows) >= MIN_ROWS:
                self.rows, self.updated, self.source = rows, float(d.get("updated") or 0), str(d.get("source") or "")
        except (OSError, ValueError, TypeError):
            pass

    # -------------------------------------------------------------------- status
    def status(self):
        age = (self.clock() - self.updated) / 86400.0 if self.updated else None
        if self._busy:
            state = "downloading"
        elif self.rows:
            state = "ready"
        elif self._error:
            state = "error"
        else:
            state = "missing"
        return {"state": state, "count": len(self.rows), "updated": self.updated,
                "age_days": None if age is None else round(age, 1), "msg": self._error if state != "ready" else "",
                "source": self.source}

    def _push(self):
        try:
            self.emit("repeaters_status", self.status())
        except Exception:      # noqa: BLE001
            pass

    def due(self):
        if self._busy or self.clock() - self._last_try < RETRY_HOURS * 3600:
            return False
        st = self.status()
        return st["state"] in ("missing", "error") or (st["age_days"] is not None and st["age_days"] >= STALE_DAYS)

    # -------------------------------------------------------------------- query
    def query(self, text="", band="", mode="", max_km=0, lat=None, lon=None, limit=400):
        """Rows matching the filters, each with  km / brg / dir  when a position is given; nearest first with a
        position, else by callsign."""
        words = [w for w in str(text or "").upper().replace(",", " ").split() if w]
        band = str(band or "").upper()
        mode = str(mode or "")
        have_pos = lat is not None and lon is not None
        res = []
        for r in self.rows:
            if band and r.get("band") != band:
                continue
            if mode and mode not in r.get("modes", []):
                continue
            if words:
                hay = " ".join((r["call"], r.get("where", "").upper(), r.get("loc", ""), r.get("chan", ""),
                                f"{r['out']:.4f}", f"{r['out']:.3f}"))
                if not all(w in hay for w in words):
                    continue
            x = dict(r)
            if have_pos and r.get("lat") is not None and r.get("lon") is not None:
                km, brg = distance_bearing(lat, lon, r["lat"], r["lon"])
                if max_km and km > max_km:
                    continue
                x["km"], x["brg"], x["dir"] = round(km, 1), round(brg), compass(brg)
            elif have_pos and max_km:
                continue                    # no position for this one, so it cannot be "within N km"
            res.append(x)
        if have_pos:
            res.sort(key=lambda r: (r.get("km") is None, r.get("km") or 0, r["call"]))
        else:
            res.sort(key=lambda r: (r["call"], r["out"]))
        total = len(res)
        return res[:max(1, int(limit))], total

    def bands(self):
        return sorted({r["band"] for r in self.rows if r.get("band")},
                      key=lambda b: (len(b), b))

    # -------------------------------------------------------------------- downloading
    def _default_open(self, url):
        from .updater import _urlopen
        req = urllib.request.Request(url, headers={"User-Agent": f"AndySDR/{VERSION}"})
        return _urlopen(req, 60)

    def refresh(self, wait=False):
        """Download in the background (or here with wait=True).  False if one is already running."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._push()
        if wait:
            self._run()
        else:
            threading.Thread(target=self._run, daemon=True, name="repeaters").start()
        return True

    def _run(self):
        ok, err = False, ""
        try:
            ok, err = self._download()
        except Exception as e:      # noqa: BLE001
            err = str(e)
            log.warning("repeater list update failed: %s", e)
        finally:
            with self._lock:
                self._busy = False
                self._last_try = self.clock()
                self._error = "" if ok else (err or "download failed")
        self._push()
        if ok:
            self.emit("event", {"level": "info", "text": f"UK repeater list updated ({len(self.rows):,} repeaters)"})
        elif not self.rows:
            self.emit("event", {"level": "warn", "text": "Could not download the UK repeater list: " + (err or "unknown error")})

    def _download(self):
        last = "no address to try"
        for url in URLS:
            try:
                with self._open(url) as resp:
                    raw = resp.read(8 * 1024 * 1024)
                text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
                rows = parse_csv(text)
                if len(rows) < MIN_ROWS:
                    raise ValueError(f"the file has only {len(rows)} repeaters - not the repeater list")
                self._store(rows, url)
                log.info("repeater list: %d repeaters from %s", len(rows), url)
                return True, ""
            except Exception as e:      # noqa: BLE001
                try:
                    from .updater import _friendly
                    last = _friendly(e)
                except Exception:       # noqa: BLE001
                    last = str(e)
                log.info("repeater list from %s failed: %s", url, e)
        return False, last

    def _store(self, rows, url):
        tmp = self.path + ".tmp"
        now = self.clock()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": now, "source": url, "rows": rows}, f, separators=(",", ":"))
        os.replace(tmp, self.path)
        self.rows, self.updated, self.source = rows, now, url
