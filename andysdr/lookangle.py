"""Where to point a fixed antenna at a geostationary satellite.

A geostationary satellite sits over the equator at a fixed longitude, so its direction from you never changes
and needs only your position - no orbit data (TLE) download.  The maths is exact (WGS-84 observer, satellite on
the 42 164 km geostationary radius, east/north/up from the difference vector); nothing here is approximate
except the satellite's own station-keeping drift of about 0.1 deg.

Azimuth is measured from TRUE north.  A compass reads magnetic north; in the UK the two differ by only a degree
or two, which is well inside a patch antenna's beam.

`GEO_SATS` is a short list of well-known L-band and weather slots.  Operators move satellites between slots, so
it is a starting point, not a live fleet list - the calculator also takes any longitude you type in.
"""
import math

_A = 6378.137                       # WGS-84 equatorial radius, km
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)
GEO_RADIUS_KM = 42164.17            # centre of the Earth to a geostationary satellite

# id -> definition.  `lon` is degrees east (west is negative).
GEO_SATS = {
    "alphasat": {"name": "Alphasat (Inmarsat-4A F4) - Europe, Middle East, Africa", "lon": 24.9,
                 "band": "Inmarsat L-band (AERO, STD-C)", "note": "The Inmarsat satellite best placed for the UK."},
    "inm3f2": {"name": "Inmarsat-3 F2 - Atlantic East (AOR-E)", "lon": -15.5,
               "band": "Inmarsat L-band (AERO, STD-C)",
               "note": "Long-standing Atlantic slot; Inmarsat moves satellites, so check it is still used."},
    "inm_ior": {"name": "Indian Ocean region (about 64 E)", "lon": 64.0,
                "band": "Inmarsat L-band (AERO, STD-C)", "note": "Low in the sky from the UK; the exact slot has changed over time."},
    "inm_amer": {"name": "Inmarsat-4 F3 - Americas (98.4 W)", "lon": -98.4,
                 "band": "Inmarsat L-band", "note": "Over the Americas: below the horizon from Europe."},
    "meteosat": {"name": "Meteosat (0 deg)", "lon": 0.0,
                 "band": "L-band LRIT 1691 MHz (weak)", "note": "Needs a dish; Meteosat-12 has no direct broadcast."},
    "elektro3": {"name": "Elektro-L3 if it moves west (planned 14.5 W)", "lon": -14.5,
                 "band": "L-band LRIT/HRIT 1691 MHz",
                 "note": "Planned, no date (rtl-sdr.com, May 2026). Today it is at 76 E, below the UK horizon."},
    "elektro3_now": {"name": "Elektro-L3 today (76 E)", "lon": 76.0,
                     "band": "L-band LRIT/HRIT 1691 MHz", "note": "Below the UK horizon."},
    "goes19": {"name": "GOES-19 East (75.2 W)", "lon": -75.2,
               "band": "L-band HRIT 1694.1 MHz", "note": "Only a degree or two above the UK horizon: not realistic."},
}


def _ecef(lat, lon, alt_km):
    la, lo = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1.0 - _E2 * math.sin(la) ** 2)
    return ((n + alt_km) * math.cos(la) * math.cos(lo),
            (n + alt_km) * math.cos(la) * math.sin(lo),
            (n * (1.0 - _E2) + alt_km) * math.sin(la))


def compass(az):
    """16-point compass name for an azimuth in degrees."""
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((az % 360.0) / 22.5 + 0.5) % 16]


def geo_look(lat, lon, sat_lon, alt_m=0.0):
    """Look angles to a geostationary satellite at `sat_lon` degrees east.

    Returns {"az", "el", "range_km", "tilt", "visible", "compass"}:
      az    degrees clockwise from true north (0-360)
      el    degrees above the horizon (negative = below it)
      tilt  how far to lean a flat patch antenna back from lying flat so its face looks at the satellite
            (90 - el; only meaningful when the satellite is above the horizon)
    """
    lat, lon, sat_lon = float(lat), float(lon), float(sat_lon)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("position out of range")
    ox, oy, oz = _ecef(lat, lon, float(alt_m or 0.0) / 1000.0)
    sl = math.radians(sat_lon)
    dx, dy, dz = GEO_RADIUS_KM * math.cos(sl) - ox, GEO_RADIUS_KM * math.sin(sl) - oy, -oz
    la, lo = math.radians(lat), math.radians(lon)
    east = -math.sin(lo) * dx + math.cos(lo) * dy
    north = -math.sin(la) * math.cos(lo) * dx - math.sin(la) * math.sin(lo) * dy + math.cos(la) * dz
    up = math.cos(la) * math.cos(lo) * dx + math.cos(la) * math.sin(lo) * dy + math.sin(la) * dz
    az = math.degrees(math.atan2(east, north)) % 360.0
    el = math.degrees(math.atan2(up, math.hypot(east, north)))
    return {"az": round(az, 1), "el": round(el, 1), "range_km": round(math.sqrt(dx * dx + dy * dy + dz * dz)),
            "tilt": round(90.0 - el, 1), "visible": el > 0.0, "compass": compass(az)}


def geo_table(lat, lon, alt_m=0.0, extra_lon=None):
    """Look angles for every satellite in GEO_SATS plus an optional typed-in longitude (listed first)."""
    rows = []
    for sid, d in GEO_SATS.items():
        r = geo_look(lat, lon, d["lon"], alt_m)
        r.update(id=sid, name=d["name"], lon=d["lon"], band=d["band"], note=d["note"])
        rows.append(r)
    if extra_lon is not None:
        r = geo_look(lat, lon, extra_lon, alt_m)
        r.update(id="custom", name="Your longitude %s" % (("%.1f E" % extra_lon) if extra_lon >= 0 else ("%.1f W" % -extra_lon)),
                 lon=float(extra_lon), band="", note="")
        rows.append(r)
    rows.sort(key=lambda r: (r["id"] != "custom", not r["visible"]))      # yours first, then those you can see, list order kept
    return rows
