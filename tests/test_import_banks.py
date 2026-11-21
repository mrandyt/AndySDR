"""CSV import fills only the chosen bank by default; more banks only on request, and only empty ones."""
import os, sys, tempfile
os.environ["APPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr.memory import MemoryBank as Memory
fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else "")); fails += 0 if ok else 1
def chans(n): return [{"freq": 100000000 + i * 12500, "mode": "NFM", "name": f"C{i}", "tag": "", "decoder": ""} for i in range(n)]
def used(m): return {b: len(v["channels"]) for b, v in m.data["banks"].items() if v["channels"]}

m = Memory(); [m.clear_bank(b) for b in range(10)]
a, o = m.import_channels(1, chans(450), False, "BIG")
check("default: only bank 1 is filled (100)", used(m) == {"1": 100} and (a, o) == (100, 350), (used(m), a, o))
m = Memory(); [m.clear_bank(b) for b in range(10)]
a, o = m.import_channels(1, chans(450), False, "BIG", 3)
check("3 banks: 1,2,3 filled, rest overflow", used(m) == {"1": 100, "2": 100, "3": 100} and (a, o) == (300, 150), (used(m), a, o))
check("later banks are named", m.data["banks"]["2"]["name"] == "BIG 2" and m.data["banks"]["1"]["name"] == "BIG")
m = Memory(); [m.clear_bank(b) for b in range(10)]
m.import_channels(2, chans(5), False, "MINE")
a, o = m.import_channels(1, chans(250), False, "BIG", 4)
check("a bank that already has channels is never touched", len(m.data["banks"]["2"]["channels"]) == 5 and m.data["banks"]["2"]["name"] == "MINE", used(m))
check("...so the spill skips it (1 then 3,4 not possible: 4 banks 1-4)", used(m) == {"1": 100, "2": 5, "3": 100, "4": 50}, used(m))
m = Memory(); [m.clear_bank(b) for b in range(10)]
m.import_channels(2, chans(5), False, "MINE")
m.import_channels(1, chans(250), True, "BIG", 3)
check("replace clears the banks it will use", used(m) == {"1": 100, "2": 100, "3": 50}, used(m))
m = Memory(); [m.clear_bank(b) for b in range(10)]
a, o = m.import_channels(9, chans(150), False, None, 5)
check("cannot run past bank 9", used(m) == {"9": 100} and (a, o) == (100, 50), (used(m), a, o))
m = Memory(); [m.clear_bank(b) for b in range(10)]
m.import_channels(1, chans(150), False, "BIG", 2, {"file": "uk-airband.csv", "file_date": "2026-11-12"})
s1, s2 = m.data["banks"]["1"].get("source"), m.data["banks"]["2"].get("source")
check("file name, file date and import time are recorded on every bank used", s1 and s1["file"] == "uk-airband.csv" and s1["file_date"] == "2026-11-12" and s1["imported"] and s2 == s1, (s1, s2))
m.clear_bank(1); check("clearing a bank forgets its source", "source" not in m.data["banks"]["1"])
print("FAILED" if fails else "ALL OK"); sys.exit(1 if fails else 0)
