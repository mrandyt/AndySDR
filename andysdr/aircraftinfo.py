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
CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/%s"          # airline, origin and destination with city, country, position
AIRPORT_URL = "https://hexdb.io/api/v1/airport/icao/%s"
AIRCRAFT2_URL = "https://api.adsbdb.com/v0/aircraft/%s"
PHOTO_URL = "https://api.planespotters.net/pub/photos/hex/%s"
PHOTO_REG_URL = "https://api.planespotters.net/pub/photos/reg/%s"
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
                "photo_link": e.get("photo_link", ""), "photographer": e.get("photographer", ""),
                "owner_country": e.get("owner_country", ""), "operator_code": e.get("operator_code", "")}

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

    def route_details(self, callsign):
        """{'airline','origin','dest'} from the cache (origin/dest: name, city, country, iata, icao, lat, lon) or {}."""
        e = self.cache.get("r:" + (callsign or "").strip().upper()) or {}
        return {k: e[k] for k in ("airline", "origin", "dest") if e.get(k)}

    def route_names(self, route):
        """'EGLL-KJFK' -> 'London Heathrow Airport → John F Kennedy International Airport' (what is cached so far)."""
        out = []
        for code in (route or "").split("-"):
            e = self.cache.get("ap:" + code.strip().upper())
            out.append((e or {}).get("name") or code.strip())
        return " → ".join(out) if route else ""

    # ---------------------------------------------------------------- one-off lookups (aircraft text details)
    # These run in a worker thread when you open a message, so they may wait for the internet.
    def hex_now(self, hexid):
        """Registry + photo for an ICAO hex, looked up now if not cached.  Returns public() fields."""
        hexid = (hexid or "").strip().lower()
        if not hexid or not self.enabled():
            return {}
        e = self.cache.get(hexid)
        if not (e and (e.get("found") or time.time() - e.get("t", 0) < NEG_TTL)):
            try:
                self._do_registry(hexid)
            except Exception as ex:                                        # noqa: BLE001 - offline
                log.debug("registry %s: %s", hexid, ex)
        if not (self.cache.get(hexid) or {}).get("photo_done"):
            try:
                self._do_photo(hexid)
            except Exception as ex:                                        # noqa: BLE001
                log.debug("photo %s: %s", hexid, ex)
        return dict(self.public(hexid), hex=hexid.upper())

    def reg_now(self, reg):
        """Aircraft details from a registration such as G-EZAV (ACARS gives the registration, not the hex)."""
        reg = (reg or "").strip().upper().lstrip(".")
        if not reg or not self.enabled():
            return {}
        key = "reg:" + reg
        e = self.cache.get(key)
        if e is None or (not e.get("hex") and time.time() - e.get("t", 0) > NEG_TTL):
            hexid = ""
            try:
                x = ((self.fetch(AIRCRAFT2_URL % reg) or {}).get("response") or {}).get("aircraft") or {}
                hexid = str(x.get("mode_s") or "").strip().lower() if isinstance(x, dict) else ""
            except Exception as ex:                                        # noqa: BLE001 - not found / offline
                log.debug("reg %s: %s", reg, ex)
            with self.lock:
                self.cache[key] = e = {"hex": hexid, "t": time.time()}
                self.dirty = True
        if e.get("hex"):
            return self.hex_now(e["hex"])
        out = {"reg": reg}                                                  # no hex: at least try a photo by registration
        try:
            ph = (self.fetch(PHOTO_REG_URL % reg).get("photos") or [None])[0]
            if ph:
                th = ph.get("thumbnail_large") or ph.get("thumbnail") or {}
                out.update(photo=str(th.get("src", "")), photo_link=str(ph.get("link", "")),
                           photographer=str(ph.get("photographer", "")))
        except Exception:                                                  # noqa: BLE001
            pass
        return out

    def route_now(self, callsign):
        """route_details() for a callsign, looked up now if not cached in the last hour."""
        callsign = (callsign or "").strip().upper()
        if not callsign or not self.enabled():
            return {}
        e = self.cache.get("r:" + callsign)
        if not (e and time.time() - e.get("t", 0) < 3600):
            try:
                self._do_route(callsign)
            except Exception as ex:                                        # noqa: BLE001
                log.debug("route %s: %s", callsign, ex)
        return dict(self.route_details(callsign), route=self.cached_route(callsign))

    def airport_now(self, code):
        """{'icao','name','country'} for a 4-letter airport code, or {} if it is not an airport."""
        code = (code or "").strip().upper()
        if len(code) != 4 or not code.isalpha():
            return {}
        key = "ap:" + code
        e = self.cache.get(key)
        if e is None and self.enabled():
            try:
                a = self.fetch(AIRPORT_URL % code)
                e = {"name": str(a.get("airport") or "").strip() if isinstance(a, dict) else "",
                     "country": str(a.get("country_code") or "").strip() if isinstance(a, dict) else "", "t": time.time()}
            except urllib.error.HTTPError as ex:
                e = {"name": "", "t": time.time()} if ex.code == 404 else None
            except Exception:                                              # noqa: BLE001 - offline: try again next time
                e = None
            if e is not None:
                with self.lock:
                    self.cache[key] = e
                    self.dirty = True
        if not e or not e.get("name"):
            return {}
        return {"icao": code, "name": e["name"], "country": e.get("country", "")}

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
        try:                                                              # second registry fills gaps and adds the owner's country
            x = ((self.fetch(AIRCRAFT2_URL % hexid.upper()) or {}).get("response") or {}).get("aircraft") or {}
            if isinstance(x, dict) and x:
                e["found"] = e["found"] or bool(x.get("registration") or x.get("type"))
                e["reg"] = e["reg"] or str(x.get("registration") or "")
                e["type"] = e["type"] or str(x.get("type") or "")
                e["tcode"] = e["tcode"] or str(x.get("icao_type") or "")
                e["owner"] = e["owner"] or str(x.get("registered_owner") or "")
                e["maker"] = e["maker"] or str(x.get("manufacturer") or "")
                e["owner_country"] = str(x.get("registered_owner_country_name") or "")
                e["operator_code"] = str(x.get("registered_owner_operator_flag_code") or "")
        except Exception:                                              # noqa: BLE001
            pass
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

    @staticmethod
    def _apt(a):
        a = a if isinstance(a, dict) else {}
        try:
            lat, lon = float(a.get("latitude")), float(a.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None
        return {"name": str(a.get("name") or ""), "city": str(a.get("municipality") or ""),
                "country": str(a.get("country_name") or ""), "iata": str(a.get("iata_code") or ""),
                "icao": str(a.get("icao_code") or ""), "lat": lat, "lon": lon}

    def _do_route(self, callsign):
        try:
            d = self.fetch(ROUTE_URL % callsign)
            route = str(d.get("route") or "") if isinstance(d, dict) else ""
        except urllib.error.HTTPError:
            route = ""
        rec = {"route": route, "t": time.time()}
        try:                                                              # richer second source (optional)
            fr = ((self.fetch(CALLSIGN_URL % callsign) or {}).get("response") or {}).get("flightroute") or {}
            if fr:
                o, de = self._apt(fr.get("origin")), self._apt(fr.get("destination"))
                al = fr.get("airline") if isinstance(fr.get("airline"), dict) else {}
                rec["airline"] = str(al.get("name") or "")
                rec["origin"], rec["dest"] = o, de
                if not route and o["icao"] and de["icao"]:
                    rec["route"] = route = o["icao"] + "-" + de["icao"]
        except Exception:                                                  # noqa: BLE001 - not found / offline
            pass
        with self.lock:
            self.cache["r:" + callsign] = rec
            self.dirty = True
        for code in route.split("-") if route else []:                      # airport names, cached for good
            key = "ap:" + code.upper()
            if key in self.cache:
                continue
            try:
                a = self.fetch(AIRPORT_URL % code.upper())
                name = str(a.get("airport") or "").strip() if isinstance(a, dict) else ""
            except Exception:                                              # noqa: BLE001 - unknown code / offline
                continue
            with self.lock:
                self.cache[key] = {"name": name, "t": time.time()}
                self.dirty = True
        self.notify()
