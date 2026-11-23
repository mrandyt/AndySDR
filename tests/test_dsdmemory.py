"""DSD-FME Auto: remember the frame type that locked on each frequency, and do not drop a decoder that just locked."""
import os, sys, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import decoders as D
from andysdr import engine as E

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    if not ok: fails.append(name)

class S(dict):
    saves = 0
    def save(self): S.saves += 1

def mgr(freq=167_925_000):
    m = D.DecoderManager.__new__(D.DecoderManager)
    m.settings = S(); m.freq_fn = lambda: freq; m.emit = lambda *a: None; m.active = "dsd"; m.sub = "auto"
    m.dsd_try = 0; m.dsd_sync_t = 0.0
    return m

m = mgr()
check("a new frequency has nothing remembered", m._dsd_recall() is None)
m.dsd_try = 1                                  # locked while trying -fn
m._dsd_remember()
check("a lock is remembered for the frequency (and saved)", m.settings["dsd_memory"] == {"167925": ["-fn"]} and S.saves == 1, m.settings)
check("... and recalled as the same place in the list", m._dsd_recall() == 1)
m._dsd_remember(); check("remembering the same thing again does not save again", S.saves == 1)
m2 = mgr(167_925_400); m2.settings = m.settings
check("the same channel (within 0.5 kHz) finds it", m2._dsd_recall() == 1)
m3 = mgr(168_000_000); m3.settings = m.settings
check("another frequency does not", m3._dsd_recall() is None)
m.dsd_try = 0; m._dsd_remember()
check("a different lock later replaces the old one", m.settings["dsd_memory"]["167925"] == ["-ft"])
m.settings["dsd_memory"]["167925"] = ["-zz"]
check("a stale or unknown entry is ignored", m._dsd_recall() is None)
for i in range(D.DecoderManager.DSD_MEMORY_MAX + 20):
    mm = mgr(100_000_000 + i * 12_500); mm.settings = m.settings; mm.dsd_try = 2; mm._dsd_remember()
check("the memory is capped, dropping the oldest", len(m.settings["dsd_memory"]) == D.DecoderManager.DSD_MEMORY_MAX)
m4 = mgr(0); m4.dsd_try = 1; m4._dsd_remember()
check("no frequency known: nothing is stored", "" not in m4.settings.get("dsd_memory", {}))

# recent lock
r = mgr(); r.dsd_sync_t = time.time() - 5
check("a lock 5 s ago counts as recent", r.dsd_recent_sync(30))
r.dsd_sync_t = time.time() - 60
check("a lock a minute ago does not", not r.dsd_recent_sync(30))
r.dsd_sync_t = 0.0
check("never locked is not recent", not r.dsd_recent_sync(30))

# the engine must not stop a freshly locked DSD decoder because the classifier briefly says "FM voice"
class Dec:
    def __init__(s, recent): s.active = "dsd"; s.recent = recent; s.stopped = 0; s.sstv = None
    def dsd_recent_sync(s, n): return s.recent
    def stop(s): s.stopped += 1
def eng(recent):
    return types.SimpleNamespace(decoders=Dec(recent), auto_owned=True, auto_failed=set(), auto_mode_prev=None, mode="NFM",
                                 _set_signal=lambda r: None, _push_state=lambda *a: None, rx=None)
voice = {"kind": "FM voice", "stable": True, "decoder": "", "sub": "auto"}
e = eng(True); E.Radio._auto_apply(e, dict(voice))
check("a voice reading right after a lock leaves the decoder running", e.decoders.stopped == 0 and e.auto_owned)
e = eng(False); E.Radio._auto_apply(e, dict(voice))
check("with no recent lock, plain voice still stops it (as before)", e.decoders.stopped == 1 and not e.auto_owned)

print("ALL PASS" if not fails else "FAILED: " + "; ".join(fails))
sys.exit(1 if fails else 0)
