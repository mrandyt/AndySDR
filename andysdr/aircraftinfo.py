"""Aircraft registry lookup for the ADS-B tab: ICAO hex -> registration, type, operator/owner, photo.

Lookups use free public services (hexdb.io for the registry, planespotters.net for photos) from a
background thread, so the radio never waits.  Results are cached on disk
(%APPDATA%\\AndySDR\\aircraft_cache.json) so every aircraft is only looked up once.  Turn it off with
the "adsb_lookup" setting (the aircraft hex code is the only thing sent).
"""
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request

from .config import data_dir

log = logging.getLogger("andysdr.aircraftinfo")

REGISTRY_URL = "https://hexdb.io/api/v1/aircraft/%s"
ROUTE_URL = "https://hexdb.io/api/v1/route/icao/%s"
PHOTO_URL = "https://api.planespotters.net/pub/photos/hex/%s"
NEG_TTL = 6 * 3600                   # "not found" answers are retried after this many seconds
MAX_CACHE = 20000


def _http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "AndySDR (aircraft lookup)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class AircraftInfo:
    def __init__(self, settings, notify=None, fetch=None, path=None):
        self.s = settings
        self.notify = notify or (lambda: None)
        self.fetch = fetch or _http_json
        self.path = path or os.path.join(data_dir(), "aircraft_cache.json")
        self.lock = threading.Lock()
        self.cache = {}
        self.q = queue.Queue()
        self.pending = set()
        self.dirty = False
        self._saved = 0.0
        self._load()
        threading.Thread(target=self._worker, name="aircraftinfo", daemon=True).start()

    # ---------------------------------------------------------------- storage
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    def _save(self, force=False):
        if not self.dirty or (not force and time.time() - self._saved < 30):
            return
        with self.lock:
            if len(self.cache) > MAX_CACHE:
                for k in sorted(self.cache, key=lambda k: self.cache[k].get("t", 0))[: len(self.cache) - MAX_CACHE]:
                    del self.cache[k]
            data = json.dumps(self.cache)
            self.dirty = False
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
            self._saved = time.time()
        except OSError:
            log.exception("aircraft cache save")

    # ---------------------------------------------------------------- public
    def enabled(self):
        return bool(self.s.get("adsb_lookup", True))

    def peek(self, hexid):
        """Cached info (dict) or None; queues a lookup if nothing is known yet."""
        hexid = hexid.lower()
        e = self.cache.get(hexid)
        if e and (e.get("found") or time.time() - e.get("t", 0) < NEG_TTL):
            return e
        if self.enabled() and hexid not in self.pending:
            self.pending.add(hexid)
            self.q.put((hexid, False))
        return e

    def want_photo(self, hexid):
        hexid = hexid.lower()
        e = self.cache.get(hexid)
        if self.enabled() and not (e and e.get("photo_done")) and ("p" + hexid) not in self.pending:
            self.pending.add("p" + hexid)
            self.q.put((hexid, True))

    def public(self, hexid):
        """Fields the GUI shows, '' when unknown."""
        e = self.cache.get(hexid.lower()) or {}
        return {"reg": e.get("reg", ""), "type": e.get("type", ""), "tcode": e.get("tcode", ""),
                "owner": e.get("owner", ""), "maker": e.get("maker", ""), "photo": e.get("photo", ""),
                "photo_link": e.get("photo_link", ""), "photographer": e.get("photographer", "")}

    def route(self, callsign):
        """origin-destination for a callsign (cached in memory for an hour), or ''."""
        callsign = (callsign or "").strip().upper()
        if not callsign or not self.enabled():
            return ""
        key = "r:" + callsign
        e = self.cache.get(key)
        if e and time.time() - e.get("t", 0) < 3600:
            return e.get("route", "")
        if key not in self.pending:
            self.pending.add(key)
            self.q.put((callsign, "route"))
        return e.get("route", "") if e else ""

    def cached_route(self, callsign):
        e = self.cache.get("r:" + (callsign or "").strip().upper())
        return e.get("route", "") if e else ""

    def shutdown(self):
        self._save(True)

    # ---------------------------------------------------------------- worker
    def _worker(self):
        while True:
            try:
                item, kind = self.q.get(timeout=5)
            except queue.Empty:
                self._save()
                continue
            try:
                if kind == "route":
                    self._do_route(item)
                elif kind:
                    self._do_photo(item)
                else:
                    self._do_registry(item)
            except Exception as e:                       # offline, rate limited ...
                log.debug("lookup %s failed: %s", item, e)
                if kind is False:
                    with self.lock:
                        self.cache.setdefault(item, {"found": False, "t": time.time() - NEG_TTL + 300})
            finally:
                self.pending.discard(("p" + item) if kind is True else ("r:" + item) if kind == "route" else item)
            self._save()
            time.sleep(0.15)                             # be polite to the free services

    def _do_registry(self, hexid):
        try:
            d = self.fetch(REGISTRY_URL % hexid.upper())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                d = {}
            else:
                raise
        reg = str(d.get("Registration") or "").strip()
        e = {"found": bool(reg or d.get("Type")), "t": time.time(), "reg": reg,
             "type": str(d.get("Type") or "").strip(), "tcode": str(d.get("ICAOTypeCode") or "").strip(),
             "owner": str(d.get("RegisteredOwners") or "").strip(), "maker": str(d.get("Manufacturer") or "").strip()}
        with self.lock:
            old = self.cache.get(hexid, {})
            for k in ("photo", "photo_link", "photographer", "photo_done"):
                if k in old:
                    e[k] = old[k]
            self.cache[hexid] = e
            self.dirty = True
        self.notify()

    def _do_photo(self, hexid):
        d = self.fetch(PHOTO_URL % hexid.upper())
        ph = (d.get("photos") or [None])[0]
        with self.lock:
            e = self.cache.setdefault(hexid, {"found": False, "t": time.time()})
            e["photo_done"] = True
            if ph:
                th = ph.get("thumbnail_large") or ph.get("thumbnail") or {}
                e["photo"], e["photo_link"] = str(th.get("src", "")), str(ph.get("link", ""))
                e["photographer"] = str(ph.get("photographer", ""))
            self.dirty = True
        self.notify()

    def _do_route(self, callsign):
        try:
            d = self.fetch(ROUTE_URL % callsign)
            route = str(d.get("route") or "") if isinstance(d, dict) else ""
        except urllib.error.HTTPError:
            route = ""
        with self.lock:
            self.cache["r:" + callsign] = {"route": route, "t": time.time()}
            self.dirty = True
        self.notify()
