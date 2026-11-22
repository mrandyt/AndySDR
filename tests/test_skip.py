"""SKIP key (carry straight on scanning, no lockout) on the real engine + Simulator.  python -I tests/test_skip.py  (about 40 s)"""
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
s["scan_delay"] = 1.0
r = Radio(s)
events = []
r.add_listener(lambda k, p: events.append(p.get("text")) if k == "event" else None)
r.start()
time.sleep(1.5)
r.memory.clear_bank(0)
r.memory.import_channels(1, [{"freq": 145550000, "mode": "NFM", "name": "TONE", "tag": "", "decoder": ""},
                             {"freq": 145500000, "mode": "NFM", "name": "VOICE", "tag": "", "decoder": ""}], False, None)
r.set_squelch(-52.0)


def wait_hold(sec, want=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        if r.scan["state"] == "hold" and (want is None or r.freq == want):
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


print("not scanning")
n = len(events)
r.skip()
time.sleep(0.4)
check("SKIP while not scanning only says what to do", any(e and "press SCAN" in e for e in events[n:]))

print("memory scan")
r.start_scan()
check("scanner stops on the tone channel", wait_hold(15, 145550000))
t0 = time.time()
r.skip()
time.sleep(0.6)
check("SKIP leaves the held channel at once (a continuous tone never ends by itself)",
      r.scan["state"] != "hold" or r.freq != 145550000)
check("nothing was locked out", r.state()["lockouts"]["count"] == 0 and not r.temp_lock)
seen = holds_during(14)
print("   holds after skip:", sorted(seen), f"({time.time() - t0:.1f} s)")
check("scanning carried on to the other channel", 145500000 in seen)
check("the skipped channel comes round again (no lockout)", 145550000 in seen)
r.stop_scan()
time.sleep(0.3)

print("search")
r.start_search(145000000, 146000000, 12500, "NFM", "stop")
check("search stops on the strongest signal", wait_hold(25))
f0 = r.freq
r.skip()
time.sleep(0.6)
check("search skip: scanner is moving on again", r.scan["state"] == "search" or r.freq != f0)
check("search skip only ignores that signal for a short while", len(r.temp_lock) == 1 and not r.search_lockouts
      and 0 < list(r.temp_lock.values())[0][0] - time.time() <= r.SEARCH_SKIP_S)
seen = holds_during(8)
print("   skipped", f0, "-> holds in the next 8 s", sorted(seen))
check("the skipped signal is not stopped on again straight away", f0 not in seen)
check("SKIP is reported", any(e and "Skipped" in e for e in events))
r.stop_scan()

r.stop()
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
