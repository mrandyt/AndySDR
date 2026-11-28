"""A timed capture that is doing a job AndySDR already does by tracking the satellite.

This is the 04:05 case, from Andy's own log on 26 Nov 2026: a daily capture on 137.900 MHz from 03:46 for
twenty-five minutes held the radio until 04:11, so the Meteor-M N2-4 pass that started at 04:05 was recorded from
04:11 to 04:15 - four minutes of ten. The schedule already warned that two things overlapped. What it did not say
is that the two were the same job, that the timer could not steer with the satellite, and that switching the timer
off would lose nothing at all.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home

from andysdr.config import Settings            # noqa: E402
from andysdr.scheduler import Scheduler        # noqa: E402

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


class Sats:
    """Just enough of the satellite side: which are tracked and on what frequency."""

    def __init__(self, on=("meteor_m23", "meteor_m24"), freq=None):
        self.on, self.freq = set(on), freq

    def cfg(self):
        from andysdr.satpass import SATELLITES
        return {sid: {"enabled": sid in self.on, "freq": self.freq} for sid in SATELLITES}

    def observer(self):
        return None

    def upcoming(self, *a, **kw):
        return []

    def status(self):                        # the scheduler asks for this when it tells the screens anything
        return {"sats": [], "passes": [], "observer": None, "have_sgp4": True, "busy": False, "error": ""}


class Radio:
    def __init__(self):
        self.events = []
        self.passlog = None

    def _emit(self, kind, payload):
        self.events.append((kind, payload))


def sched(sats=None):
    s = Settings()
    s["schedule"] = []
    r = Radio()
    sc = Scheduler(r, s)
    sc.sats = sats or Sats()
    return sc, r, s


# --- the real case ----------------------------------------------------------------------------------------
sc, radio, st = sched()
job = sc.save_job({"name": "Meteor-M LRPT 137.900 MHz", "freq": 137_900_000, "mode": "DIG25",
                   "action": "lrpt", "start": "03:46", "duration": 25, "days": [], "priority": 1})
dup = sc.redundant()
check("the duplicate is spotted", len(dup) == 1 and dup[0]["id"] == job["id"], dup)
check("...and names the satellites it duplicates",
      dup and set(dup[0]["sats"]) == {"Meteor-M N2-3 (LRPT)", "Meteor-M N2-4 (LRPT)"}, dup and dup[0]["sats"])
check("...and says switching it off loses nothing", dup and "loses nothing" in dup[0]["text"], dup and dup[0]["text"])
check("...and it was said when the capture was saved, not only later",
      any("already does properly" in (p.get("text") or "") for _k, p in radio.events), radio.events)
check("the schedule hands it to the screen", "redundant" in sc.status() and len(sc.status()["redundant"]) == 1)

# --- what must NOT be called a duplicate ---------------------------------------------------------------------
sc2, _r2, _s2 = sched()
sc2.save_job({"name": "Marine VHF", "freq": 156_800_000, "mode": "NFM", "action": "listen",
              "start": "09:00", "duration": 30, "days": [], "priority": 1})
check("an ordinary capture is left alone", sc2.redundant() == [], sc2.redundant())

sc3, _r3, _s3 = sched()
sc3.save_job({"name": "Just listening on the Meteor frequency", "freq": 137_900_000, "mode": "NFM",
              "action": "listen", "start": "09:00", "duration": 30, "days": [], "priority": 1})
check("the same frequency with a different purpose is not a duplicate", sc3.redundant() == [],
      sc3.redundant())

sc4, _r4, _s4 = sched(Sats(on=()))
sc4.save_job({"name": "Meteor timer", "freq": 137_900_000, "mode": "DIG25", "action": "lrpt",
              "start": "03:46", "duration": 25, "days": [], "priority": 1})
check("a satellite that is not tracked is not being duplicated", sc4.redundant() == [], sc4.redundant())

sc5, _r5, _s5 = sched()
j5 = sc5.save_job({"name": "Meteor timer", "freq": 137_900_000, "mode": "DIG25", "action": "lrpt",
                   "start": "03:46", "duration": 25, "days": [], "priority": 1, "enabled": False})
check("a capture already switched off is not nagged about", sc5.redundant() == [], sc5.redundant())

# --- switching it off is what the button does, and it works ----------------------------------------------------
sc.save_job({**job, "enabled": False})
check("switching it off clears the warning", sc.redundant() == [], sc.redundant())
check("...and the capture is still there, just off",
      len(sc.jobs()) == 1 and sc.jobs()[0]["enabled"] is False, sc.jobs())

# --- a satellite moved to its own frequency is still matched -----------------------------------------------------
sc6, _r6, _s6 = sched(Sats(freq=137_100_000))
sc6.save_job({"name": "137.1 timer", "freq": 137_100_000, "mode": "DIG25", "action": "lrpt",
              "start": "12:00", "duration": 20, "days": [], "priority": 1})
check("a satellite set to another of its frequencies is matched too", len(sc6.redundant()) == 1, sc6.redundant())

# --- a few kHz out is still the same job --------------------------------------------------------------------------
sc7, _r7, _s7 = sched()
sc7.save_job({"name": "Meteor timer, tuned a bit off", "freq": 137_902_500, "mode": "DIG25", "action": "lrpt",
              "start": "03:46", "duration": 25, "days": [], "priority": 1})
check("a couple of kHz out is still the same job", len(sc7.redundant()) == 1, sc7.redundant())
sc8, _r8, _s8 = sched()
sc8.save_job({"name": "Well away", "freq": 138_500_000, "mode": "DIG25", "action": "lrpt",
              "start": "03:46", "duration": 25, "days": [], "priority": 1})
check("...but half a megahertz away is not", sc8.redundant() == [], sc8.redundant())

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
