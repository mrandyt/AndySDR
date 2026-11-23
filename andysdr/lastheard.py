"""DMR / P25 / NXDN "Last heard" list.

DSD-FME prints a line for every voice burst it decodes, for example

    SLOT 1 TGT=235 SRC=2356184 FLCO=0x00 FID=0x00 SVC=0x00 Group Call          (DMR)
    Sync: +P25p1 ... Group Voice Channel User - Group 1234 Source 5678         (P25)
    Group Call - Full Duplex - Src=1234 - Dst/TG=567                           (NXDN)

This module turns those lines into one row per transmission - who (callsign and name from the RadioID list), to which
talkgroup (with its name), when, on which frequency and for how long - newest first, and pushes the rows to every screen
as "heard" events.  Lines that are only trunking signalling (call alerts, channel grants) are ignored.
"""
import collections
import csv
import io
import logging
import re
import threading
import time

log = logging.getLogger("andysdr.lastheard")

MAX_ROWS = 200
SAME_CALL_GAP_S = 4.0        # voice bursts closer together than this belong to the same transmission
PUSH_EVERY_S = 1.0           # a running transmission is re-sent to the screens about once a second

RE_SYNC = re.compile(r"Sync:\s*[+-]?\s*(DMR|P25|NXDN|dPMR|YSF|D-STAR|M17|X2-TDMA|ProVoice)", re.I)
RE_CC = re.compile(r"Color Code\s*=\s*(\d+)", re.I)
RE_DMR = re.compile(r"\bSLOT\s*(\d)\b.*?\bTGT\s*=\s*(\d+)\s+SRC\s*=\s*(\d+)(.*)$", re.I)
RE_FLCO = re.compile(r"FLCO\s*=\s*0x([0-9A-F]+)", re.I)
RE_P25_GRP = re.compile(r"\bGroup\s+(\d+)\s+Source\s+(\d+)", re.I)
RE_P25_UU = re.compile(r"\bTarget\s+(\d+)\s+Source\s+(\d+)", re.I)
RE_NXDN = re.compile(r"\bSrc\s*=\s*(\d+)\s*-\s*Dst/TG\s*=\s*(\d+)", re.I)
RE_M17 = re.compile(r"\bCAN:\s*(\d+)\s+DST:\s*(\S+)\s+SRC:\s*(\S+)", re.I)
RE_NAC = re.compile(r"\bNAC(?:/CC)?:\s*([0-9A-F]+)", re.I)

PRIVATE_FLCO = {0x03, 0x07, 0x13, 0x23}      # unit-to-unit and Capacity Plus private calls


def parse(line, ctx):
    """One decoder line -> dict(proto, slot, src, tgt, kind, emerg, enc) or None.  ctx is a dict remembering the last
    "Sync:" protocol and colour code / NAC so that a later line can say which system it belongs to."""
    m = RE_SYNC.search(line)
    if m:
        ctx["proto"] = {"D-STAR": "D-STAR"}.get(m.group(1).upper(), m.group(1).upper() if m.group(1).upper() != "DPMR" else "dPMR")
    m = RE_CC.search(line)
    if m:
        ctx["cc"] = m.group(1)
    m = RE_NAC.search(line)
    if m:
        ctx["cc"] = m.group(1)

    low = line.lower()
    m = RE_M17.search(line)
    if m:                                                  # M17 carries the callsigns in the stream itself
        dst, src = m.group(2).upper(), m.group(3).upper()
        if src in ("UNKNOWN", "RESERVED") or src.startswith(("FFFF", "000000")):
            return None
        ctx["cc"] = "CAN " + m.group(1)
        return {"proto": "M17", "slot": 0, "src": src, "tgt": dst,
                "kind": "group" if dst in ("BROADCAST", "ALL") or dst.startswith("#") else "private",
                "emerg": False, "enc": "encrypted" in low or "scrambl" in low}
    m = RE_DMR.search(line)
    if m:
        slot, tgt, src, tail = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        t = tail.lower()
        if "alert" in t or "pseudo" in t:
            return None                                    # trunking signalling, not somebody talking
        if "data" in t or "gps" in t or "sms" in t:
            return None
        kind = "group"
        fm = RE_FLCO.search(tail)
        if fm and int(fm.group(1), 16) in PRIVATE_FLCO:
            kind = "private"
        if "private" in t:
            kind = "private"
        elif "group" in t:
            kind = "group"
        if src <= 0 or tgt <= 0:
            return None
        return {"proto": "DMR", "slot": slot, "src": src, "tgt": tgt, "kind": kind,
                "emerg": "emergency" in t, "enc": "encrypted" in t}

    m = RE_NXDN.search(line)
    if m:
        src, tgt = int(m.group(1)), int(m.group(2))
        if src <= 0 or tgt <= 0:
            return None
        return {"proto": "NXDN", "slot": 0, "src": src, "tgt": tgt,
                "kind": "private" if "individual" in low else "group",
                "emerg": "emergency" in low, "enc": False}

    if "voice channel user" in low:
        m = RE_P25_GRP.search(line)
        kind = "group"
        if not m:
            m = RE_P25_UU.search(line)
            kind = "private"
        if m:
            tgt, src = int(m.group(1)), int(m.group(2))
            if src > 0 and tgt > 0:
                return {"proto": "P25", "slot": 0, "src": src, "tgt": tgt, "kind": kind,
                        "emerg": "emergency" in low, "enc": "encrypted" in low}
    return None


class LastHeard:
    def __init__(self, settings, emit, db=None, freq_fn=None, on_new=None, clock=time.time, timers=True):
        self.s = settings
        self.emit = emit
        self.db = db                      # DmrDb: lookup(id) / tg_name(tg)
        self.freq_fn = freq_fn or (lambda: 0)
        self.on_new = on_new              # on_new(row, text) - once per new transmission (the DECODERS log shows it)
        self.clock = clock
        self.timers = timers
        self._lock = threading.Lock()
        self.rows = collections.deque(maxlen=MAX_ROWS)
        self.active = {}                  # (proto, slot) -> the row being added to
        self.ctx = {}
        self.seq = 1
        self._dirty = set()
        self._timer = None

    # ------------------------------------------------------------------ input
    def feed(self, line):
        """Called with every line DSD-FME prints (from its reader thread).  Never raises."""
        try:
            hit = parse(line, self.ctx)
            if hit:
                self._heard(hit)
        except Exception as e:      # noqa: BLE001
            log.debug("last heard: %s", e)

    def _heard(self, h):
        now = self.clock()
        key = (h["proto"], h["slot"])
        new_row = push_row = None
        with self._lock:
            row = self.active.get(key)
            if row and row["src"] == h["src"] and row["tgt"] == h["tgt"] and now - row["last"] <= SAME_CALL_GAP_S:
                row["last"] = now
                row["n"] += 1
                row["emerg"] = row["emerg"] or h["emerg"]
                row["enc"] = row["enc"] or h["enc"]
                if now - row["_sent"] >= PUSH_EVERY_S:        # a running transmission is re-sent about once a second
                    row["_sent"] = now
                    self._dirty.discard(row["id"])
                    push_row = row
                else:                                          # ... and the final length goes out a moment later
                    self._dirty.add(row["id"])
                    if self.timers and self._timer is None:
                        self._timer = threading.Timer(PUSH_EVERY_S + 0.2, self.flush)
                        self._timer.daemon = True
                        self._timer.start()
            else:
                if row:                                        # the previous transmission on this slot is over
                    self._dirty.add(row["id"])
                new_row = self._new_row(h, now)
                new_row["_sent"] = now
                self.active[key] = new_row
                self.rows.appendleft(new_row)
        if push_row:
            self._push(push_row)
        if new_row:
            self._push(new_row)
            if self.on_new:
                try:
                    self.on_new(new_row, describe(new_row))
                except Exception:      # noqa: BLE001
                    pass

    def _new_row(self, h, now):
        text = isinstance(h["src"], str)                       # M17: the callsign itself
        who = None if text or not self.db else self.db.lookup(h["src"])
        tgname = ""
        if h["kind"] == "group" and self.db and not text:
            tgname = self.db.tg_name(h["tgt"])
        row = {"id": self.seq, "proto": h["proto"], "slot": h["slot"], "cc": self.ctx.get("cc", ""),
               "src": h["src"], "call": h["src"] if text else (who["call"] if who else ""),
               "name": who["name"] if who else "", "place": who["place"] if who else "",
               "tgt": h["tgt"], "tgname": tgname, "kind": h["kind"], "emerg": h["emerg"], "enc": h["enc"],
               "freq": int(self.freq_fn() or 0), "first": round(now, 1), "last": now, "n": 1,
               "t": time.strftime("%H:%M:%S", time.localtime(now))}
        self.seq += 1
        return row

    # ------------------------------------------------------------------ pushing to the screens
    def _public(self, row):
        out = {k: v for k, v in row.items() if not k.startswith("_")}
        out["secs"] = max(0, int(round(row["last"] - row["first"])))
        out["live"] = (self.clock() - row["last"]) <= SAME_CALL_GAP_S
        return out

    def _push(self, row):
        try:
            self.emit("heard", self._public(row))
        except Exception:      # noqa: BLE001
            pass

    def flush(self):
        """Sends every transmission that changed since it was last sent (also used by the timer)."""
        with self._lock:
            self._timer = None
            rows = [r for r in self.rows if r["id"] in self._dirty]
            self._dirty.clear()
            now = self.clock()
            for r in rows:
                r["_sent"] = now
        for r in rows:
            self._push(r)

    # ------------------------------------------------------------------ for new screens / commands
    def snapshot(self):
        with self._lock:
            return [self._public(r) for r in self.rows]          # newest first

    CSV_FIELDS = ("date", "time", "protocol", "slot", "system", "source_id", "callsign", "name", "place", "call_type",
                  "target", "talkgroup_name", "frequency_mhz", "seconds", "emergency", "encrypted")

    def export_csv(self):
        """Everything in the list as CSV, oldest first (opens in Excel).  Text that came off the air is made safe against
        spreadsheet formulas, because a station can send any callsign it likes."""
        def safe(v):
            v = "" if v is None else str(v)
            return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(self.CSV_FIELDS)
        for r in reversed(self.snapshot()):
            lt = time.localtime(r["first"])
            w.writerow([time.strftime("%Y-%m-%d", lt), time.strftime("%H:%M:%S", lt), r["proto"], r["slot"] or "",
                        safe(r["cc"]), safe("" if isinstance(r["src"], str) else r["src"]),
                        safe(r["call"]), safe(r["name"]), safe(r["place"]),
                        r["kind"], safe(r["tgt"]), safe(r["tgname"]),
                        f"{r['freq'] / 1e6:.4f}" if r["freq"] else "", r["secs"],
                        "yes" if r["emerg"] else "", "yes" if r["enc"] else ""])
        return buf.getvalue()

    def clear(self):
        with self._lock:
            self.rows.clear()
            self.active.clear()
            self._dirty.clear()
        try:
            self.emit("heard_all", [])
        except Exception:      # noqa: BLE001
            pass

    def relookup(self):
        """After the callsign list has been (re)downloaded: fill in names for rows that were heard before it arrived."""
        if not self.db:
            return 0
        changed = []
        with self._lock:
            for r in self.rows:
                if not r["call"] and not isinstance(r["src"], str):
                    who = self.db.lookup(r["src"])
                    if who:
                        r["call"], r["name"], r["place"] = who["call"], who["name"], who["place"]
                        changed.append(r)
                if r["kind"] == "group" and not r["tgname"] and not isinstance(r["tgt"], str):
                    r["tgname"] = self.db.tg_name(r["tgt"])
        for r in changed:
            self._push(r)
        return len(changed)


def describe(row):
    """One readable line, e.g. 'Heard: M7FEP Andy (2356184) -> TG 235 United Kingdom  [DMR slot 1]'."""
    who = row["call"] or "ID"
    if row["name"]:
        who += " " + row["name"]
    if not isinstance(row["src"], str):
        who += f" ({row['src']})"
    if row["proto"] == "M17":
        to = str(row["tgt"]).title() if row["tgt"] in ("BROADCAST", "ALL") else str(row["tgt"])
    elif row["kind"] == "private":
        to = f"private call to {row['tgt']}"
    else:
        to = f"TG {row['tgt']}" + (f" {row['tgname']}" if row["tgname"] else "")
    extra = " EMERGENCY" if row["emerg"] else ""
    extra += " (encrypted)" if row["enc"] else ""
    slot = f" slot {row['slot']}" if row["slot"] else ""
    return f"Heard: {who} -> {to}{extra}  [{row['proto']}{slot}]"
