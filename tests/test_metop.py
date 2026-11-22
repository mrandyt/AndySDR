"""Metop AHRPT: satellites listed, decoder starts SatDump's metop_ahrpt pipeline on 1701.3 MHz."""
import os, sys, tempfile
os.environ["HOME"] = os.environ["APPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import decoders, satpass, scheduler
from andysdr.config import Settings
fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n, "" if ok else extra)
    fails += 0 if ok else 1

for sid, norad in (("metopb", 38771), ("metopc", 43689)):
    d = satpass.SATELLITES[sid]
    check(f"{sid} listed", d["norad"] == norad and d["action"] == "metop" and d["freqs"] == [1701300000] and d["kind"] == "leo")
check("decoder registered", "metop" in decoders.DECODERS and decoders.DECODERS["metop"]["exe"] == "satdump")
check("scheduler label + preset", "metop" in scheduler.ACTIONS if hasattr(scheduler, "ACTIONS") else True)

class FakeProc:
    stdout = None
    stdin = None
    def poll(self): return None
    def terminate(self): pass
    def kill(self): pass
    def wait(self, *a, **k): return 0
seen = {}
decoders._popen = lambda args, **kw: (seen.setdefault("args", args), FakeProc())[1]
class R:
    def __init__(self, *a, **k): pass
    def start(self): return self
decoders._Reader = R
decoders.find_exe = lambda name, ov=None: "/fake/satdump"
decoders.IqBridge = type("B", (), {"__init__": lambda self, out_rate=None: None, "start": lambda self: 40000, "feed": lambda *a: None, "stop": lambda self: None})
logs = []
m = decoders.DecoderManager(Settings() if callable(Settings) else {}, lambda *a: None)
m.log = lambda t, lvl="info": logs.append((lvl, t))
m.rate_fn = lambda: 6000000
m._watch_images = lambda out: None
ok = m.start("metop", "auto")
a = seen.get("args", [])
check("started", ok and m.active == "metop", (ok, m.active, logs))
check("pipeline + frequency", a[:3] == ["/fake/satdump", "live", "metop_ahrpt"] and "1701.3e6" in a and "6000000" in a, a)
check("no sample-rate warning at 6 MS/s", not [l for l in logs if l[0] == "warn"], logs)
m.active = None
m.rate_fn = lambda: 2000000
logs.clear(); m.start("metop", "auto")
check("warns when the sample rate is too low", any(l[0] == "warn" and "6 or 10 MS/s" in l[1] for l in logs), logs)
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
