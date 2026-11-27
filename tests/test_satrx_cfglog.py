"""Changing a second-receiver setting is written to the log with its old and new value and who asked.

This exists because the "use both receivers" tickbox has twice been found holding a value nobody remembered setting.
Without a record of the change there is no way to tell a bug from somebody else's click.
"""
import logging, os, sys, tempfile
tmp = tempfile.mkdtemp()
os.environ["HOME"] = tmp
os.environ["APPDATA"] = tmp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import devices
from andysdr.config import DEFAULTS
from andysdr.satrx import SatRx, SETTINGS

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1

class S(dict):
    def save(self): pass

class FakeSched:
    def _emit(self): pass

class FakeRadio:
    def __init__(self):
        # start from the real defaults, as the running program does, so the logged "old" value is the real one
        self.s = S({name: DEFAULTS[name] for name in SETTINGS.values()})
        self.events, self.dev = [], None
        self.scheduler = FakeSched()
    def _emit(self, k, v): self.events.append((k, v))

class Catcher(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
    def emit(self, rec): self.lines.append(rec.getMessage())

devices.list_rtl_native = lambda: []
rx = SatRx(FakeRadio())

cat = Catcher()
lg = logging.getLogger("andysdr.satrx")
lg.addHandler(cat)
lg.setLevel(logging.INFO)

# ---- a change is logged, with the old value, the new value and who asked
rx.configure(who="192.168.1.50", both=True)
line = " | ".join(cat.lines)
check("turning 'use both receivers' on is logged", "satrx_both" in line, line)
check("the log carries the old and the new value", "False" in line and "True" in line, line)
check("the log says who asked", "192.168.1.50" in line, line)

# ---- setting the same value again is not a change, so nothing is logged
cat.lines.clear()
rx.configure(who="192.168.1.50", both=True)
check("writing the same value again logs nothing", cat.lines == [], cat.lines)

# ---- turning it back off is logged too
cat.lines.clear()
rx.configure(who="local", both=False)
check("turning it off again is logged", any("satrx_both" in x for x in cat.lines), cat.lines)

# ---- a caller that gives no name still gets a line
cat.lines.clear()
rx.configure(both=True)
check("a change with no name given is still logged", any("satrx_both" in x for x in cat.lines), cat.lines)
check("an unnamed caller is recorded as unknown", any("?" in x for x in cat.lines), cat.lines)

# ---- every setting configure() writes is covered, not just the tickbox
cat.lines.clear()
rx.configure(who="test", gain=22.0, ppm=3, bias=True, gain_mode="auto", device="rtlsdr:ABC", enabled=True)
got = " | ".join(cat.lines)
for name in ("satrx_gain", "satrx_ppm", "satrx_bias", "satrx_gain_mode", "satrx_device", "satrx_enabled"):
    check(f"{name} change is logged", name in got, got)

# ---- the settings really were written
check("the values reached the settings", rx.cfg()["ppm"] == 3 and rx.cfg()["bias"] and rx.cfg()["enabled"], rx.cfg())

lg.removeHandler(cat)
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
