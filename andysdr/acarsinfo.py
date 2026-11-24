"""Everything AndySDR can tell you about one aircraft text (ACARS / VDL2) message.

Opened when you click a message in the AIRCRAFT TEXT tab.  Uses the same free lookups as the ADS-B tab
(see aircraftinfo.py): the aircraft (type, operator, photo), the flight's route, and the names of any airports
the message mentions.  Only the registration, flight number and airport codes are sent, never the message.
"""
import re

# What each label means, in plain English (the short names in acars.py are for the table).
LABEL_HELP = {
    "Q0": "Link test – the aircraft checking it can still reach the ground station. No text.",
    "_d": "Acknowledgement – 'message received'. No text.",
    "SQ": "Ground station announcing itself (squitter). Not from an aircraft.",
    "H1": "Message to or from the aircraft's computers (flight management system, printer or crew screen). "
          "Often flight plans, performance figures, fuel or engine data.",
    "5U": "Weather request – the crew asking for the weather at one or more airports.",
    "80": "Weather report for an airport, sent up to the aircraft.",
    "83": "Weather report.",
    "SA": "Media advisory – the aircraft reporting which radio links (VHF, VDL2, satellite) it can use.",
    "5Z": "Airline message – free text between the crew and the airline.",
    "10": "Departure or delay report.",
    "QA": "OUT – the aircraft has pushed back from the gate.", "QB": "OFF – the aircraft has taken off.",
    "QC": "ON – the aircraft has landed.", "QD": "IN – the aircraft has arrived at the gate.",
    "QE": "OUT/fuel report.", "QF": "OFF/destination report.", "QG": "OUT/return-to-gate report.",
    "QH": "OUT report.", "QK": "Landing report.", "QM": "Landing/arrival report.", "QN": "Diversion report.",
    "QP": "OUT report.", "QQ": "OFF report.", "QR": "ON report.", "QS": "IN report.", "QT": "OUT/return report.",
    "5Y": "Take-off report.",
    "H2": "Wind report.", "H3": "Wind report.",
    "B6": "Request to air traffic control (oceanic clearance or similar).", "A9": "Request to air traffic control.",
    "AA": "Answer from air traffic control.", "A0": "Air traffic control message.", "B9": "Air traffic control message.",
    "RA": "Clearance or message printed in the cockpit.", "C1": "Message to the cabin crew.",
    "57": "Fuel report.", "58": "Fuel report.", "MA": "Media message.", "MB": "Media message.",
    "8S": "Airline-defined message (often a flight log or delay information).",
    "5V": "Airline-defined message.", "4A": "Position and airport information (ATIS).",
    "44": "Position and weather report.", "4M": "Position report.",
}
for _l in ("12", "14", "15", "16", "21", "22", "23", "24", "30", "33"):
    LABEL_HELP.setdefault(_l, "Position report – where the aircraft is, its height and sometimes speed and weather.")
LABEL_HELP.setdefault("13", "Position and OUT/OFF/ON/IN times.")
LABEL_HELP.setdefault("20", "Position report (sometimes delayed).")

# Two-letter airline codes in ACARS flight numbers -> ICAO radio callsign prefix (for the route lookup).
AIRLINE_ICAO = {
    "U2": "EZY", "EC": "EJU", "DS": "EZS", "BA": "BAW", "FR": "RYR", "RK": "RUK", "LS": "EXS", "EI": "EIN",
    "LX": "SWR", "LH": "DLH", "AF": "AFR", "KL": "KLM", "IB": "IBE", "VS": "VIR", "BY": "TOM", "TOM": "TOM",
    "W6": "WZZ", "W9": "WUK", "VY": "VLG", "SK": "SAS", "AY": "FIN", "TP": "TAP", "OS": "AUA", "SN": "BEL",
    "AA": "AAL", "UA": "UAL", "DL": "DAL", "AC": "ACA", "EK": "UAE", "QR": "QTR", "EY": "ETD", "TK": "THY",
    "LO": "LOT", "AZ": "ITY", "BE": "BEE", "T3": "EZE", "LM": "LOG", "MT": "TCX", "ZT": "AWC", "FX": "FDX",
    "5X": "UPS", "BCS": "BCS", "QY": "BCS", "D8": "NSZ", "DY": "NOZ", "HV": "TRA", "PC": "PGT", "A3": "AEE",
    "SQ": "SIA", "CX": "CPA", "AI": "AIC", "PK": "PIA", "ET": "ETH", "MS": "MSR", "SV": "SVA", "GF": "GFA",
}

# A 4-letter word is only treated as an airport code if it starts like one (ICAO regions) and is then confirmed online.
_APT = re.compile(r"(?<![A-Z0-9])([EKLCBGHOPRSTUVWYZDFMN][A-Z]{3})(?![A-Z0-9])")
_NOT_APT = {"NONE", "DATA", "TEXT", "POSN", "TIME", "FUEL", "WIND", "TEMP", "EGTS", "DEST", "ROUT", "FLAP",
            "PUSH", "GATE", "LAND", "TAXI", "CLMB", "DESC", "OPEN", "FROM", "WITH", "THIS", "THAT", "FREE",
            "SEND", "LAST", "NEXT", "OVER", "PLAN", "LOAD", "FLTN", "LINE", "MAIN", "LEFT", "HOLD", "STBY"}


def flight_callsigns(flight):
    """'U2579U' -> ['EZY579U', 'U2579U']: the radio callsign the route databases know, then the flight as sent."""
    f = (flight or "").strip().upper()
    if len(f) < 3:
        return []
    out = []
    for n in (3, 2):
        al, num = f[:n], f[n:]
        if al in AIRLINE_ICAO and num:
            out.append(AIRLINE_ICAO[al] + (num.lstrip("0") or "0"))
            break
    if f not in out:
        out.append(f)
    return out


def airport_codes(text, limit=6):
    seen = []
    for c in _APT.findall((text or "").upper()):
        if c not in _NOT_APT and c not in seen:
            seen.append(c)
        if len(seen) >= limit:
            break
    return seen


def explain(m, info):
    """Details for one stored message `m` (a DatalinkStore record).  `info` is the AircraftInfo cache."""
    lab = m.get("label") or ""
    out = {"id": m.get("id"), "label": {"code": lab, "name": m.get("label_name") or "",
                                        "what": LABEL_HELP.get(lab, "")},
           "direction": "from the aircraft" if m.get("down") else "from the ground to the aircraft",
           "aircraft": {}, "flight": {}, "airports": [], "online": bool(info and info.enabled())}
    if not info or not info.enabled():
        return out
    try:
        if m.get("icao"):
            out["aircraft"] = info.hex_now(m["icao"])
        if not out["aircraft"].get("type") and m.get("reg"):
            out["aircraft"] = info.reg_now(m["reg"]) or out["aircraft"]
    except Exception:                                                      # noqa: BLE001 - offline
        pass
    for cs in flight_callsigns(m.get("flight")):
        try:
            r = info.route_now(cs)
        except Exception:                                                  # noqa: BLE001
            r = {}
        if r.get("origin") or r.get("route"):
            out["flight"] = dict(r, callsign=cs)
            break
    for code in airport_codes(m.get("text")):
        try:
            a = info.airport_now(code)
        except Exception:                                                  # noqa: BLE001
            a = {}
        if a:
            out["airports"].append(a)
    return out
