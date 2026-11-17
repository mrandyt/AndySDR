"""Aircraft registry lookup: mocked network, cache, negative cache, photo, route, Tracker merge."""
import os, sys, tempfile, time, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr.aircraftinfo import AircraftInfo
from andysdr import adsb

fails = 0
def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1

calls = []
def fake(url):
    calls.append(url)
    if "hexdb.io/api/v1/aircraft/406A5C" in url:
        return {"Registration": "G-EUUU", "Type": "Airbus A320-232", "ICAOTypeCode": "A320",
                "RegisteredOwners": "British Airways", "Manufacturer": "Airbus"}
    if "hexdb.io/api/v1/aircraft/" in url:
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)
    if "planespotters" in url:
        return {"photos": [{"thumbnail_large": {"src": "https://t.plnspttrs.net/x.jpg"}, "link": "https://www.planespotters.net/photo/1", "photographer": "A Spotter"}]}
    if "route" in url:
        return {"route": "EGLL-LFPG"}
    raise RuntimeError("unexpected " + url)

def wait(cond, t=3):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    return False

d = tempfile.mkdtemp()
path = os.path.join(d, "c.json")
settings = {"adsb_lookup": True}
inf = AircraftInfo(settings, fetch=fake, path=path)
check("first peek returns nothing", inf.peek("406a5c") is None)
check("registry arrives", wait(lambda: inf.public("406A5C")["reg"] == "G-EUUU"))
p = inf.public("406A5C")
check("type/owner", p["tcode"] == "A320" and p["owner"] == "British Airways" and p["type"].startswith("Airbus"))
n = len(calls); inf.peek("406A5C"); time.sleep(0.2)
check("cached, no second call", len(calls) == n)
inf.peek("ABCDEF")
check("unknown 404 stored as not found", wait(lambda: (inf.cache.get("abcdef") or {}).get("found") is False))
n = len(calls); inf.peek("ABCDEF"); time.sleep(0.2)
check("negative cache honoured", len(calls) == n)
inf.want_photo("406A5C")
check("photo arrives", wait(lambda: inf.public("406A5C")["photo"].endswith("x.jpg")) and inf.public("406A5C")["photographer"] == "A Spotter")
check("registry data kept after photo", inf.public("406A5C")["reg"] == "G-EUUU")
inf.route("BAW123")
check("route", wait(lambda: inf.cached_route("baw123") == "EGLL-LFPG"))
inf._save(True)
inf2 = AircraftInfo(settings, fetch=fake, path=path)
check("persisted to disk", inf2.public("406A5C")["reg"] == "G-EUUU")
off = AircraftInfo({"adsb_lookup": False}, fetch=lambda u: (_ for _ in ()).throw(AssertionError("network used")), path=os.path.join(d, "o.json"))
off.peek("123456"); off.want_photo("123456"); off.route("XYZ1"); time.sleep(0.3)
check("disabled makes no calls", not off.cache)
def boom(url): raise OSError("offline")
bad = AircraftInfo(settings, fetch=boom, path=os.path.join(d, "b.json"))
bad.peek("111111")
check("offline: retried within minutes not hours", wait(lambda: "111111" in bad.cache) and time.time() - bad.cache["111111"]["t"] > 3600 * 5)

# Tracker merge
tr = adsb.Tracker({}, lambda *a: None)
tr.info = inf
a = adsb.Aircraft(0x406A5C, time.time()); a.flight = "BAW123 "
tr.ac[0x406A5C] = a
snap = tr.snapshot(time.time())
check("snapshot carries reg/type/route", snap[0]["reg"] == "G-EUUU" and snap[0]["tcode"] == "A320" and snap[0]["route"] == "EGLL-LFPG")
check("csv has registration", "G-EUUU" in tr.export_csv(time.time()))
print("FAILED" if fails else "ALL OK", fails)


# --- airport names on the route, military flag
def fake2(url):
    if "airport/icao/EGLL" in url: return {"airport": "London Heathrow Airport"}
    if "airport/icao/LFPG" in url: return {"airport": "Paris Charles de Gaulle"}
    return fake(url)
inf2 = AircraftInfo({"adsb_lookup": True}, fetch=fake2, path=os.path.join(d, "c2.json"))
inf2.route("BAW123")
check("route airport names", wait(lambda: inf2.route_names("EGLL-LFPG") == "London Heathrow Airport → Paris Charles de Gaulle"))
check("military hex blocks", adsb.is_military(0x43C123) and adsb.is_military(0xAE1234) and not adsb.is_military(0x406A5C))

# --- adsbdb route details
def fake3(url):
    if "adsbdb.com/v0/callsign" in url:
        return {"response": {"flightroute": {"airline": {"name": "British Airways"},
                "origin": {"name": "London Heathrow", "municipality": "London", "country_name": "United Kingdom", "iata_code": "LHR", "icao_code": "EGLL", "latitude": 51.47, "longitude": -0.45},
                "destination": {"name": "Charles de Gaulle", "municipality": "Paris", "country_name": "France", "iata_code": "CDG", "icao_code": "LFPG", "latitude": 49.01, "longitude": 2.55}}}}
    if "adsbdb.com/v0/aircraft" in url:
        return {"response": {"aircraft": {"registered_owner_country_name": "United Kingdom", "registered_owner_operator_flag_code": "BAW"}}}
    return fake2(url)
inf3 = AircraftInfo({"adsb_lookup": True}, fetch=fake3, path=os.path.join(d, "c3.json"))
inf3.route("BAW123"); inf3.peek("406a5c")
check("route details (airline, cities, position)", wait(lambda: inf3.route_details("BAW123").get("dest", {}).get("city") == "Paris"
      and inf3.route_details("BAW123")["airline"] == "British Airways" and inf3.route_details("BAW123")["origin"]["lat"] == 51.47))
check("owner country from second registry", wait(lambda: inf3.public("406A5C")["owner_country"] == "United Kingdom"))

sys.exit(1 if fails else 0)
