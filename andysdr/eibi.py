"""EiBi shortwave broadcast schedule (www.eibispace.de): download / import, search, "on air now".

The EiBi file is a semicolon-separated text file, one line per broadcast:
    kHz;Time(UTC);Days;ITU;Station;Lang;Target;Remarks;P;Start;Stop
e.g.  9410;0000-0100;;D;Deutsche Welle;E;NAm;;1;;
It is updated for every broadcasting season (A = late March-October, B = late October-March), e.g. sked-b26.csv.
The file is kept in the AndySDR data folder as eibi.csv.
"""
import csv
import datetime as dt
import io
import logging
import os
import threading
import urllib.request

from .config import data_dir

log = logging.getLogger("andysdr.eibi")
BASE = "http://www.eibispace.de/dx/sked-{code}.csv"
DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def _last_sunday(year, month):
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def season_code(today=None):
    """a26 / b26 ... for the broadcasting season that is running on `today`."""
    t = today or dt.date.today()
    if t >= _last_sunday(t.year, 10):
        return f"b{t.year % 100:02d}"
    if t >= _last_sunday(t.year, 3):
        return f"a{t.year % 100:02d}"
    return f"b{(t.year - 1) % 100:02d}"


def candidate_codes(today=None):
    """Season codes to try, newest first (the file for a new season appears a little late)."""
    t = today or dt.date.today()
    first = season_code(t)
    prev = season_code(dt.date(t.year, t.month, 1) - dt.timedelta(days=40))
    out = [first]
    if prev != first:
        out.append(prev)
    return out


def days_set(text):
    """EiBi 'Days' field -> set of weekdays (Mon=0) or None for every day / unknown (irr, alt ...)."""
    s = (text or "").strip()
    if not s or s.lower() in ("irr", "alt", "daily"):
        return None
    out = set()
    if s.replace(",", "").isdigit():                      # "1234567", "1245" (1 = Monday)
        for ch in s.replace(",", ""):
            if ch in "1234567":
                out.add(int(ch) - 1)
        return out or None
    for part in s.replace(" ", "").split(","):
        if "-" in part:
            a, _, b = part.partition("-")
            if a[:2] in DAYS and b[:2] in DAYS:
                i, j = DAYS.index(a[:2]), DAYS.index(b[:2])
                k = i
                while True:
                    out.add(k)
                    if k == j:
                        break
                    k = (k + 1) % 7
        elif part[:2] in DAYS:
            out.add(DAYS.index(part[:2]))
    return out or None


def _mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[2:4])


def parse(text):
    """EiBi CSV text -> list of dicts (f in kHz).  Unreadable lines are skipped."""
    rows = []
    for rec in csv.reader(io.StringIO(text), delimiter=";"):
        if len(rec) < 5:
            continue
        try:
            f = float(rec[0].strip())
            t = rec[1].strip()
            a, _, b = t.partition("-")
            s0, s1 = _mins(a), _mins(b)
        except (ValueError, IndexError):
            continue                                        # header line / junk
        rec = rec + [""] * (11 - len(rec))
        rows.append({"f": f, "t": t, "s0": s0, "s1": s1, "d": rec[2].strip(), "days": days_set(rec[2]),
                     "itu": rec[3].strip(), "st": rec[4].strip(), "l": rec[5].strip(), "tg": rec[6].strip(),
                     "r": rec[7].strip()})
    return rows


def on_air(row, now_utc):
    """True if the broadcast is on at `now_utc` (a datetime in UTC)."""
    m = now_utc.hour * 60 + now_utc.minute
    s0, s1, days = row["s0"], row["s1"], row["days"]
    if s0 <= s1:                                          # 0000-0100, 0000-2400
        return s0 <= m < s1 and (days is None or now_utc.weekday() in days)
    if m >= s0:                                           # 2200-0200: late part belongs to the start day
        return days is None or now_utc.weekday() in days
    return m < s1 and (days is None or (now_utc.weekday() - 1) % 7 in days)


class Eibi:
    def __init__(self):
        self.path = os.path.join(data_dir(), "eibi.csv")
        self.rows = []
        self.source = ""
        self.lock = threading.Lock()
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="latin-1") as f:
                text = f.read()
        except OSError:
            return
        self._set(parse(text))

    def _set(self, rows):
        rows.sort(key=lambda r: (r["f"], r["s0"]))
        with self.lock:
            self.rows = rows

    def status(self):
        mt = os.path.getmtime(self.path) if os.path.exists(self.path) else 0
        return {"count": len(self.rows), "updated": mt, "source": self.source}

    def import_text(self, text):
        rows = parse(text)
        if len(rows) < 50:
            raise ValueError("That does not look like an EiBi schedule file (sked-b26.csv style, semicolon separated).")
        with open(self.path, "w", encoding="latin-1", errors="replace", newline="") as f:
            f.write(text)
        self._set(rows)
        return len(rows)

    def download(self):
        """Fetch the current season's file from eibispace.de (runs in the caller's thread)."""
        last = None
        for code in candidate_codes():
            url = BASE.format(code=code)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AndySDR (EiBi import)"})
                with urllib.request.urlopen(req, timeout=40) as r:
                    text = r.read().decode("latin-1")
                n = self.import_text(text)
                self.source = url
                return code, n
            except Exception as e:                       # try the previous season next
                last = e
                log.warning("EiBi download %s failed: %s", url, e)
        raise RuntimeError(f"Could not download the EiBi schedule ({last}). Download sked-xx.csv from eibispace.de "
                           "yourself and use Import file.")

    def query(self, q="", lang="", fmin=0, fmax=0, now_only=False, limit=400, now_utc=None):
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        q = (q or "").strip().lower()
        lang = (lang or "").strip().lower()
        out, total = [], 0
        with self.lock:
            rows = self.rows
        for r in rows:
            if fmin and r["f"] < fmin or fmax and r["f"] > fmax:
                continue
            if lang and r["l"].lower() != lang:
                continue
            if q and q not in (r["st"] + " " + r["l"] + " " + r["tg"] + " " + r["r"] + " " + str(r["f"])).lower():
                continue
            now = on_air(r, now_utc)
            if now_only and not now:
                continue
            total += 1
            if len(out) < limit:
                out.append({k: r[k] for k in ("f", "t", "d", "itu", "st", "l", "tg", "r")}
                           | {"now": now, "dl": sorted(r["days"]) if r["days"] else []})
        return {"rows": out, "total": total, "utc": now_utc.strftime("%H:%M")}
