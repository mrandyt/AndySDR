"""Alerts for the tablet app: what is worth a notification, what is not, and the feed the app polls."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio, json
from andysdr.alerts import Alerts

fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

now = [1000.0]
S = {"pager_highlight": ["1234567"], "pager_aliases": {"1234567": "Duty crew"}}
class St(dict):
    pass
a = Alerts(St(S), clock=lambda: now[0])

# --- pager
a.feed("pager", {"capcode": "7654321", "text": "not starred"})
check("an unstarred capcode is ignored", a.since(0)["alerts"] == [])
a.feed("pager", {"capcode": "1234567", "text": "Shout 4 - house fire", "type": "Text"})
r = a.since(0)["alerts"]
check("a starred capcode alerts", len(r) == 1 and r[0]["kind"] == "pager")
check("... with its name and the message", r[0]["title"] == "Pager Duty crew (1234567)" and r[0]["text"] == "Shout 4 - house fire")
a.feed("pager", {"capcode": "1234567", "text": "Shout 4 - house fire"})
check("the same page again inside a minute is not repeated", len(a.since(0)["alerts"]) == 1)
a.feed("pager", {"capcode": "1234567", "text": "", "type": "Tone"})
check("a tone-only page still says something", a.since(0)["alerts"][-1]["text"] == "Tone – no text")

# --- aircraft
plane = {"icao": "4ca7b1", "flight": "EIN123", "squawk": "7700", "alt": 31000}
a.feed("adsb", {"aircraft": [{"icao": "400001", "flight": "BAW1", "squawk": "1200"}, plane]})
r = [x for x in a.since(0)["alerts"] if x["kind"] == "emergency"]
check("squawk 7700 alerts, 1200 does not", len(r) == 1 and "7700" in r[0]["title"])
check("... naming the flight, the meaning and the height", r[0]["text"] == "EIN123 – general emergency, 31000 ft")
a.feed("adsb", {"aircraft": [plane]})
check("the same aircraft is not announced every second", len([x for x in a.since(0)["alerts"] if x["kind"] == "emergency"]) == 1)
now[0] += 31 * 60
a.feed("adsb", {"aircraft": [plane]})
check("... but is again after half an hour", len([x for x in a.since(0)["alerts"] if x["kind"] == "emergency"]) == 2)
a.feed("adsb", {"aircraft": [{"icao": "abc", "flight": "", "squawk": "7600"}]})
check("an aircraft with no callsign is named by its hex code", a.since(0)["alerts"][-1]["text"].startswith("ABC – radio failure"))

# --- scanner / scheduler events
a.feed("event", {"level": "info", "text": "Priority channel active: Fire ground 1"})
a.feed("event", {"level": "info", "text": "Scheduled capture started: Meteor-M N2-4 until 14:32"})
a.feed("event", {"level": "info", "text": "Settings saved"})
ks = [x["kind"] for x in a.since(0)["alerts"]]
check("priority channel alert", "priority" in ks)
check("capture started alert", "capture" in ks)
check("ordinary events are ignored", not any("Settings saved" in x["text"] for x in a.since(0)["alerts"]))
last = a.since(0)["alerts"][-1]
check("capture alert has the job text", last["title"] == "Capture started" and last["text"].startswith("Meteor-M N2-4"))

# --- the feed
f = a.since(-1)
check("since=-1 only says where the queue is", f["alerts"] == [] and f["last"] == a._seq)
mid = a.since(0)["alerts"][2]["id"]
check("since=N returns only newer ones", all(x["id"] > mid for x in a.since(mid)["alerts"]) and len(a.since(mid)["alerts"]) == a._seq - mid)
check("ids only go up", [x["id"] for x in a.since(0)["alerts"]] == sorted(x["id"] for x in a.since(0)["alerts"]))
for i in range(150):
    a.add("test", "t", f"n{i}")
check("only the latest 100 are kept", len(a.since(0)["alerts"]) == 100)

# --- switches
b = Alerts(St({"pager_highlight": ["1"], "alert_pager": False, "alert_emergency": False, "alert_priority": False, "alert_capture": False}), clock=lambda: 5.0)
b.feed("pager", {"capcode": "1", "text": "x"}); b.feed("adsb", {"aircraft": [plane]})
b.feed("event", {"text": "Priority channel active: x"}); b.feed("event", {"text": "Scheduled capture started: y"})
check("every kind can be switched off", b.since(0)["alerts"] == [])
check("junk payloads never raise", (a.feed("pager", None), a.feed("adsb", None), a.feed("event", 5), a.feed("spectrum", b"x"))[0] is None)

# --- the HTTP route
from andysdr import server as srvmod
class R:
    def __init__(self, req): self.req = req
    async def readuntil(self, sep): return self.req
class W:
    def __init__(self, ip): self.buf = b""; self.ip = ip
    def write(self, x): self.buf += x
    async def drain(self): pass
    def close(self): pass
    def get_extra_info(self, k): return (self.ip, 5000)
def ask(path, ip="192.168.1.77", pw="secret"):
    s = srvmod.Server.__new__(srvmod.Server)
    s.s = {"server_port": 8045, "password": pw, "require_password_local": False}
    s.alerts = a
    w = W(ip)
    asyncio.run(s.handle(R(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode()), w))
    head, _, body = w.buf.partition(b"\r\n\r\n")
    return head.decode(), body
h, body = ask("/api/alerts?since=-1")
check("alert feed needs the password from another device", h.startswith("HTTP/1.1 403"))
h, body = ask("/api/alerts?since=-1&token=secret")
check("... and answers with it", h.startswith("HTTP/1.1 200") and json.loads(body)["last"] == a._seq)
h, body = ask(f"/api/alerts?since={a._seq - 2}&token=secret")
check("... returning only what is new", [x["id"] for x in json.loads(body)["alerts"]] == [a._seq - 1, a._seq])
h, body = ask("/api/alerts?since=junk&token=secret")
check("a bad since value is treated as -1", h.startswith("HTTP/1.1 200") and json.loads(body)["alerts"] == [])

print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
