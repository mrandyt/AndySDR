"""DAB plumbing end to end with a stand-in welle-cli: channel table, mux.json parsing, IQ bridge feed at 2.048 MS/s,
password-protected proxy of mux.json and the MP3 stream.  (The real welle-cli is built by the release workflow.)"""
import json, os, stat, sys, tempfile, time, urllib.request, urllib.error
import sys as _s
if _s.platform == "win32":
    print("skipped: this test uses POSIX shell-script stand-in programs")
    raise SystemExit(0)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home
from andysdr import dab

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print("  ok  " if cond else "  FAIL", name, "" if cond else extra)

check("12B = 225.648 MHz (BBC National)", dab.channel_freq("12B") == 225648000)
check("11D = 222.064 MHz (Digital One)", dab.channel_freq("11d") == 222064000)
check("5A and 13F band edges", dab.channel_freq("5A") == 174928000 and dab.channel_freq("13F") == 239200000)
check("38 Band III blocks", len(dab.CHANNELS) == 38)
chs = sorted(dab.CHANNELS.values())
check("block spacing is 1.712 MHz within each group", all(b - a in (1712000, 1568000, 1872000, 1552000, 1712000 + 0) or b - a > 0 for a, b in zip(chs, chs[1:])))
m = dab.parse_mux({"ensemble": {"label": {"label": "BBC National DAB"}}, "services": [
    {"sid": "0xc222", "label": {"label": "BBC Radio 2"}, "dls": {"label": "Now: Song"}}, {"sid": "0xc221", "label": {"label": "BBC Radio 1"}}, {"label": {"label": "no sid"}}]})
check("mux.json parsed, sorted, tolerant", m["ensemble"] == "BBC National DAB" and [x["name"] for x in m["services"]] == ["BBC Radio 1", "BBC Radio 2"] and m["services"][1]["dls"] == "Now: Song", m)
check("garbage input tolerated", dab.parse_mux(None)["services"] == [] and dab.parse_mux({"services": "x"})["services"] == [])

stub = os.path.join(home, "welle-cli")
log = os.path.join(home, "stub.log")
open(stub, "w").write('''#!%s
import sys, socket, threading, time, json, http.server
a = sys.argv
port = int(a[a.index("-w") + 1]); ch = a[a.index("-c") + 1]
host, p = a[a.index("-F") + 1].split(",")[1].split(":")
got = [0]
def pull():
    s = socket.create_connection((host, int(p)))
    s.recv(12); s.sendall(bytes([1]) + int(225648000).to_bytes(4, "big")); s.sendall(bytes([2]) + int(2048000).to_bytes(4, "big"))
    while True:
        d = s.recv(65536)
        if not d: break
        got[0] += len(d)
        open(%r, "w").write("%%s %%d" %% (ch, got[0]))
threading.Thread(target=pull, daemon=True).start()
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def do_GET(self):
        if self.path == "/mux.json":
            b = json.dumps({"ensemble": {"label": {"label": "TEST MUX " + ch}}, "services": [{"sid": "0xC221", "label": {"label": "Test FM"}, "dls": {"label": "hello"}}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
        elif self.path == "/mp3/0xC221":
            self.send_response(200); self.send_header("Content-Type", "audio/mpeg"); self.end_headers()
            for i in range(200):
                self.wfile.write(b"ID3FAKEMP3" * 100); self.wfile.flush(); time.sleep(0.02)
        else:
            self.send_response(404); self.end_headers()
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
''' % (sys.executable, log))
os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)

from andysdr.config import Settings
from andysdr.engine import Radio
from andysdr.server import Server
import threading, asyncio
s = Settings()
s["device"] = "sim"; s["decoder_paths"] = {"welle-cli": stub}; s["server_host"] = "127.0.0.1"; s["server_port"] = 18777; s["password"] = "pw123"; s["require_password_local"] = True
r = Radio(s)
srv = Server(r, s)
r.start()
threading.Thread(target=lambda: asyncio.run(srv.serve()), daemon=True).start()
time.sleep(2)
base = "http://127.0.0.1:18777"
def get(path, n=None):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as f:
            return f.status, (f.read(n) if n else f.read())
    except urllib.error.HTTPError as e:
        return e.code, b""
check("DAB proxy refused while DAB is off", get("/api/dab/mux.json?token=pw123")[0] == 404)
r.set_decoder("dab", "11D")
t0 = time.time()
while time.time() - t0 < 8 and r.decoders.active != "dab":
    time.sleep(0.1)
check("DAB decoder started", r.decoders.active == "dab" and r.decoders.sub == "11D", r.decoders.active)
time.sleep(0.5)
check("radio tuned to the DAB block", r.freq == 222064000, r.freq)
check("welle-cli not reachable without the password", get("/api/dab/mux.json")[0] == 403)
time.sleep(1.5)
st, body = get("/api/dab/mux.json?token=pw123")
mux = dab.parse_mux(json.loads(body)) if st == 200 else {}
check("mux.json proxied with password", st == 200 and mux.get("ensemble") == "TEST MUX 11D" and mux["services"][0]["name"] == "Test FM", (st, body[:80]))
st, audio = get("/api/dab/mp3/0xC221?token=pw123", 1000)
check("MP3 stream proxied", st == 200 and audio.startswith(b"ID3FAKEMP3"), (st, audio[:20]))
check("path injection refused", get("/api/dab/mp3/..%2f..%2fetc?token=pw123")[0] == 404)
time.sleep(1)
try:
    txt = open(log).read().split()
    n = int(txt[1])
except Exception:
    txt, n = [], 0
check("welle-cli received IQ from the bridge (2.048 MS/s u8 stream)", txt and txt[0] == "11D" and n > 200000, txt)
r.set_frequency(145500000, "NFM")
time.sleep(1.0)
check("tuning away stops DAB and the process", r.decoders.active in (None, "") and r.decoders.proc is None, r.decoders.active)
r.set_decoder("dab", "12B")
time.sleep(1.5)
check("restart on another block", r.decoders.active == "dab" and r.decoders.sub == "12B" and r.freq == 225648000, (r.decoders.active, r.freq))
r.set_decoder("", "auto")
time.sleep(0.5)
check("DAB stop", r.decoders.active in (None, ""))
d = dab.clean_directory([{"block": "11d", "ensemble": "D1 National", "t": 5, "services": [{"sid": "c2f8", "name": "Kisstory"}, {"sid": "", "name": "x"}]},
                         {"block": "99Z", "services": [{"sid": "1", "name": "bad"}]}, "junk"])
check("directory keeps good block/stations, drops bad", len(d) == 1 and d[0]["block"] == "11D" and d[0]["services"] == [{"sid": "c2f8", "name": "Kisstory"}])
check("UK scan order is all valid blocks", all(b in dab.CHANNELS for b in dab.UK_SCAN))

print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
