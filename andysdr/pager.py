"""Pager messages: turns multimon-ng POCSAG / FLEX output into a tidy message list.

Every message is kept in memory (newest 2000), written to a daily CSV log in
%APPDATA%\\AndySDR\\pager\\pager_YYYY-MM-DD.csv and pushed to every screen as a
"pager" event. Capcode names (aliases) and highlighted capcodes live in settings.json.
"""
import collections
import csv
import io
import logging
import os
import re
import threading
import time

from .config import data_dir

log = logging.getLogger("andysdr.pager")

MAX_KEEP = 2000
DEDUP_SECONDS = 60
CSV_FIELDS = ["date", "time", "freq_mhz", "protocol", "capcode", "name", "function", "type", "message", "channel"]

# optional prefixes multimon-ng adds: "--label X" and "--timestamp" (plain or ISO 8601)
_TS = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?:\s*)")
_POCSAG = re.compile(
    r"^POCSAG(?P<rate>\d+):\s*Address:\s*(?P<addr>\d+|-)\s+Function:\s*(?P<func>\d|-)\s*"
    r"(?:(?P<kind>Alpha|Numeric|Skyper):\s*(?P<text>.*))?$")
# FLEX|2026-10-30 16:40:01|1600/2/K/A|05.083|001234567[ 001234568 ...]|ALN|text
_FLEX_PIPE = re.compile(
    r"^FLEX\|(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\|)?(?P<baud>\d+)/(?P<lev>\d+)[^|]*\|[\d.]+\|"
    r"\[?(?P<cap>\d+)\]?(?P<group>(?: \d+)*)\|(?P<kind>ALN|NUM|TON|UNK|BIN|SEC[:A-Z]*)\s?\|?(?P<text>.*)$")
# FLEX: 2026-10-30 16:40:01 1600/2/K/A 05.083 [001234567] ALN text
_FLEX_OLD = re.compile(
    r"^FLEX:\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+(?P<baud>\d+)/(?P<lev>\d+)\S*\s+[\d.]+\s+"
    r"\[(?P<cap>\d+)\]\s+(?P<kind>ALN|NUM|TON|UNK|BIN)\s?(?P<text>.*)$")
# FLEX_NEXT|ts|1600/2|05.083.A|0001234567[ ...]|S|ALN|K.0/0.N1|text
_FLEX_NEXT = re.compile(
    r"^FLEX_NEXT\|[^|]*\|(?P<baud>\d+)/(?P<lev>\d+)\|[^|]*\|(?P<cap>\d+)(?P<group>(?: \d+)*)\|[A-Z]\|"
    r"(?P<kind>[A-Z:/]+)\|(?P<flags>[^|]*)\|(?P<text>.*)$")
_CTRL = re.compile(r"(?:<(?:NUL|SOH|STX|ETX|EOT|ENQ|ACK|BEL|BS|LF|VT|FF|CR|SO|SI|DLE|DC[1-4]|NAK|SYN|ETB|CAN|EM|SUB|ESC|FS|GS|RS|US|DEL)>)+")

_KIND = {"Alpha": "Alpha", "Skyper": "Alpha", "Numeric": "Numeric",
         "ALN": "Alpha", "SEC:ALN": "Alpha", "NUM": "Numeric", "SNUM": "Numeric", "NNUM": "Numeric",
         "TON": "Tone", "Tone": "Tone", "UNK": "Other", "BIN": "Other", "HEX": "Other"}


def clean_text(text):
    """Drop multimon-ng's <NUL>/<ETX> style control markers at the ends; turn inner ones into spaces."""
    text = (text or "").strip()
    text = re.sub(r"^" + _CTRL.pattern, "", text)
    text = re.sub(_CTRL.pattern + r"$", "", text)
    text = _CTRL.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def parse_line(line):
    """Return {"proto","capcode","func","type","text"} for a pager line, or None for anything else."""
    line = _TS.sub("", line.strip())
    m = _POCSAG.match(line)
    if m:
        if m["addr"] == "-":
            return None
        kind = m["kind"]
        text = clean_text(m["text"])
        if not kind or not text:
            kind, text = "Tone", ""
        return {"proto": f"POCSAG {m['rate']}", "capcode": m["addr"], "func": m["func"],
                "type": _KIND.get(kind, "Other"), "text": text}
    for rx in (_FLEX_PIPE, _FLEX_NEXT, _FLEX_OLD):
        m = rx.match(line)
        if not m:
            continue
        kind = m["kind"]
        if rx is _FLEX_NEXT and kind in ("SMSG", "FLEXTIME") or kind.startswith("OPR"):
            return None
        if rx is _FLEX_NEXT and ("PARTIAL" in m["flags"]):
            return None
        text = clean_text(m["text"])
        typ = _KIND.get(kind, "Other")
        if typ == "Tone":
            text = ""
        elif not text:
            typ = "Tone"
        cap = m["cap"].lstrip("0") or "0"
        group = " ".join(c.lstrip("0") or "0" for c in (m.groupdict().get("group") or "").split())
        out = {"proto": f"FLEX {m['baud']}", "capcode": cap, "func": "",
               "type": typ, "text": text}
        if group:
            out["group"] = group
        return out
    return None


class PagerStore:
    def __init__(self, settings, emit):
        self.s = settings
        self.emit = emit
        self.messages = collections.deque(maxlen=MAX_KEEP)
        self.next_id = 1
        self._lock = threading.Lock()
        self.dir = os.path.join(data_dir(), "pager")
        os.makedirs(self.dir, exist_ok=True)
        self._load_today()

    # ------------------------------------------------------------------ settings
    def aliases(self):
        return dict(self.s.get("pager_aliases") or {})

    def highlights(self):
        return list(self.s.get("pager_highlight") or [])

    def set_alias(self, capcode, name):
        capcode = str(capcode).strip().lstrip("0") or "0"
        al = self.aliases()
        name = str(name or "").strip()[:40]
        if name:
            al[capcode] = name
        else:
            al.pop(capcode, None)
        self.s["pager_aliases"] = al
        self.s.save()
        self.emit("pager_cfg", self.config())

    def set_highlight(self, capcode, on):
        capcode = str(capcode).strip().lstrip("0") or "0"
        hl = [c for c in self.highlights() if c != capcode]
        if on:
            hl.append(capcode)
        self.s["pager_highlight"] = hl
        self.s.save()
        self.emit("pager_cfg", self.config())

    def config(self):
        return {"aliases": self.aliases(), "highlight": self.highlights(),
                "log_dir": self.dir, "logging": bool(self.s.get("pager_log", True))}

    def snapshot(self, n=1000):
        with self._lock:
            msgs = list(self.messages)[-n:]
        return dict(self.config(), messages=msgs)

    # ------------------------------------------------------------------ messages
    def add_line(self, line, freq=0, channel=""):
        p = parse_line(line)
        if not p:
            return None
        now = time.time()
        with self._lock:
            # the same page is often heard twice (repeat transmissions, or two POCSAG rates at once)
            for old in reversed(self.messages):
                if now - old["ts"] > DEDUP_SECONDS:
                    break
                if (old["capcode"] == p["capcode"] and old["text"] == p["text"]
                        and old["proto"].split()[0] == p["proto"].split()[0]):
                    return None
            msg = dict(p, id=self.next_id, ts=round(now, 1), date=time.strftime("%Y-%m-%d", time.localtime(now)),
                       t=time.strftime("%H:%M:%S", time.localtime(now)), freq=int(freq or 0),
                       channel=str(channel or ""))
            self.next_id += 1
            self.messages.append(msg)
        self._write(msg)
        self.emit("pager", msg)
        return msg

    def clear(self):
        with self._lock:
            self.messages.clear()
        self.emit("pager_all", self.snapshot())

    def _row(self, m, aliases):
        return {"date": m["date"], "time": m["t"], "freq_mhz": f"{m['freq'] / 1e6:.5f}" if m["freq"] else "",
                "protocol": m["proto"], "capcode": m["capcode"], "name": aliases.get(m["capcode"], ""),
                "function": m.get("func", ""), "type": m["type"], "message": m["text"],
                "channel": m.get("channel", "")}

    def _write(self, m):
        if not self.s.get("pager_log", True):
            return
        path = os.path.join(self.dir, f"pager_{m['date']}.csv")
        try:
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if new:
                    w.writeheader()
                w.writerow(self._row(m, self.aliases()))
        except OSError as e:
            log.warning("pager log: %s", e)

    def _load_today(self):
        """Bring back today's messages after a restart."""
        path = os.path.join(self.dir, f"pager_{time.strftime('%Y-%m-%d')}.csv")
        if not os.path.exists(path):
            return
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))[-MAX_KEEP:]
        except (OSError, csv.Error) as e:
            log.warning("pager log read: %s", e)
            return
        for r in rows:
            try:
                ts = time.mktime(time.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M:%S"))
                freq = int(round(float(r["freq_mhz"]) * 1e6)) if r.get("freq_mhz") else 0
            except (ValueError, KeyError, TypeError):
                continue
            self.messages.append({"id": self.next_id, "ts": ts, "date": r["date"], "t": r["time"], "freq": freq,
                                  "proto": r.get("protocol", ""), "capcode": r.get("capcode", ""),
                                  "func": r.get("function", ""), "type": r.get("type", ""),
                                  "text": r.get("message", ""), "channel": r.get("channel", "")})
            self.next_id += 1

    def export_csv(self):
        al = self.aliases()
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
        w.writeheader()
        with self._lock:
            for m in self.messages:
                w.writerow(self._row(m, al))
        return buf.getvalue()
