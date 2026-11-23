"""UK repeater list: parsing the RSGB CSV, filters, distance, download and cache."""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import repeaters as R          # noqa: E402

HEAD = '"CALL","BAND","CHAN","txMHz","rxMHz","CTCSS","QTHR","WHERE","lat","lon","ANALOG","DMR","DSTAR","FUSION",\n'
REAL = """2E0CJT,70CM,DVU12,439.150000,430.150000,,IO93,BARNSLEY,53.5,-1.4,,Y,,,
2E0CVL,70CM,DMU26,439.325000,430.325000,,IO80NX,WESTON SUPER MARE,51.349,-2.924,,Y,,,
2E0PEU,2M,RV46,145.575000,144.975000,103.5,IO83,STOKE-ON-TRENT,53.1,-2.2,Y,,,,
G1YFF,70CM,RB08,433.200000,434.800000,210.7,JO02,HAVERHILL,52.1,0.4,Y,,,,
G3YPP,2M,,144.562500,438.425000,94.8,JO02RV,MUNDESLEY,52.876,1.439,Y,,,,
G4EBD,70CM,RU67,430.837500,438.437500,71.9,IO90AR,POOLE,50.733,-1.96,Y,,,,
G4IPE,70CM,RU73,430.912500,438.512500,77.0,IO93PP,SCUNTHORPE,53.656,-0.69,Y,,,,
G4WTV,70CM,UR63,438.387500,430.787500,88.5,IO90ST,WORTHING,50.834,-0.422,Y,,,,
"""


def big_csv():
    lines = [HEAD, REAL]
    for i in range(300):                         # filler so the list is "big enough" to be believable
        lines.append(f"GB7{i:03d},2M,RV{i % 80:02d},{145.6 + (i % 20) * 0.0125:.6f},{145.0 + (i % 20) * 0.0125:.6f},"
                     f"{67.0 + i % 10},IO91,TOWN{i},{51 + (i % 30) / 10:.3f},{-1 - (i % 25) / 10:.3f},Y,{'Y' if i % 7 == 0 else ''},,,\n")
    lines.append("NOFREQ,2M,,,,,IO91,NOWHERE,51,-1,Y,,,,\n")           # skipped: no frequency
    lines.append("LOCONLY,2M,RV99,145.775000,145.175000,82.5,IO91WL,LOCTOWN,,,Y,,,,\n")   # position from the locator
    return "".join(lines)


def ok(msg):
    print("PASS", msg)


def check(cond, msg):
    if not cond:
        print("FAIL", msg)
        sys.exit(1)
    ok(msg)


rows = R.parse_csv(HEAD + REAL)
check(len(rows) == 8, "all 8 real sample rows parsed")
r = {x["call"]: x for x in rows}
check(r["G4EBD"]["out"] == 430.8375 and r["G4EBD"]["in"] == 438.4375, "txMHz is the listen (output) frequency")
check(r["2E0PEU"]["tone"] == 103.5 and r["2E0CJT"]["tone"] is None, "CTCSS tone read, blank means none")
check(r["2E0CJT"]["modes"] == ["DMR"] and r["2E0PEU"]["modes"] == ["FM"], "mode flags -> DMR / FM")
check(r["2E0CJT"]["where"] == "Barnsley", "town is title-cased")
check(parse_none := R.parse_csv("") == [], "empty file gives nothing")
check(len(R.parse_csv(REAL)) == 8, "a file without a header row is read in the usual order")
check(len(R.parse_csv("﻿" + HEAD + REAL)) == 8, "byte-order mark ignored")

ll = R.locator_to_latlon("IO91WL")
check(ll and abs(ll[0] - 51.52) < 0.05 and abs(ll[1] + 0.12) < 0.1, f"locator IO91WL is central London ({ll})")
check(R.locator_to_latlon("ZZ") is None and R.locator_to_latlon("") is None, "bad locator -> None")
km, brg = R.distance_bearing(51.5074, -0.1278, 53.4808, -2.2426)
check(250 < km < 270 and 300 < brg < 330, f"London -> Manchester about 260 km NW ({km:.0f} km, {brg:.0f}°)")
check(R.compass(0) == "N" and R.compass(90) == "E" and R.compass(315) == "NW" and R.compass(359) == "N", "compass points")

tmp = tempfile.mkdtemp()
events = []
text = big_csv()
rep = R.Repeaters({}, lambda k, p: events.append((k, p)), folder=tmp, opener=lambda url: io.BytesIO(text.encode()))
check(rep.status()["state"] == "missing", "no list yet -> missing")
check(rep.refresh(wait=True), "download ran")
st = rep.status()
check(st["state"] == "ready" and st["count"] >= 300, f"list ready ({st['count']} repeaters)")
check(any(k == "repeaters_status" for k, _ in events), "status was pushed")
check(os.path.exists(os.path.join(tmp, "repeaters.json")), "cached on disk")
loc = [x for x in rep.rows if x["call"] == "LOCONLY"]
check(loc and loc[0]["approx"] and loc[0]["lat"] is not None, "row with only a locator gets an approximate position")
check(not [x for x in rep.rows if x["call"] == "NOFREQ"], "row without a frequency skipped")

res, total = rep.query(text="poole")
check(total == 1 and res[0]["call"] == "G4EBD", "search by town")
res, total = rep.query(text="rv46")
check(total >= 1 and all("RV46" in x["chan"] for x in res), "search by channel")
res, total = rep.query(text="430.8375")
check(any(x["call"] == "G4EBD" for x in res), "search by frequency")
res, total = rep.query(mode="DMR")
check(total >= 3 and all("DMR" in x["modes"] for x in res), "mode filter")
res, total = rep.query(band="70CM", mode="FM")
check(total >= 3 and all(x["band"] == "70CM" for x in res), "band + mode filter")
res, total = rep.query(lat=50.9, lon=-1.4, max_km=60)
check(res and all(x["km"] <= 60 for x in res) and res[0]["km"] <= res[-1]["km"], "within 60 km of Southampton, nearest first")
check(any(x["call"] == "G4EBD" for x in res) and not any(x["call"] == "G3YPP" for x in res), "Poole in, Norfolk out")
res, total = rep.query(lat=50.9, lon=-1.4, limit=5)
check(len(res) == 5 and total > 5, "limit caps the rows, total says how many matched")
check("2M" in rep.bands() and "70CM" in rep.bands(), "bands listed")

rep2 = R.Repeaters({}, folder=tmp, opener=lambda url: (_ for _ in ()).throw(OSError("no internet")))
check(rep2.status()["state"] == "ready" and rep2.status()["count"] == st["count"], "list survives a restart (read from disk)")
rep2.refresh(wait=True)
check(rep2.status()["count"] == st["count"] and rep2.status()["state"] == "ready", "failed download keeps the old list")
check(not rep2.due(), "no retry straight after a failure")

tmp3 = tempfile.mkdtemp()
rep3 = R.Repeaters({}, folder=tmp3, opener=lambda url: io.BytesIO(b"<html>Error 500</html>"))
rep3.refresh(wait=True)
check(rep3.status()["state"] == "error" and rep3.status()["count"] == 0, "an HTML error page is not taken as a list")
clock = [1_700_000_000.0]
tmp4 = tempfile.mkdtemp()
rep4 = R.Repeaters({}, folder=tmp4, clock=lambda: clock[0], opener=lambda url: io.BytesIO(text.encode()))
check(rep4.due(), "missing list is due")
rep4.refresh(wait=True)
check(not rep4.due(), "fresh list not due")
clock[0] += 15 * 86400 + 7 * 3600
check(rep4.due(), "list older than 14 days is due again")
check(R.memory_name({"call": "GB3SN", "where": "Southampton Water"}) == "GB3SN Southampto", "memory name fits 16 characters")
print("ALL PASS")
