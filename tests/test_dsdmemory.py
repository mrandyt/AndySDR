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
    m.dsd_try = 0; m.dsd_sync_t = 0.0; m.dsd_flags = ['-ft']
    m.dsd_good = D.deque(maxlen=60); m.dsd_saved = False; m.on_dsd_line = None
    return m

m = mgr()
check("a new frequency has nothing remembered", m._dsd_recall() is None)
m.dsd_flags = ['-fn']                          # locked while trying -fn
m._dsd_remember()
check("a lock is remembered for the frequency (and saved)", m.settings["dsd_memory"] == {"167925": ["-fn"]} and S.saves == 1, m.settings)
check("... and recalled as the same place in the list", m._dsd_recall() == 1)
m._dsd_remember(); check("remembering the same thing again does not save again", S.saves == 1)
m2 = mgr(167_925_400); m2.settings = m.settings
check("the same channel (within 0.5 kHz) finds it", m2._dsd_recall() == 1)
m3 = mgr(168_000_000); m3.settings = m.settings
check("another frequency does not", m3._dsd_recall() is None)
m.dsd_flags = ['-ft']; m._dsd_remember()
check("a different lock later replaces the old one", m.settings["dsd_memory"]["167925"] == ["-ft"])
m.settings["dsd_memory"]["167925"] = ["-zz"]
check("a stale or unknown entry is ignored", m._dsd_recall() is None)
for i in range(D.DecoderManager.DSD_MEMORY_MAX + 20):
    mm = mgr(100_000_000 + i * 12_500); mm.settings = m.settings; mm.dsd_flags = ['-fi']; mm._dsd_remember()
check("the memory is capped, dropping the oldest", len(m.settings["dsd_memory"]) == D.DecoderManager.DSD_MEMORY_MAX)
m4 = mgr(0); m4.dsd_flags = ['-fn']; m4._dsd_remember()
check("no frequency known: nothing is stored", "" not in m4.settings.get("dsd_memory", {}))

check("a frame type Auto never tries is not stored", (lambda x: (setattr(x, "dsd_flags", ["-fs"]), x._dsd_remember(), "167925" not in x.settings.get("dsd_memory", {}))[-1])(mgr()))

# which lines count as a lock, and when it is worth remembering
f = mgr(); f.dsd_flags = ["-fi"]
f._dsd_filter("18:11:10 Sync: NXDN48  RTCH Data           UDCH (CRC ERR)")
f._dsd_filter("18:11:10 Sync: +DMR  [slot1]  slot2  | CACH/Burst FEC ERR")
f._dsd_filter("Sync: no sync")
check("lines marked ERR and 'no sync' are not a lock", f.dsd_sync_t == 0.0 and len(f.dsd_good) == 0)
for _ in range(3): f._dsd_filter("18:11:16 Sync: NXDN48  RDCH V1+F2  RAN 01 PF X/4")
check("three clean frames are a lock but not yet worth remembering", f.dsd_sync_t > 0 and not f.dsd_saved and "dsd_memory" not in f.settings)
f._dsd_filter("18:11:17 Sync: NXDN48  RDCH V1+F2  RAN 01 PF X/4")
check("four clean frames in a few seconds are remembered", f.dsd_saved and f.settings["dsd_memory"] == {"167925": ["-fi"]}, f.settings)
f2 = mgr(); f2.dsd_flags = ["-ft"]; f2.settings = f.settings
f2.dsd_good.extend([time.time() - 30] * 10)
f2._dsd_filter("18:11:17 Sync: +DMR  [slot1]  slot2  | Color Code=01 | IDLE")
check("old frames do not add up to a lock", not f2.dsd_saved and f.settings["dsd_memory"] == {"167925": ["-fi"]})
f3 = mgr(); f3.dsd_flags = ["-ft"]; f3.settings = f.settings
for _ in range(5): f3._dsd_filter("18:12:52 Sync: +DMR   slot1  [slot2] | Color Code=01 | IDLE")
check("a later sustained lock on another frame type replaces it", f.settings["dsd_memory"]["167925"] == ["-ft"])

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

# optional gate: no noise to the decoder while the squelch has been closed for a while
g = mgr(); g.settings["dsd_gate"] = False
check("gate off: the decoder always gets the audio", g._dsd_gate_pass(False, 100.0) and g._dsd_gate_pass(False, 500.0))
g.settings["dsd_gate"] = True
check("gate on, squelch open: audio passes", g._dsd_gate_pass(True, 1000.0))
check("... and for 1.5 s after it closes (call endings)", g._dsd_gate_pass(False, 1001.0) and g._dsd_gate_pass(False, 1001.4))
check("... then the decoder gets silence", not g._dsd_gate_pass(False, 1001.6) and not g._dsd_gate_pass(False, 1100.0))
check("... until the squelch opens again", g._dsd_gate_pass(True, 1101.0) and g._dsd_gate_pass(False, 1102.0))
g2 = mgr(); g2.settings["dsd_gate"] = True
check("gate on with no signal since start: silence", not g2._dsd_gate_pass(False, 5.0))

print("ALL PASS" if not fails else "FAILED: " + "; ".join(fails))
sys.exit(1 if fails else 0)
