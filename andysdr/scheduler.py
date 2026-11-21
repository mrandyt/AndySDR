"""Capture scheduler: tune a frequency, run a decoder (WEFAX, SSTV, LRPT, ...) or record,
for a set time, then put the radio back the way it was.

A job:
  {"id": "a1b2c3", "name": "Northwood surface chart", "freq": 8038100, "mode": "USB",
   "action": "wefax" | "sstv" | "lrpt" | "pager" | "data" | "dsd" | "record" | "listen",
   "start": "HH:MM" (local time on this PC), "duration": minutes,
   "days": [0..6] (Mon=0; empty = every day), "date": "YYYY-MM-DD" (optional: once only),
   "priority": 0 low | 1 normal | 2 high (a higher one takes over the radio from a lower one),
   "enabled": true}

Only one capture can run at a time. If two overlap, the higher priority wins; at equal priority the one that
started first finishes first. A capture that was interrupted carries on afterwards if some of its time is left.
Satellite passes count as high priority (they cannot be repeated); geostationary captures count as low.
"""
import datetime as dt
import logging
import secrets
import threading
import time

from .satpass import SATELLITES, SatPlanner

log = logging.getLogger("andysdr.scheduler")

ACTIONS = {
    "wefax": "Weather fax (WEFAX)", "sstv": "SSTV pictures", "lrpt": "Meteor-M LRPT pass", "goes": "GOES HRIT (satellite)",
    "record": "Record audio", "listen": "Just tune & listen", "pager": "Pagers", "data": "APRS / data",
    "dsd": "Digital voice",
}

PRIORITY_NAMES = {0: "low", 1: "normal", 2: "high"}
CLASH_DAYS = 7

PRESETS = [
    {"name": "Met Office Northwood WEFAX 2618.5 kHz", "freq": 2616600, "mode": "USB", "action": "wefax"},
    {"name": "Met Office Northwood WEFAX 4610 kHz", "freq": 4608100, "mode": "USB", "action": "wefax"},
    {"name": "Met Office Northwood WEFAX 8040 kHz", "freq": 8038100, "mode": "USB", "action": "wefax"},
    {"name": "Met Office Northwood WEFAX 11086.5 kHz", "freq": 11084600, "mode": "USB", "action": "wefax"},
    {"name": "ISS SSTV 145.800 MHz", "freq": 145800000, "mode": "NFM", "action": "sstv"},
    {"name": "2 m SSTV 144.500 MHz", "freq": 144500000, "mode": "NFM", "action": "sstv"},
    {"name": "20 m SSTV 14.230 MHz", "freq": 14230000, "mode": "USB", "action": "sstv"},
    {"name": "40 m SSTV 7.165 MHz", "freq": 7165000, "mode": "LSB", "action": "sstv"},
    {"name": "80 m SSTV 3.735 MHz", "freq": 3735000, "mode": "LSB", "action": "sstv"},
    {"name": "Meteor-M LRPT 137.900 MHz", "freq": 137900000, "mode": "DIG25", "action": "lrpt"},
    {"name": "Meteor-M LRPT 137.100 MHz", "freq": 137100000, "mode": "DIG25", "action": "lrpt"},
    {"name": "GOES HRIT 1694.1 MHz (needs dish + LNA)", "freq": 1694100000, "mode": "DIG25", "action": "goes"},
]


def _clean(job):
    j = {
        "id": str(job.get("id") or secrets.token_hex(3)),
        "name": str(job.get("name") or "Capture")[:60],
        "freq": int(job["freq"]),
        "mode": str(job.get("mode") or "NFM"),
        "action": job.get("action") if job.get("action") in ACTIONS else "listen",
        "start": str(job.get("start") or "12:00")[:5],
        "duration": max(1, min(24 * 60, int(float(job.get("duration") or 10)))),
        "days": sorted({int(d) for d in job.get("days", []) if 0 <= int(d) <= 6}),
        "date": str(job.get("date") or "")[:10],
        "priority": max(0, min(2, int(job.get("priority", 1)))),
        "enabled": bool(job.get("enabled", True)),
    }
    dt.datetime.strptime(j["start"], "%H:%M")
    if j["date"]:
        dt.date.fromisoformat(j["date"])
    return j


class Scheduler:
    def __init__(self, radio, settings):
        self.radio = radio
        self.s = settings
        self.running = None          # {"id", "name", "until", "occurrence"}
        self.done = set()            # occurrences already run ("id@YYYY-MM-DDTHH:MM")
        self.lock = threading.Lock()
        self.alive = True
        self.sats = SatPlanner(settings, notify=lambda: self._emit())
        self._tle_check = 0.0
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.alive = False

    # ------------------------------------------------------------------ jobs
    def jobs(self):
        return list(self.s.get("schedule", []) or [])

    def save_job(self, job):
        j = _clean(job)
        with self.lock:
            lst = [x for x in self.jobs() if x.get("id") != j["id"]]
            lst.append(j)
            lst.sort(key=lambda x: (x["date"] or "", x["start"]))
            self.s["schedule"] = lst
            self.s.save()
        if j["enabled"]:
            mine = [c for c in self.conflicts() if j["id"] in (c["a_id"], c["b_id"])]
            if mine:
                self.radio._emit("event", {"level": "warn", "text": f"Schedule clash: {mine[0]['text']}"
                                           + (f" (+{len(mine) - 1} more clash{'es' if len(mine) > 2 else ''})" if len(mine) > 1 else "")})
        self._emit()
        return j

    def delete_job(self, jid):
        with self.lock:
            self.s["schedule"] = [x for x in self.jobs() if x.get("id") != jid]
            self.s.save()
        if self.running and self.running["id"] == jid:
            self.stop_job()
        self._emit()

    # ------------------------------------------------------------------ timing
    def _occurrence(self, job, now):
        """Start datetime of the occurrence of `job` that covers `now`, else None."""
        hh, mm = map(int, job["start"].split(":"))
        for back in (0, 1):                     # today's, or yesterday's that runs past midnight
            day = now.date() - dt.timedelta(days=back)
            if job["date"] and day.isoformat() != job["date"]:
                continue
            if job["days"] and day.weekday() not in job["days"]:
                continue
            st = dt.datetime.combine(day, dt.time(hh, mm))
            if st <= now < st + dt.timedelta(minutes=job["duration"]):
                return st
        return None

    def next_runs(self, now=None, limit=8):
        now = now or dt.datetime.now()
        out = []
        for job in self.jobs():
            if not job.get("enabled", True):
                continue
            hh, mm = map(int, job["start"].split(":"))
            for k in range(0, 8):
                day = now.date() + dt.timedelta(days=k)
                if job["date"] and day.isoformat() != job["date"]:
                    continue
                if job["days"] and day.weekday() not in job["days"]:
                    continue
                st = dt.datetime.combine(day, dt.time(hh, mm))
                if st + dt.timedelta(minutes=job["duration"]) > now:
                    out.append({"id": job["id"], "name": job["name"], "at": st.strftime("%a %d %b %H:%M")})
                    out[-1]["_t"] = st
                    break
        out.sort(key=lambda x: x["_t"])
        for o in out:
            o.pop("_t")
        return out[:limit]

    def _spans(self, job, start, end):
        """Every (start, end) datetime pair of `job` that overlaps the window start..end."""
        hh, mm = map(int, job["start"].split(":"))
        out = []
        for k in range(-1, (end.date() - start.date()).days + 1):
            day = start.date() + dt.timedelta(days=k)
            if job["date"] and day.isoformat() != job["date"]:
                continue
            if job["days"] and day.weekday() not in job["days"]:
                continue
            st = dt.datetime.combine(day, dt.time(hh, mm))
            en = st + dt.timedelta(minutes=job["duration"])
            if en > start and st < end:
                out.append((st, en))
        return out

    def conflicts(self, now=None):
        """Captures whose times overlap, so only one of them can run. Looks CLASH_DAYS ahead for your own
        captures; satellite passes are only predicted about two days ahead."""
        now = now or dt.datetime.now()
        end = now + dt.timedelta(days=CLASH_DAYS)
        items = []
        for job in self.jobs():
            if not job.get("enabled", True):
                continue
            for st, en in self._spans(job, now, end):
                items.append({"id": job["id"], "name": job["name"], "st": st, "en": en, "prio": job.get("priority", 1)})
        try:
            passes = self.sats.upcoming(now.timestamp(), limit=400) if self.sats.observer() else []
        except Exception:
            passes = []
        for p in passes:
            if p.get("geo"):
                continue                                 # a geostationary capture is continuous: never counted as a clash
            d = SATELLITES[p["sat"]]
            items.append({"id": f"sat:{p['sat']}:{int(p['aos'])}", "name": f"{d['name']} pass",
                          "st": dt.datetime.fromtimestamp(p["aos"] - d["lead"]), "en": dt.datetime.fromtimestamp(p["los"] + d["tail"]),
                          "prio": 2})
        items.sort(key=lambda x: x["st"])
        pairs = {}
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if b["st"] >= a["en"]:
                    break
                if a["id"] == b["id"]:
                    continue
                key = (a["id"].split(":")[0] == "sat" and a["id"].rsplit(":", 1)[0] or a["id"],
                       b["id"].split(":")[0] == "sat" and b["id"].rsplit(":", 1)[0] or b["id"])
                lo, hi = max(a["st"], b["st"]), min(a["en"], b["en"])
                mins = max(1, int((hi - lo).total_seconds() // 60))
                if a["prio"] > b["prio"]:
                    rule = f"{a['name']} (higher priority) takes over; {b['name']} is cut short"
                elif b["prio"] > a["prio"]:
                    rule = f"{b['name']} (higher priority) takes over; {a['name']} is cut short"
                else:
                    rule = f"{a['name']} runs first; {b['name']} starts late or is missed"
                if key in pairs:
                    pairs[key]["more"] += 1
                    continue
                pairs[key] = {"a_id": a["id"], "b_id": b["id"], "a": a["name"], "b": b["name"], "more": 0,
                              "at": lo.strftime("%a %d %b %H:%M"), "mins": mins, "rule": rule}
        out = []
        for c in pairs.values():
            c["text"] = (f"{c['a']} and {c['b']} overlap by {c['mins']} min on {c['at']} - {c['rule']}"
                         + (f" (and {c['more']} more time{'s' if c['more'] > 1 else ''})" if c["more"] else ""))
            out.append(c)
        return out

    def _best(self, now):
        """The capture that should be running now among those that are due: highest priority, then earliest start."""
        cands = []
        due = self.sats.due(now.timestamp())
        if due:
            sid, p, key, job, until = due
            cands.append({"prio": 0 if p.get("geo") else 2, "start": p["aos"], "key": key, "job": job,
                          "until": dt.datetime.fromtimestamp(until), "sat": True})
        for job in self.jobs():
            if not job.get("enabled", True):
                continue
            st = self._occurrence(job, now)
            if st is None:
                continue
            key = f"{job['id']}@{st.isoformat(timespec='minutes')}"
            if key in self.done:
                continue
            cands.append({"prio": job.get("priority", 1), "start": st.timestamp(), "key": key, "job": job,
                          "until": st + dt.timedelta(minutes=job["duration"]), "sat": False})
        if not cands:
            return None
        return sorted(cands, key=lambda c: (-c["prio"], c["start"]))[0]

    def _start(self, c):
        if c["sat"]:
            self.sats.done.add(c["key"])
        else:
            self.done.add(c["key"])
        self._begin(c["job"], c["until"], c["key"], c["prio"])
        if not c["sat"] and c["job"].get("date"):    # one-off jobs switch themselves off afterwards
            self.save_job(dict(c["job"], enabled=False))

    def _preempt(self, c):
        old = self.running
        self.stop_job(f"cut short by higher priority capture: {c['job']['name']}")
        if old.get("occurrence"):                        # let it carry on afterwards if time is left
            (self.sats.done if str(old["occurrence"]).startswith("sat:") else self.done).discard(old["occurrence"])
            oj = old.get("job")
            if oj and oj.get("date") and not str(old["occurrence"]).startswith("sat:"):
                self.save_job(dict(oj, enabled=True))
        self._start(c)

    def _loop(self):
        while self.alive:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler")
            time.sleep(2 if self.running else 5)

    def tick(self, now=None):
        now = now or dt.datetime.now()
        if self.running:
            if now >= self.running["until"]:
                self.stop_job()
                return
            if str(self.running.get("occurrence", "")).startswith("sat:"):
                sid = self.running["occurrence"].split(":")[1]
                self.radio.retune_job(self.sats.tuned_freq(sid, now.timestamp()))   # follow the Doppler shift
            best = self._best(now)
            if best and best["prio"] > self.running.get("prio", 1):
                self._preempt(best)
            return
        if self._tle_check < time.time() - 1800:           # keep orbit data fresh for the satellites that are on
            self._tle_check = time.time()
            self.sats.refresh_tles()
        best = self._best(now)
        if best:
            self._start(best)

    # ------------------------------------------------------------------ run / stop
    def run_now(self, jid):
        job = next((j for j in self.jobs() if j["id"] == jid), None)
        if not job:
            return
        if self.running:
            self.stop_job()
        self._begin(job, dt.datetime.now() + dt.timedelta(minutes=job["duration"]), f"{jid}@now", 3)   # started by hand: nothing scheduled takes it over

    def _begin(self, job, until, key, prio=None):
        self.running = {"id": job["id"], "name": job["name"], "until": until, "occurrence": key, "job": job,
                        "prio": job.get("priority", 1) if prio is None else prio,
                        "action": job["action"], "freq": job["freq"]}
        self.radio.begin_job(job)
        self.radio._emit("event", {"level": "info", "text": f"Scheduled capture started: {job['name']} "
                                                            f"until {until.strftime('%H:%M')}"})
        self._emit()

    def stop_job(self, why=None):
        if not self.running:
            return
        name = self.running["name"]
        self.running = None
        self.radio.end_job()
        self.radio._emit("event", {"level": "warn" if why else "info",
                                   "text": f"Scheduled capture {'stopped' if why else 'finished'}: {name}" + (f" ({why})" if why else "")})
        self._emit()

    # ------------------------------------------------------------------ GUI
    def status(self):
        r = None
        if self.running:
            r = {k: self.running[k] for k in ("id", "name", "action", "freq")}
            r["until"] = self.running["until"].strftime("%H:%M")
        return {"jobs": self.jobs(), "running": r, "next": self.next_runs(),
                "presets": PRESETS, "actions": ACTIONS, "conflicts": self.conflicts(), "now": dt.datetime.now().strftime("%H:%M"),
                "sats": self.sats.status()}

    def _emit(self):
        self.radio._emit("schedule", self.status())
