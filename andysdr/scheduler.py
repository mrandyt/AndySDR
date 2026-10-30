"""Capture scheduler: tune a frequency, run a decoder (WEFAX, SSTV, LRPT, ...) or record,
for a set time, then put the radio back the way it was.

A job:
  {"id": "a1b2c3", "name": "Northwood surface chart", "freq": 8038100, "mode": "USB",
   "action": "wefax" | "sstv" | "lrpt" | "pager" | "data" | "dsd" | "record" | "listen",
   "start": "HH:MM" (local time on this PC), "duration": minutes,
   "days": [0..6] (Mon=0; empty = every day), "date": "YYYY-MM-DD" (optional: once only),
   "enabled": true}
"""
import datetime as dt
import logging
import secrets
import threading
import time

log = logging.getLogger("andysdr.scheduler")

ACTIONS = {
    "wefax": "Weather fax (WEFAX)", "sstv": "SSTV pictures", "lrpt": "Meteor-M LRPT pass",
    "record": "Record audio", "listen": "Just tune & listen", "pager": "Pagers", "data": "APRS / data",
    "dsd": "Digital voice",
}

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

    def _loop(self):
        while self.alive:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler")
            time.sleep(5)

    def tick(self, now=None):
        now = now or dt.datetime.now()
        if self.running:
            if now >= self.running["until"]:
                self.stop_job()
            return
        for job in self.jobs():
            if not job.get("enabled", True):
                continue
            st = self._occurrence(job, now)
            if st is None:
                continue
            key = f"{job['id']}@{st.isoformat(timespec='minutes')}"
            if key in self.done:
                continue
            self.done.add(key)
            until = st + dt.timedelta(minutes=job["duration"])
            self._begin(job, until, key)
            if job.get("date"):          # one-off jobs switch themselves off afterwards
                job2 = dict(job, enabled=False)
                self.save_job(job2)
            return

    # ------------------------------------------------------------------ run / stop
    def run_now(self, jid):
        job = next((j for j in self.jobs() if j["id"] == jid), None)
        if not job:
            return
        if self.running:
            self.stop_job()
        self._begin(job, dt.datetime.now() + dt.timedelta(minutes=job["duration"]), f"{jid}@now")

    def _begin(self, job, until, key):
        self.running = {"id": job["id"], "name": job["name"], "until": until, "occurrence": key,
                        "action": job["action"], "freq": job["freq"]}
        self.radio.begin_job(job)
        self.radio._emit("event", {"level": "info", "text": f"Scheduled capture started: {job['name']} "
                                                            f"until {until.strftime('%H:%M')}"})
        self._emit()

    def stop_job(self):
        if not self.running:
            return
        name = self.running["name"]
        self.running = None
        self.radio.end_job()
        self.radio._emit("event", {"level": "info", "text": f"Scheduled capture finished: {name}"})
        self._emit()

    # ------------------------------------------------------------------ GUI
    def status(self):
        r = None
        if self.running:
            r = {k: self.running[k] for k in ("id", "name", "action", "freq")}
            r["until"] = self.running["until"].strftime("%H:%M")
        return {"jobs": self.jobs(), "running": r, "next": self.next_runs(),
                "presets": PRESETS, "actions": ACTIONS, "now": dt.datetime.now().strftime("%H:%M")}

    def _emit(self):
        self.radio._emit("schedule", self.status())
