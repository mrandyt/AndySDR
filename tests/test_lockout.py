"""Temporary lockout (SKIP) on the real engine + Simulator. Run:  python -I tests/test_lockout.py   (about 40 s)"""
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

ok = fail = 0


def check(name, cond):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print("  ok  " if cond else "  FAIL", name)


s = Settings()
s["device"] = "sim"
s["squelch"] = -52.0
s["scan_delay"] = 0.5
r = Radio(s)
events = []
r.add_listener(lambda k, p: events.append(p.get("text")) if k == "event" else None)
r.start()
time.sleep(1.5)
r.memory.clear_bank(0)                      # the default memory has its own 2 m channels
r.memory.import_channels(1, [{"freq": 145550000, "mode": "NFM", "name": "TONE", "tag": "", "decoder": ""},
                             {"freq": 145500000, "mode": "NFM", "name": "VOICE", "tag": "", "decoder": ""}], False, None)
r.set_squelch(-52.0)


def wait_hold(sec, want=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        st = r.state()
        if st["scan"] == "hold" and (want is None or r.freq == want):
            return True
        time.sleep(0.05)
    return False


def holds_during(sec):
    seen = set()
    t0 = time.time()
    while time.time() - t0 < sec:
        if r.scan["state"] == "hold":
            seen.add(r.freq)
        time.sleep(0.05)
    return seen


print("memory scan")
r.start_scan()
check("scanner stops on the continuous tone channel", wait_hold(15, 145550000))
check("not locked yet", not r.state()["lockouts"]["current"] and r.state()["lockouts"]["count"] == 0)
r.lockout_temp(10)
time.sleep(0.5)
st = r.state()
check("skip recorded", st["lockouts"]["count"] == 1 and "TONE" in st["lockouts"]["list"][0])
check("scanner carried on (state scan, not stuck on TONE)", r.scan["state"] in ("scan", "hold"))
seen = holds_during(10)
print("   holds during 10 s:", sorted(seen))
check("TONE channel not revisited while skipped", 145550000 not in seen)
check("other channel still scanned", 145500000 in seen)
check("skip survives (still counted)", r.state()["lockouts"]["count"] == 1)
r.stop_scan()
time.sleep(0.3)
r.recall(1, 0)
time.sleep(0.5)
check("sitting on the skipped channel manually is flagged", r.state()["lockouts"]["current"])
r.start_scan()
r.lockout_clear()
time.sleep(0.4)
check("unskip clears the list", r.state()["lockouts"]["count"] == 0)
check("TONE channel is scanned again", wait_hold(15, 145550000))

print("expiry")
r.lockout_temp(10)
time.sleep(0.4)
key = next(iter(r.temp_lock))
r.temp_lock[key] = (time.time() - 1, "x")          # pretend the 10 minutes are over
check("expired skip drops out", r.state()["lockouts"]["count"] == 0 and not r.temp_lock)

print("search")
r.lockout_clear()
r.stop_scan()
time.sleep(0.5)
r.start_search(145000000, 146000000, 12500, "NFM", "stop")
check("search stops on the strongest signal", wait_hold(25))
f0 = r.freq
r.lockout_temp(30)
time.sleep(0.5)
check("search skip stored by frequency", any(k[0] == "f" for k in r.temp_lock))
seen = holds_during(14)
print("   skipped", f0, "-> later holds", sorted(seen))
check("skipped frequency not stopped on again", f0 not in seen)
r.lockout_clear()
r.stop_scan()
check("events were reported", any(e and "Skipping" in e for e in events) and any(e and "Cleared" in e for e in events))

r.stop()
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
