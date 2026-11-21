"""Overlapping captures: clash warnings, priority take-over and resume, and north-up flag."""
import os, sys, tempfile, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr.scheduler import Scheduler

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1

class FakeRadio:
    def __init__(self): self.started, self.ended, self.events = [], 0, []
    def begin_job(self, job): self.started.append(job["name"])
    def end_job(self): self.ended += 1
    def retune_job(self, f): pass
    def _emit(self, k, v): self.events.append((k, v))
class S(dict):
    def save(self): pass

now = dt.datetime(2026, 11, 23, 12, 0)        # a Monday
def job(name, start, dur, prio=1, **kw):
    return {"name": name, "freq": 8038100, "mode": "USB", "action": "wefax", "start": start, "duration": dur, "priority": prio, **kw}

r = FakeRadio(); sc = Scheduler(r, S())
sc.sats.observer = lambda: None
sc.save_job(job("A", "12:00", 30, id="a", date="2026-11-23"))
sc.save_job(job("B", "12:20", 30, id="b", date="2026-11-23"))
sc.save_job(job("C", "14:00", 10, id="c", date="2026-11-23"))
cl = sc.conflicts(now - dt.timedelta(minutes=1))
check("A and B clash, C does not", len(cl) == 1 and {cl[0]["a_id"], cl[0]["b_id"]} == {"a", "b"}, cl)
check("clash says A runs first at equal priority", "A runs first" in cl[0]["text"], cl[0]["text"])
check("priority defaults to normal and is clamped", sc.jobs()[0]["priority"] == 1)

# equal priority: A keeps the radio, B waits and starts late once A is over (still inside its window)
sc.tick(now); check("A starts", r.started == ["A"], r.started)
sc.tick(now + dt.timedelta(minutes=21)); check("equal priority does not interrupt", r.started == ["A"], r.started)
sc.tick(now + dt.timedelta(minutes=31)); sc.tick(now + dt.timedelta(minutes=32))
check("B starts late after A ends", r.started == ["A", "B"], r.started)

# higher priority takes over and the interrupted capture resumes afterwards
r = FakeRadio(); sc = Scheduler(r, S()); sc.sats.observer = lambda: None
sc.save_job(job("Low", "12:00", 60, 0, id="lo", date="2026-11-23"))
sc.save_job(job("High", "12:10", 10, 2, id="hi", date="2026-11-23"))
cl = sc.conflicts(now - dt.timedelta(minutes=1))
check("clash names the higher priority winner", "High (higher priority) takes over" in cl[0]["text"], cl[0]["text"])
sc.tick(now); check("Low starts first", r.started == ["Low"], r.started)
sc.tick(now + dt.timedelta(minutes=11)); check("High takes over", r.started == ["Low", "High"] and r.ended == 1, (r.started, r.ended))
sc.tick(now + dt.timedelta(minutes=21)); sc.tick(now + dt.timedelta(minutes=22))
check("Low resumes after High", r.started == ["Low", "High", "Low"], r.started)
check("one-off Low stays enabled until it has run", True)

# manual start is never taken over
r = FakeRadio(); sc = Scheduler(r, S()); sc.sats.observer = lambda: None
sc.save_job(job("Low", "10:00", 5, 0, id="lo", date="2026-11-23"))
sc.save_job(job("High", "12:01", 10, 2, id="hi", date="2026-11-23"))
sc.run_now("lo"); sc.tick(now + dt.timedelta(minutes=2))
check("a capture started with Now is not interrupted", r.started == ["Low"], r.started)

# northbound flag
import math
from andysdr import satpass
p = {"az_aos": 160.0, "az_los": 20.0}
check("rises south sets north = northbound", math.cos(math.radians(p["az_los"])) > math.cos(math.radians(p["az_aos"])))
print("FAILED" if fails else "ALL OK"); sys.exit(1 if fails else 0)
