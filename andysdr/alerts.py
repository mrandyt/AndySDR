"""Alerts for the phone / tablet app.

The radio reports everything that happens (pages, aircraft, scanner events).  This module picks out the few things
worth interrupting someone for, keeps the latest 100 in a small queue and hands them out through /api/alerts, which the
Android app polls from a background service and turns into notifications.

  pager      a page for a capcode you have starred in PAGER MESSAGES
  emergency  an aircraft squawking 7500 / 7600 / 7700
  priority   activity on the priority channel
  capture    a scheduled capture (satellite pass, WEFAX, SSTV ...) has started

Each kind can be switched off with the settings alert_pager / alert_emergency / alert_priority / alert_capture.
"""
import threading
import time
from collections import deque

KINDS = ("pager", "emergency", "priority", "capture")
SQUAWK = {"7500": "hijack", "7600": "radio failure", "7700": "general emergency"}
EMERGENCY_REPEAT_S = 30 * 60     # the same aircraft with the same code is announced again after half an hour
SAME_TEXT_S = 60                 # an identical alert inside this time is ignored


class Alerts:
    def __init__(self, settings, clock=time.time):
        self.s = settings
        self.clock = clock
        self._lock = threading.Lock()
        self._items = deque(maxlen=100)
        self._seq = 0
        self._emerg = {}         # (icao, squawk) -> when it was announced
        self._recent = {}        # (kind, text) -> when

    def enabled(self, kind):
        v = self.s.get("alert_" + kind, True)
        return True if v is None else bool(v)

    # ------------------------------------------------------------------ queue
    def add(self, kind, title, text):
        now = self.clock()
        key = (kind, title, text)
        with self._lock:
            if now - self._recent.get(key, -1e9) < SAME_TEXT_S:
                return None
            self._recent[key] = now
            if len(self._recent) > 300:
                self._recent = {k: t for k, t in self._recent.items() if now - t < SAME_TEXT_S}
            self._seq += 1
            item = {"id": self._seq, "t": round(now, 1), "kind": kind, "title": str(title)[:80], "text": str(text)[:300]}
            self._items.append(item)
            return item

    def since(self, n):
        """{"last": newest id, "alerts": [...]} - everything after id n.  n < 0 only reports the newest id."""
        with self._lock:
            last = self._seq
            items = [] if n < 0 else [dict(i) for i in self._items if i["id"] > n]
        return {"last": last, "alerts": items}

    # ------------------------------------------------------------------ what the radio reports
    def feed(self, kind, payload):
        """Called for every radio event (from the radio threads): cheap for the busy ones."""
        if kind == "pager":
            self._pager(payload)
        elif kind == "adsb":
            self._adsb(payload)
        elif kind == "event":
            self._event(payload)

    def _pager(self, m):
        if not self.enabled("pager") or not isinstance(m, dict):
            return
        cap = str(m.get("capcode", ""))
        if cap not in [str(x) for x in (self.s.get("pager_highlight") or [])]:
            return
        alias = (self.s.get("pager_aliases") or {}).get(cap, "")
        name = f"{alias} ({cap})" if alias else cap
        text = (m.get("text") or "").strip() or f"{m.get('type', 'Page')} – no text"
        self.add("pager", "Pager " + name, text)

    def _adsb(self, d):
        if not self.enabled("emergency") or not isinstance(d, dict):
            return
        now = self.clock()
        for a in d.get("aircraft") or []:
            sq = str(a.get("squawk") or "")
            if sq not in SQUAWK:
                continue
            key = (a.get("icao"), sq)
            if now - self._emerg.get(key, -1e9) < EMERGENCY_REPEAT_S:
                continue
            self._emerg[key] = now
            who = (a.get("flight") or "").strip() or str(a.get("icao") or "aircraft").upper()
            alt = a.get("alt")
            extra = f", {alt} ft" if alt not in (None, "") else ""
            self.add("emergency", f"Aircraft squawk {sq}", f"{who} – {SQUAWK[sq]}{extra}")

    def _event(self, e):
        if not isinstance(e, dict):
            return
        text = str(e.get("text") or "")
        if text.startswith("Priority channel active:") and self.enabled("priority"):
            self.add("priority", "Priority channel", text.split(":", 1)[1].strip())
        elif text.startswith("Scheduled capture started:") and self.enabled("capture"):
            self.add("capture", "Capture started", text.split(":", 1)[1].strip())
