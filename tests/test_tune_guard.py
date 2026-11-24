"""A frequency outside the receiver's range (a typo such as kHz typed as MHz) is refused, never sent to the hardware.
Run:  python -I tests/test_tune_guard.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
home = tempfile.mkdtemp(); os.environ["HOME"] = home; os.environ["APPDATA"] = home
from andysdr.config import Settings
from andysdr.engine import Radio

s = Settings(); s["device"] = "sim"
r = Radio(s)
ev = []
r.add_listener(lambda k, p: ev.append((k, p)) if k == "event" else None)
r.start()
fails = 0
def check(n, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + n); fails += 0 if ok else 1
import time; time.sleep(1.0)
r.set_frequency(145500000); time.sleep(0.5)
check("a normal frequency is accepted", r.freq == 145500000)
centre = r.dev.center
r.set_frequency(131725600000); time.sleep(0.5)
check("131.7 GHz is refused: the frequency does not change", r.freq == 145500000)
check("... and the hardware is never retuned", r.dev.center == centre)
check("... and the screen is told why", any("outside what this receiver can tune" in p["text"] for k, p in ev))
r.set_frequency(5000); time.sleep(0.3)
check("5 kHz is below the simulator's range and refused", r.freq == 145500000)
r.set_frequency(131725000); time.sleep(0.5)
check("131.725 MHz is fine", r.freq == 131725000)
r.stop()
print("FAILED" if fails else "ALL OK")
sys.exit(1 if fails else 0)
