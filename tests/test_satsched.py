"""Scheduler <-> satellite planner integration (simulator radio) and the rtl_tcp IQ bridge."""
import os, socket, struct, sys, tempfile, time, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from andysdr.iqbridge import IqBridge
from andysdr.scheduler import Scheduler
from andysdr import satpass as sp

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1

# ---- IQ bridge
b = IqBridge(out_rate=2400000)
port = b.start()
c = socket.create_connection(("127.0.0.1", port), timeout=3)
hdr = c.recv(12)
check("rtl_tcp header", hdr[:4] == b"RTL0" and len(hdr) == 12)
time.sleep(0.2)
fs = 2400000
n = 240000
t = np.arange(n) / fs
tone = np.exp(2j * np.pi * (-600000 + 12345) * t).astype(np.complex64) * 0.5       # wanted signal sits 12.345 kHz above the "offset" frequency
b.feed(tone, fs, offset=-600000)
buf = b""
c.settimeout(3)
while len(buf) < n * 2:
    buf += c.recv(65536)
u = np.frombuffer(buf[: n * 2], np.uint8).astype(np.float32) - 127.5
iq = u[0::2] + 1j * u[1::2]
spec = np.abs(np.fft.fft(iq * np.hanning(len(iq))))
k = int(np.argmax(spec))
f = (k if k < n // 2 else k - n) * fs / n
check("bridge shifts the wanted signal to DC (+12.3 kHz)", abs(f - 12345) < 300, f)
c.sendall(struct.pack(">BI", 1, 1694100000))
time.sleep(0.2)
check("client commands read", (1, 1694100000) in b.cmds)
# resampling path (DAB style 2.048 MS/s)
b.out_rate = 2048000
b.feed(tone, fs, offset=-600000)
buf = b""
while len(buf) < 2048000 // 10 * 2 - 40:
    buf += c.recv(65536)
check("resampled to 2.048 MS/s (0.1 s block)", abs(len(buf) / 2 - 204800) < 40, len(buf) / 2)
b.stop(); c.close()

# ---- scheduler with a fake radio
class FakeRadio:
    def __init__(self): self.started, self.ended, self.events = [], 0, []
    def begin_job(self, job): self.started.append(job)
    def end_job(self): self.ended += 1
    def _emit(self, k, v): self.events.append(k)
class S(dict):
    def save(self): pass
L1 = "1 59051U 24039A   26167.62069696  .00000001  00000-0  19950-4 0  9993"
L2 = "2 59051  98.7010 126.9258 0007253 162.5453 197.5976 14.22429458119188"
d = tempfile.mkdtemp()
st = S(obs_lat=51.5, obs_lon=-0.12, schedule=[])
r = FakeRadio()
sch = Scheduler(r, st); sch.alive = False
sch.sats.path = os.path.join(d, "t.json")
sch.sats.tle["59051"] = {"l1": L1, "l2": L2, "t": time.time()}
sch._tle_check = time.time()
sch.sats.set_cfg("meteor_m24", enabled=True)
sat = sch.sats.satrec(59051)
ep = (sat.jdsatepoch - 2440587.5) * 86400 + sat.jdsatepochF * 86400
# make "now" be the TLE epoch + 1 h by shifting TLE time stamp relation: planner uses wall time for pass search
real_now = time.time()
sch.sats.tle["59051"]["t"] = real_now
ps = sch.sats.passes("meteor_m24", real_now)
check("planner finds real upcoming passes (SGP4 propagates from old epoch)", len(ps) >= 3, len(ps))
p0 = ps[0]
when = dt.datetime.fromtimestamp(p0["aos"] + 10)
sch.tick(when)
check("pass start launches an LRPT job", len(r.started) == 1 and r.started[0]["action"] == "lrpt" and r.started[0]["freq"] == 137900000, r.started)
check("scheduler shows it running", sch.running and sch.running["action"] == "lrpt")
sch.tick(when + dt.timedelta(seconds=30))
check("no second start mid-pass", len(r.started) == 1)
sch.tick(dt.datetime.fromtimestamp(p0["los"] + 60))
check("job stops after the pass", sch.running is None and r.ended == 1)
sch.tick(dt.datetime.fromtimestamp(p0["los"] + 70))
check("same pass is not re-run", len(r.started) == 1)
stt = sch.status()
check("status carries satellites and passes", stt["sats"]["observer"] and any(x["id"] == "meteor_m24" for x in stt["sats"]["sats"]) and stt["sats"]["passes"])
check("GOES HRIT preset and action exist", "goes" in stt["actions"] and any(p["action"] == "goes" for p in stt["presets"]))
# manual job beats auto: a running manual job blocks satellite start
sch2 = Scheduler(FakeRadio(), S(obs_lat=51.5, obs_lon=-0.12, schedule=[])); sch2.alive = False
sch2.sats.tle["59051"] = {"l1": L1, "l2": L2, "t": real_now}; sch2._tle_check = time.time()
sch2.sats.set_cfg("meteor_m24", enabled=True)
sch2.running = {"id": "x", "name": "manual", "until": dt.datetime.fromtimestamp(real_now + 99999), "action": "record", "freq": 1}
sch2.tick(dt.datetime.fromtimestamp(sch2.sats.passes("meteor_m24", real_now)[0]["aos"] + 10))
check("manual capture keeps priority", not sch2.radio.started)
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
