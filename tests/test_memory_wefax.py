"""WEFAX Northwood memory channel: stored at the carrier (8038.1 kHz), and an old saved 8040.0 kHz entry is corrected on load."""
import json, os, sys, tempfile
os.environ["APPDATA"] = tempfile.mkdtemp()
os.environ["HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import memory as M

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    if not ok: fails.append(name)

def freqs_named(m, name):
    return [c["freq"] for b in m.data["banks"].values() for c in b["channels"].values() if c.get("name") == name]

# 1. fresh install: the starter channel is at the carrier
m = M.MemoryBank()
check("fresh starter WEFAX Northwood is 8038.100 kHz", freqs_named(m, "WEFAX Northwood") == [8038100], freqs_named(m, "WEFAX Northwood"))

# 2. an existing memory.json with the old 8040.0 entry is corrected when loaded and saved

data = json.load(open(m.path, encoding="utf-8"))
data["banks"]["0"]["channels"]["0"] = {"freq": 8040000, "mode": "USB", "name": "WEFAX Northwood", "tag": "Weather", "decoder": "wefax", "lockout": False, "delay": True, "description": "", "tone": ""}
data["banks"]["0"]["channels"]["1"] = {"freq": 8040000, "mode": "USB", "name": "My own 8040", "tag": "", "decoder": "", "lockout": False, "delay": True, "description": "", "tone": ""}
json.dump(data, open(m.path, "w", encoding="utf-8"))
m2 = M.MemoryBank()
ch = m2.data["banks"]["0"]["channels"]
check("saved 8040.0 WEFAX Northwood corrected to 8038.1 on load", ch["0"]["freq"] == 8038100, ch["0"]["freq"])
check("a differently named 8040.0 channel is left alone", ch["1"]["freq"] == 8040000, ch["1"]["freq"])
saved = json.load(open(m2.path, encoding="utf-8"))
check("the correction is written back to disk", saved["banks"]["0"]["channels"]["0"]["freq"] == 8038100)

print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
