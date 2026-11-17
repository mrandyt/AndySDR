"""The real Radio engine on the Simulator: start the SDR-only MeshCore receiver, check it tunes, runs, and stops cleanly.
Run:  python -I tests/test_radio_meshsdr.py"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home

from andysdr.config import Settings  # noqa: E402
from andysdr.engine import Radio  # noqa: E402

fails = 0
def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1

s = Settings()
s["device"] = "sim"
r = Radio(s)
snaps = []
r.add_listener(lambda k, p: snaps.append(p) if k == "meshsdr" else None)
r.start()
time.sleep(1.5)
r.start_meshsdr()
time.sleep(4)
check("decoder active", r.decoders.active == "meshsdr")
check("tuned to the MeshCore channel", abs(r.freq - 869618000) < 1000)
check("receiver running", r.meshsdr.running)
check("samples reaching the decoder", r.meshsdr.stats["samples"] > 100000)
r.stop_meshsdr()
time.sleep(1.5)
check("stopped cleanly", r.decoders.active in (None, "") and not r.meshsdr.running)
check("snapshots published", len(snaps) >= 1 and "nodes" in snaps[-1])
r.stop()
sys.exit(1 if fails else 0)
