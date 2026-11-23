"""DMR callsign list: automatic download / refresh from RadioID, kept safe when a download fails."""
import os, sys, tempfile, json, threading, http.server
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from andysdr import dmrdb
from andysdr.dmrdb import DmrDb, parse_csv, parse_talkgroups

fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

dmrdb.MIN_ROWS = 5
HEAD = "RADIO_ID,CALLSIGN,FIRST_NAME,LAST_NAME,CITY,STATE,COUNTRY,REMARKS\n"
ROWS = "".join(f"{2350000 + i},G{i}ABC,Sam{i},Smith,Leeds,England,United Kingdom,\n" for i in range(10))
CSV = HEAD + "2356184,m7fep,Andy,T,Bristol,England,United Kingdom,\n" + ROWS

# ---------------------------------------------------------------- parsing
recs = list(parse_csv(CSV.splitlines(True)))
check("csv with a header", len(recs) == 11 and recs[0] == (2356184, "M7FEP", "Andy T", "Bristol, United Kingdom"))
recs = list(parse_csv("﻿id,call_sign,fname,surname,city,state,country\n1234,G1AAA,Bob,Jones,Hull,,UK\nbad,row\n".splitlines(True)))
check("other column names, a BOM and a junk line", recs == [(1234, "G1AAA", "Bob Jones", "Hull, UK")])
recs = list(parse_csv(["1234,G1AAA,Bob,Jones,Hull,Yorks,UK\n"]))
check("a file with no header uses the classic column order", recs == [(1234, "G1AAA", "Bob Jones", "Hull, UK")])
check("talkgroup names (object form)", parse_talkgroups({"91": "World-wide", "x": "no", "9": " Local "}) == {91: "World-wide", 9: "Local"})
check("talkgroup names (list form)", parse_talkgroups([{"id": 235, "name": "UK"}]) == {235: "UK"})

# ---------------------------------------------------------------- a fake RadioID
state = {"csv": CSV, "csv_ok": True, "hits": []}
TG = {str(i): f"Group {i}" for i in range(100, 140)}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        state["hits"].append(self.path)
        if self.path.endswith("user.csv") and state["csv_ok"]:
            body = state["csv"].encode()
        elif self.path.endswith("talkgroup"):
            body = json.dumps(TG).encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
dmrdb.TG_URL = base + "/v2/talkgroup"
dmrdb.JSON_URL = base + "/users.json"
import urllib.request
def opener(url):
    return urllib.request.urlopen(url, timeout=10)

now = [1_000_000.0]
events = []
d = tempfile.mkdtemp()
S = {"dmr_db_auto": True, "dmr_db_url": base + "/static/user.csv"}
db = DmrDb(S, lambda k, p: events.append((k, p)), folder=d, clock=lambda: now[0], opener=opener,
           memory_tg=lambda: {235: "My own name for 235"})
st = db.status()
check("before any download the state is 'missing' and nothing is found", st["state"] == "missing" and db.lookup(2356184) is None)
check("... and it is due (auto is on)", db.due() is True)
db.refresh(wait=True)
st = db.status()
check("the download fills the list", st["state"] == "ready" and st["count"] == 11 and st["tg_count"] == 40)
r = db.lookup(2356184)
check("lookup gives callsign, name and place", r == {"call": "M7FEP", "name": "Andy T", "place": "Bristol, United Kingdom"})
check("an unknown ID gives None", db.lookup(1) is None and db.lookup("junk") is None)
check("talkgroup names: own import first, then BrandMeister, then built-in",
      db.tg_name(235) == "My own name for 235" and db.tg_name(100) == "Group 100" and db.tg_name(91) == "World-wide" and db.tg_name(5) == "")
check("progress and result were sent to the screens", [p["state"] for k, p in events if k == "dmrdb"][:1] == ["downloading"]
      and [p for k, p in events if k == "dmrdb"][-1]["state"] == "ready"
      and any(k == "event" and "11" in p["text"] for k, p in events))
check("it is not due again straight away", db.due() is False)
now[0] += 29 * 86400
check("... but is due again after 28+ days", db.due() is True)

# a restart keeps what was downloaded
db2 = DmrDb(S, None, folder=d, clock=lambda: now[0], opener=opener)
check("a new run reads the saved list and talkgroups", db2.lookup(2356184)["call"] == "M7FEP" and db2.tg_name(100) == "Group 100" and db2.status()["age_days"] >= 29)

# a failed download must not lose the old list
state["csv_ok"] = False
before = len(state["hits"])
db2.refresh(wait=True)
st = db2.status()
check("when the site is down the old list is kept", st["state"] == "ready" and st["count"] == 11 and db2.lookup(2356184)["call"] == "M7FEP")
check("... a retry is not attempted for a few hours", db2.due() is False)
now[0] += 7 * 3600
check("... and is allowed after that", db2.due() is True)
state["csv_ok"] = True
state["csv"] = "<html>Sorry, down for maintenance</html>"
db2.refresh(wait=True)
check("a junk file is rejected and the good list stays", db2.status()["count"] == 11 and db2.lookup(2356184)["call"] == "M7FEP")
check("... and the half-written file is cleaned up", not os.path.exists(db2.db_path + ".new"))
now[0] += 7 * 3600
state["csv"] = CSV.replace("Andy T", "Andy").replace("Andy,T", "Andrew,Tester") + "2350999,G9ZZZ,New,Person,York,England,United Kingdom,\n"
db2.refresh(wait=True)
check("a good new file replaces the old one", db2.status()["count"] == 12 and db2.lookup(2356184)["name"] == "Andrew Tester" and db2.lookup(2350999)["call"] == "G9ZZZ")
check("... (the lookup cache was dropped)", db2.lookup(2356184)["name"] == "Andrew Tester")

# a never-reachable site on first run
d3 = tempfile.mkdtemp()
dead = DmrDb({"dmr_db_auto": True, "dmr_db_url": "http://127.0.0.1:9/none.csv"}, None, folder=d3, clock=lambda: now[0], opener=lambda u: urllib.request.urlopen(u, timeout=2))
dead.refresh(wait=True)
st = dead.status()
check("no internet on first run -> state 'error' with a message, no crash", st["state"] == "error" and st["msg"] != "" and dead.lookup(1) is None)

# auto switched off
off = DmrDb({"dmr_db_auto": False}, None, folder=tempfile.mkdtemp(), clock=lambda: now[0], opener=opener)
check("the auto setting is reported", off.status()["auto"] is False and db.status()["auto"] is True)

# a second refresh while one runs is refused
started = []
gate = threading.Event()
def slow(url):
    gate.wait(5); raise OSError("stop")
busy = DmrDb(S, None, folder=tempfile.mkdtemp(), clock=lambda: now[0], opener=slow)
check("a download starts in the background", busy.refresh() is True and busy.status()["state"] == "downloading")
check("... and a second one is refused while it runs", busy.refresh() is False)
gate.set()
srv.shutdown()

print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
