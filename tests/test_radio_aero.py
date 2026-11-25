"""End-to-end: the real Radio engine on the built-in Simulator runs the AERO decoder against a fake SatDump.
Checks: tunes into the Inmarsat band, shares IQ with SatDump, a decoded message reaches the AIRCRAFT TEXT store (tagged AERO),
the status reaches the screen, 'centre on carrier' moves the VFO, and tuning away stops the decoder.
Run:  PYTHONPATH=. python tests/test_radio_aero.py   (about 25 s)"""
import os
import sys
import tempfile
import time

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from andysdr import decoders  # noqa: E402
from andysdr.config import Settings  # noqa: E402
from andysdr.engine import Radio  # noqa: E402

FAKE = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_satdump.py")]
decoders.find_exe = lambda name, ov=None: FAKE if name == "satdump" else None
ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if extra and not cond else ""))
    ok = ok and bool(cond)


s = Settings()
s["device"] = "sim"
r = Radio(s)
status, acars, logs, events = [], [], [], []


def on(kind, p):
    if kind == "datalink_status" and "aero" in p:
        status.append(p["aero"])
    elif kind == "acars":
        acars.append(p)
    elif kind == "decoder":
        logs.append(p.get("text"))
    elif kind == "event":
        events.append(p.get("text"))


r.add_listener(on)
r.start()
time.sleep(1.5)
r.set_frequency(100000000, "WFM")                         # somewhere that is not the Inmarsat band
time.sleep(0.5)
r.set_decoder("aero", "p600")
t0 = time.time()
while time.time() - t0 < 25 and not (acars and any(x.get("lock") for x in status)):
    time.sleep(0.3)
st = r.state()
check("decoder is active", st["decoder"] == "aero", st["decoder"])
check("tuned into the Inmarsat band (1546.100 MHz default)", abs(r.freq - 1546100000) < 1, r.freq)
check("mode is a 25 kHz digital channel", r.mode == "DIG25", r.mode)
check("message reached the AIRCRAFT TEXT store", bool(acars) and acars[0]["reg"] == "G-EUUU" and acars[0]["via"] == "AERO"
      and acars[0]["icao"] == "4CA123" and acars[0]["freq"] == 1546100000, acars[:1])
check("status says locked on 600 bit/s", any(x.get("lock") and x.get("mode") == "p600" and x.get("bps") == 600 for x in status), status[-2:])
check("SatDump is receiving IQ from the radio", any(isinstance(l, str) and l.startswith("bytes=") and int(l[6:]) > 100000 for l in logs), logs[-4:])

# 'centre on the carrier'
a = r.decoders.aero
a.off = (250.0, 12.0)
f0 = r.freq
r.aero_centre()
time.sleep(0.8)
check("centre-on-carrier moves the VFO by the measured offset", abs((r.freq - f0) - 250) <= 1, r.freq - f0)
a.off = None
n = len(events)
r.aero_centre()
time.sleep(0.5)
check("centre with no carrier says so", any("no carrier" in (e or "") for e in events[n:]), events[n:])

# tuning away stops it
r.set_frequency(145500000, "NFM")
time.sleep(1.5)
check("tuning away from L-band stops the decoder", r.state()["decoder"] in (None, ""), r.state()["decoder"])
check("SatDump process is gone", r.decoders.aero is None)
check("final status says stopped", status and status[-1] == {"running": False}, status[-1:])

# stopping with no decoder
r.aero_centre()
time.sleep(0.4)
check("centre without the decoder is refused politely", any("Start the AERO decoder" in (e or "") for e in events))
r.stop() if hasattr(r, "stop") else None
print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
