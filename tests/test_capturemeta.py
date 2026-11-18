"""Self-test: capture log -> picture / recording descriptions (python tests/test_capturemeta.py)."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
tmp = tempfile.mkdtemp()
os.environ["HOME"] = tmp
os.environ.pop("APPDATA", None)

from andysdr import capturemeta as cm   # noqa: E402
from andysdr.config import data_dir      # noqa: E402

t0 = time.mktime(time.strptime("20261118_101200", "%Y%m%d_%H%M%S"))
with open(os.path.join(data_dir(), "capture_log.jsonl"), "w") as f:
    f.write(json.dumps({"t": t0, "kind": "lrpt", "freq": 137900000,
                        "name": "Meteor-M N2-3 (LRPT)", "mode": "DIG25"}) + "\n")
    f.write(json.dumps({"t": t0 - 3000, "kind": "wefax", "freq": 8038100, "name": "", "mode": "USB"}) + "\n")
ents = cm.entries()

d = cm.describe("lrpt_20261118_101200_240k/MSU-MR_RGB.png", t0 + 900, 1234, ents)
assert d["name"] == "Meteor-M N2-3 (LRPT)" and d["freq"] == 137900000, d
assert d["dl"] == "Meteor-M_N2-3_LRPT_137.9000MHz_20261118_1012_MSU-MR_RGB.png", d["dl"]
assert d["when"].endswith("10:12"), d

d = cm.describe("wefax_20261118_092300.png", t0, 10, ents)
assert d["freq"] == 8038100 and "Northwood" in d["name"], d

d = cm.describe("audio_20261118_101200_145.8000MHz.wav", t0, 10, ents)
assert d["freq"] == 145800000 and d["name"] == "Audio recording", d

d = cm.describe("old_picture.png", t0, 10, ents)         # unknown file: still described, no crash
assert d["name"] == "Picture" and d["freq"] == 0, d

cm.note("sstv", 145800000, "ISS SSTV (ARISS) pass (max 61°)", "NFM")
e = cm.entries()[-1]
assert e["name"] == "ISS SSTV (ARISS)", e
print("capturemeta OK")
