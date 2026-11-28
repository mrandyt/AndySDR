"""Reading the inside of an ACARS message body: position, route, waypoints and what the message is for.

Every message below was really received on 131.725 / 131.825 MHz on 27 Nov 2026 and taken from the day's CSV.
The positions are checked against where the aircraft could actually have been.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import acarsdecode as A
from andysdr.acarsinfo import _NOT_APT

HOME = (52.668, -2.472)          # the receiver, in Shropshire
fails = 0


def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1


def near(got, lat, lon, tol=0.002):
    return got and abs(got["lat"] - lat) < tol and abs(got["lon"] - lon) < tol


# --- the five layouts that are actually heard -------------------------------------------------
cases = [
    ("American, label 16",
     "POSA1N53532E    42,ESHIN  ,111358,339,ERKIT  ,112052,,-54, 58,1510,833", 53.532, 0.042),
    ("American, west of Greenwich",
     "POSA1N55611W  5123,ERKIT  ,112051,339,GOMUP  ,120916,,-55, 81,1427,832", 55.611, -5.123),
    ("Air France, label 2F",
     "1035+5342-133/30905 | LFPG/KLAX/30000/ -34/ -45/0.83/310534/220703/26.0/LEHOC  /HAL-13 /-105/   51",
     53.42, -1.33),
    ("Air France, east of Greenwich",
     "1115+5164+035/30822 | LFPG/KATL/29997/ -39/ -44/0.83/315941/234003/23.0/POT-08 /BPK-07 / -76/", 51.64, 0.35),
    ("plain decimal, label 16",
     "PC1157/27NOV26 10:13:36,32006,1050,  99,N 53.550 W  2.418", 53.550, -2.418),
    ("decimal with commas",
     "LAM30  ,N 51.602,W   0.522,6930,0243,1331,028\\TS132350,271126", 51.602, -0.522),
    ("degrees and minutes, label QQ",
     "EGBJLEPA1155 | 001FE27115554N5153.7W00208.70111450110", 51.895, -2.145),
    ("British Airways, label 15",
     "FST01MYNNEGLLN506129W0047904410  68 437 M63C 6125408808752111600010441008", 50.6129, -4.7904),
]
for name, text, lat, lon in cases:
    check("position – " + name, near(A.position(text, near=HOME), lat, lon), A.position(text, near=HOME))

# the Gloucestershire one should land on Gloucestershire airport (51.894 N, 2.167 W), 25 km from home
p = A.position("EGBJLEPA1155 | 001FE27115554N5153.7W00208.70111450110", near=HOME)
check("a local take-off report lands on the right airfield", abs(p["lat"] - 51.894) < 0.01 and abs(p["lon"] + 2.167) < 0.03, p)

# two reports from the same flight 30 minutes apart must be a sensible distance apart for a jet
a = A.position("POSA1N53532E    42,ESHIN  ,111358,339,ERKIT  ,112052,,-54, 58,1510,833", near=HOME)
b = A.position("POSA1N55611W  5123,ERKIT  ,112051,339,GOMUP  ,120916,,-55, 81,1427,832", near=HOME)
km = A._km(a["lat"], a["lon"], b["lat"], b["lon"])
check("two fixes 30 min apart give a jet's ground speed", 300 < km < 500, round(km))

# --- nothing invented ---------------------------------------------------------------------------
for name, text in [("a masked position", "101339,,1122, 105,N   .    MMMM.MMM"),
                   ("an engine data download", "#DFB76 815214732668 17 16 |  897 897 8971072 824014942668 22 19"),
                   ("an acknowledgement", ""),
                   ("a link test", "0EV101433V"),
                   ("plain words", "ATM DISPATCH MSG | . | FREETEXT ACCEPTED | .")]:
    check("no position from " + name, A.position(text, near=HOME) is None, A.position(text, near=HOME))

check("a position on the other side of the world is thrown away",
      A.position("N 12.000 E 120.000", near=HOME) is None)
check("...but is read when no receiver position is given",
      A.position("N 12.000 E 120.000") is not None)

# --- where the flight is going ------------------------------------------------------------------
for name, text, want in [
        ("codes run together", "FST01MYNNEGLLN506129W0047904410  68 437", ("MYNN", "EGLL")),
        ("codes with a slash", "1035+5342-133/30905 | LFPG/KLAX/30000/ -34", ("LFPG", "KLAX")),
        ("a KLM progress report", "RST17A27102900KLM706  SBGLEHAM", ("SBGL", "EHAM")),
        ("a take-off report", "EGBJLEPA1155 | 001FE27115554N5153.7W00208.7", ("EGBJ", "LEPA"))]:
    check("route – " + name, A.route(text, _NOT_APT) == want, A.route(text, _NOT_APT))
check("plain words are not a route", A.route("ATM DISPATCH MSG | FREETEXT ACCEPTED", _NOT_APT) is None,
      A.route("ATM DISPATCH MSG | FREETEXT ACCEPTED", _NOT_APT))

# --- waypoints -----------------------------------------------------------------------------------
w = A.waypoints("POSA1N53532E    42,ESHIN  ,111358,339,ERKIT  ,112052,,-54, 58,1510,833")
check("waypoints and their times", w[:2] == [("ESHIN", "11:13:58"), ("ERKIT", "11:20:52")], w)
w = A.waypoints("1035+5342-133/30905 | LFPG/KLAX/30000/ -34/ -45/0.83/310534/220703/26.0/LEHOC  /HAL-13 /-105/")
check("waypoints without times", [n for n, _ in w][:2] == ["LEHOC", "HAL-13"], w)
check("an airport pair is not a waypoint", "MYNNE" not in [n for n, _ in A.waypoints("FST01MYNNEGLLN506129W0047904")])
check("a sublabel is not a waypoint", "DFBOP" not in [n for n, _ in A.waypoints("#DFBOP0051022018 851630250400051")])
check("a ground address is not a waypoint",
      "SOUCA" not in [n for n, _ in A.waypoints("/SOUCAYA.AT1.G-VIIK6086A233030F33")])

# --- what the message is for -----------------------------------------------------------------------
check("engine download is named", "Flight-data" in A.application("#DFBACM01ACM220DSG-VIIV  BAW252N"))
check("FANS application is named", "Air traffic services" in A.application("#M1BPOSN51234"))
check("an oceanic clearance is named", "Oceanic clearance" in A.application("/PIKCLYA.CLA 1224 261127 EGGX CLRNCE 724"))
check("a datalink logon is named", "logon" in A.application("/SOUCAYA.AFN/FMHAAL705,.N793AN,AAC337,110800"))
check("a position contract is named", "Position contract" in A.application("/PIKCPYA.ADS.N778AN0301A999"))
check("the ground station is named", "PIKCLYA" in A.application("/PIKCLYA.CLA 1224 261127 EGGX"))
check("an unknown application says nothing", A.application("RST17A27102900KLM706  SBGLEHAM") == "")

# --- laying the body out for reading ----------------------------------------------------------------
check("the body splits on the bar", A.lines("A | B | C") == ["A", "B", "C"])
check("empty pieces are dropped", A.lines("A | | B\n\nC") == ["A", "B", "C"])

# --- the whole thing together -------------------------------------------------------------------------
d = A.decode("POSA1N55611W  5123,ERKIT  ,112051,339,GOMUP  ,120916,,-55, 81,1427,832", near=HOME, not_apt=_NOT_APT)
check("decode gives position and waypoints together",
      abs(d["lat"] - 55.611) < 0.001 and d["waypoints"][0] == ["ERKIT", "11:20:51"], d)
d = A.decode("", near=HOME)
check("an empty message decodes to nothing", d == {}, d)

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
