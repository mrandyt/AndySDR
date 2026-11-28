"""Read what is actually inside an ACARS / VDL2 message body.

An ACARS message is a label, an aircraft, and then a body in whatever format the airline's computer felt like
using.  A lot of it is private to the airline and will never be readable - an engine data download is numbers and
nothing else.  But a good share of what passes overhead carries a position, the waypoint it has just left and the
one it is heading for, and where it is flying from and to, all buried in a single run-on line.

This module pulls out only the parts that can be read with confidence:

* `position()` - latitude and longitude, in the five layouts that cover what is actually heard.
* `waypoints()` - navigation points and the times against them.
* `application()` - what the message is for, from the `#xx` sublabel or the FANS address at the front.

Anything it is not sure of is left alone rather than guessed at: a wrongly labelled field is worse than a raw one.
"""
import re

# ----------------------------------------------------------------------------- position

# Each entry is (name, pattern, how to turn the groups into degrees).  Tried in order; the first that gives a
# sensible position wins.
_POS = []


def _add(name, pat, fn):
    _POS.append((name, re.compile(pat), fn))


# "POSA1N53532E    42,ESHIN  ,..."  - degrees x 1000, the longitude right-justified in its field
_add("POSA1", r"POS[A-Z0-9]{0,2}([NS])\s*(\d{5})\s*([EW])\s*(\d{1,5})(?!\d)",
     lambda g: (int(g[1]) / 1000.0, int(g[3]) / 1000.0))

# "N 53.550 W  2.418"  /  "N 51.602,W   0.522"  - plain decimal degrees
_add("decimal", r"([NS])\s*(\d{1,3}\.\d{1,5})\s*[, ]\s*([EW])\s*(\d{1,3}\.\d{1,5})",
     lambda g: (float(g[1]), float(g[3])))

# "N5153.7W00208.7" - degrees and minutes
_add("deg-min", r"([NS])(\d{2})(\d{2}\.\d{1,2})\s*([EW])(\d{3})(\d{2}\.\d{1,2})",
     lambda g: (int(g[1]) + float(g[2]) / 60.0, int(g[4]) + float(g[5]) / 60.0))

# "N506129W0047904" - degrees with four decimal places, no point
_add("packed", r"([NS])(\d{2})(\d{4})([EW])(\d{3})(\d{4})",
     lambda g: (int(g[1]) + int(g[2]) / 10000.0, int(g[4]) + int(g[5]) / 10000.0))

# "1035+5342-133/" - signed hundredths, as Air France sends in label 2F
_add("signed", r"(?<!\d)\d{4}([+-]\d{4})([+-]\d{3,4})/",
     lambda g: (int(g[0]) / 100.0, int(g[1]) / 100.0))


def position(text, near=None, max_km=3000.0):
    """Latitude and longitude from a message body, or None.

    `near` is (lat, lon) of the receiver: a position further than `max_km` from it did not come from an aircraft
    this radio can hear, so it is a misread rather than a fix, and is thrown away.
    """
    t = (text or "").upper()
    for name, pat, fn in _POS:
        m = pat.search(t)
        if not m:
            continue
        g = list(m.groups())
        try:
            if name in ("POSA1", "decimal"):
                lat, lon = fn(g)
                lat = -lat if g[0] == "S" else lat
                lon = -lon if g[2] == "W" else lon
            elif name in ("deg-min", "packed"):
                lat, lon = fn(g)
                lat = -lat if g[0] == "S" else lat
                lon = -lon if g[3] == "W" else lon
            else:
                lat, lon = fn(g)
        except (ValueError, ZeroDivisionError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or (lat == 0.0 and lon == 0.0):
            continue
        if near and _km(near[0], near[1], lat, lon) > max_km:
            continue
        return {"lat": round(lat, 4), "lon": round(lon, 4), "how": name}
    return None


def _km(lat1, lon1, lat2, lon2):
    import math
    m = math.radians((lat1 + lat2) / 2.0)
    return math.hypot((lat2 - lat1) * 111.19, (lon2 - lon1) * 111.32 * math.cos(m))


# ----------------------------------------------------------------------------- what the message is for

# The `#xx` sublabel at the front says which program on the aircraft sent it.
_SUB = {
    "DF": "Flight-data download – engine and sensor readings for the airline's maintenance computer. Numbers only, "
          "not meant to be read.",
    "M1": "Air traffic services (FANS) – clearances, position contracts and controller messages.",
    "T1": "Air traffic services (FANS).",
    "CF": "Datalink set-up between the aircraft and the ground network.",
    "H1": "Message to or from the aircraft's own computers.",
}

# The FANS messages that arrive with a ground address at the front, e.g. "/PIKCLYA.CLA 1224 ... EGGX CLRNCE 724".
_FANS = {
    "AFN": "Datalink logon – the aircraft introducing itself to an air traffic centre.",
    "ADS": "Position contract – the aircraft agreeing to report its position automatically.",
    "CLA": "Oceanic clearance – the route, level and speed for crossing the Atlantic.",
    "CR1": "Air traffic datalink connection request.",
    "CC1": "Air traffic datalink connection confirmed.",
    "AT1": "Controller–pilot message (CPDLC).",
    "TI2": "Air traffic information.",
}
_FANS_RE = re.compile(r"^/([A-Z0-9]{3,8})\.([A-Z]{2,3}\d?)")
_SUB_RE = re.compile(r"^#([A-Z0-9]{2})")


def application(text):
    """One line saying what kind of message this is, or '' when nothing can be said."""
    t = (text or "").strip().upper()
    m = _SUB_RE.match(t)
    if m:
        return _SUB.get(m.group(1), f"Airline application {m.group(1)}.")
    m = _FANS_RE.match(t)
    if m:
        who, app = m.group(1), m.group(2)
        said = _FANS.get(app, f"Air traffic datalink message ({app}).")
        return f"{said} Ground station {who}."
    return ""


# ----------------------------------------------------------------------------- waypoints

# A navigation point is five letters (DIKAS, ERKIT), or three letters and a number (PEM-03, HAL-13), and in a
# position report it is usually followed by a six-figure time.
_WPT = re.compile(r"(?<![A-Z0-9-])([A-Z]{5}|[A-Z]{3}-\d{2})(?![A-Z0-9])\s*,?\s*(\d{6})?(?![\d.])")
_WPT_NOT = {"POSAB", "FIXED", "ROUTE", "LEVEL", "CLEAR", "TOTAL", "FUELS", "WINDS", "BEGIN", "FINAL", "OTHER",
            "PRINT", "RADIO", "DELAY", "DEPTH", "FLAPS", "ALTER", "NOTAM", "VALID", "INDEX", "START"}


# Where the flight is going: "LFPG/KLAX", or the two airport codes run together as "MYNNEGLL".
_ICAO = "EKLCBGHOPRSTUVWYZDFMN"          # the first letter of an ICAO airport code, by region
_RT_SLASH = re.compile(r"(?<![A-Z])([A-Z]{4})/([A-Z]{4})(?![A-Z])")
_RT_PAIR = re.compile(r"(?<![A-Z])([A-Z]{4})([A-Z]{4})(?![A-Z])")


def _strip_position(t):
    """The body with any latitude/longitude blanked out, so what is left reads cleanly."""
    for _name, pat, _fn in _POS:
        m = pat.search(t)
        if m:
            return t[:m.start()] + " " + t[m.end():]
    return t


def _icao_ok(c, not_apt):
    return len(c) == 4 and c[0] in _ICAO and c not in not_apt


def route(text, not_apt=frozenset()):
    """('MYNN', 'EGLL') - where the flight is from and to, when the body says so plainly."""
    t = _strip_position(_body(text))        # "MYNNEGLLN506129W0047904" hides the route behind the position
    for pat in (_RT_SLASH, _RT_PAIR):
        for a, b in pat.findall(t):
            if _icao_ok(a, not_apt) and _icao_ok(b, not_apt) and a != b:
                return a, b
    return None


def _body(text):
    """The message with its routing prefix removed, so "#DFB..." and "/PIKCLYA.CLA..." do not read as waypoints."""
    t = (text or "").upper()
    m = _SUB_RE.match(t.strip())
    if m:
        return t.strip()[len(m.group(0)):]
    m = _FANS_RE.match(t.strip())
    if m:
        return t.strip()[len(m.group(0)):]
    return t


def waypoints(text, limit=4):
    """[(name, 'HH:MM:SS' or '')] for the navigation points named in a message."""
    out, seen = [], set()
    for name, t in _WPT.findall(_body(text)):
        if name in _WPT_NOT or name in seen:
            continue
        seen.add(name)
        when = ""
        if t and t[:2] <= "23" and t[2:4] <= "59" and t[4:] <= "59":
            when = f"{t[:2]}:{t[2:4]}:{t[4:]}"
        out.append((name, when))
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------- putting it together

def decode(text, near=None, not_apt=frozenset()):
    """Everything that can be read out of one message body, as a dict (empty keys left out)."""
    out = {}
    what = application(text)
    if what:
        out["what"] = what
    p = position(text, near=near)
    if p:
        out["lat"], out["lon"], out["pos_how"] = p["lat"], p["lon"], p["how"]
    w = waypoints(text)
    if w:
        out["waypoints"] = [list(x) for x in w]
    r = route(text, not_apt)
    if r:
        out["from"], out["to"] = r
    return out


def lines(text):
    """The body split for reading: airline computers send one long run-on line, and '|' is a line break."""
    t = (text or "").replace("\r", "\n")
    parts = [p.strip() for chunk in t.split("\n") for p in chunk.split("|")]
    return [p for p in parts if p]
