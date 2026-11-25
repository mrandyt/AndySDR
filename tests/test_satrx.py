"""The second (satellite-only) receiver: recording a pass on a fake dongle, the pass report, and how the scheduler hands passes over."""
import os, sys, tempfile, time, datetime as dt, wave
tmp = tempfile.mkdtemp()
os.environ["HOME"] = tmp
os.environ["APPDATA"] = tmp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from andysdr import devices, satrx as satrx_mod
from andysdr.satrx import SatRx
from andysdr.scheduler import Scheduler

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1

class S(dict):
    def save(self): pass

class FakeDongle(devices.BaseDevice):
    kind = "rtlsdr"
    def __init__(self, key="rtlsdr:TEST", fail=False):
        super().__init__(key, "Fake NESDR :: TEST")
        self.fail, self.opened, self.closed, self.gains_set = fail, 0, 0, []
        self.t = 0
    def open(self):
        if self.fail:
            raise RuntimeError("rtlsdr_open failed (-1)")
        self.opened += 1
    def close(self): self.closed += 1
    def gain_range(self): return (0.0, 49.6)
    def set_gain(self, g): self.gain = g; self.gains_set.append(g)
    def sample_rates(self): return [1200000, 2400000]
    def read(self, n):
        time.sleep(n / self.sample_rate)                      # real time
        k = np.arange(self.t, self.t + n); self.t += n
        off = 137900000 - self.center                         # the satellite sits here relative to the centre
        rng = np.random.default_rng(self.t)
        x = 0.05 * (rng.standard_normal(n) + 1j * rng.standard_normal(n)) + 0.3 * np.exp(2j * np.pi * off * k / self.sample_rate)
        return x.astype(np.complex64)

class FakeSched:
    def __init__(self): self.n = 0
    def _emit(self): self.n += 1
class FakeRadio:
    def __init__(self):
        self.s = S(satrx_enabled=False)
        self.events, self.dev = [], None
        self.scheduler = FakeSched()
    def _emit(self, k, v): self.events.append((k, v))

dongle = FakeDongle()
devices.list_rtl_native = lambda: [dongle]
radio = FakeRadio()
rx = SatRx(radio)

check("off by default: not usable", not rx.usable())
rx.configure(enabled=True, gain=36.0)
check("switched on and a dongle found: usable", rx.usable())
check("gain setting stored and clamped to the dongle's range", rx.cfg()["gain"] == 36.0)
rx.configure(gain=99)
check("gain above the range is clamped", rx.cfg()["gain"] <= 49.6, rx.cfg()["gain"])
rx.configure(gain=36.0)

radio.dev = FakeDongle()                                      # the main radio is using this very dongle
check("a dongle the main radio uses is never taken", not rx.usable())
radio.dev = None
rx._found = ([], 0.0)

rx.configure(device="rtlsdr:OTHER")
check("a chosen dongle that is not there is not silently replaced", not rx.usable())
rx.configure(device="")

# ---- a pass
job = {"id": "sat:meteor_m24:1", "name": "Meteor-M N2-4 (LRPT) pass (max 60°)", "sat": "meteor_m24", "freq": 137900000,
       "action": "lrpt", "max_el": 60.0, "gain": 36.0, "gain_mode": "fixed", "northbound": False}
check("pass starts on the second receiver", rx.begin_job(job) is True and rx.busy())
check("dongle opened at the pass gain", dongle.opened == 1 and 36.0 in dongle.gains_set, dongle.gains_set)
check("centre kept off the signal (DC spike)", abs(dongle.center - 137900000) > 100000, dongle.center)
rx.retune_job(137903000)
time.sleep(3.2)
check("signal strength measured", rx.snr is not None and rx.snr > 10, rx.snr)
path = rx.decoders.rec_path
rx.end_job()
check("dongle let go of afterwards", dongle.closed >= 1 and not rx.busy())
check("the recording is a 240 kS/s IQ file with samples in it", path and os.path.exists(path), path)
if path and os.path.exists(path):
    w = wave.open(path)
    check("WAV is 2 channels, 240000 Hz", w.getnchannels() == 2 and w.getframerate() == 240000)
    check("about 3 s of IQ was recorded", 2.0 * 240000 < w.getnframes() < 4.5 * 240000, w.getnframes())
    w.close()
rows = rx.passlog.rows
check("pass report written with the gain and signal", len(rows) == 1 and rows[0]["gain"] == 36.0 and rows[0]["snr_peak"] > 10 and rows[0]["rx"] == "rx2", rows)
check("the report is kept in its own file", os.path.basename(rx.passlog.path) == "passes_rx2.jsonl")

# ---- Learn the best uses this receiver's own history
rx.configure(gain_mode="auto")
g, mode = rx.pass_gain("meteor_m24")
check("Learn the best gives a gain inside the dongle's range", mode == "auto" and 0 <= g <= 49.6, (g, mode))
rx.configure(gain_mode="fixed")
check("fixed mode gives the fixed gain", rx.pass_gain("meteor_m24") == (36.0, "fixed"))

# ---- failing dongle: the main radio gets the pass
bad = FakeDongle(fail=True)
devices.list_rtl_native = lambda: [bad]
rx._found = ([], 0.0)
rx.fail_until = 0.0
check("a dongle that will not open: begin_job says no", rx.begin_job(dict(job, id="x")) is False and not rx.busy())
check("and the second receiver rests for a while", not rx.usable() and "could not open" in rx.last_error, rx.last_error)

# ---- which satellites does it take?
from andysdr.satpass import SATELLITES
took = {sid for sid, d in SATELLITES.items() if satrx_mod.takes(sid, d)}
check("only the 137 MHz Meteors", took == {"meteor_m23", "meteor_m24"}, took)

# ---- scheduler hand-over
L1 = "1 59051U 24039A   26167.62069696  .00000001  00000-0  19950-4 0  9993"
L2 = "2 59051  98.7010 126.9258 0007253 162.5453 197.5976 14.22429458119188"
class MainRadio:
    def __init__(self, sat=None):
        self.started, self.ended, self.events, self.satrx = [], 0, [], sat
    def begin_job(self, job): self.started.append(job)
    def end_job(self, why=None): self.ended += 1
    def retune_job(self, f): pass
    def _emit(self, k, v): self.events.append(k)
class StubRx:
    def __init__(self, ok=True):
        self.ok, self.started, self.ended, self.retunes, self._busy = ok, [], 0, [], False
        self.passlog = rx.passlog
    def usable(self): return True
    def busy(self): return self._busy
    def pass_gain(self, sid): return 33.0, "fixed"
    def begin_job(self, job):
        if self.ok:
            self.started.append(job); self._busy = True
        return self.ok
    def retune_job(self, f): self.retunes.append(f)
    def end_job(self, why=None): self.ended += 1; self._busy = False
    def status(self): return {"enabled": True}

def make(sat):
    m = MainRadio(sat)
    sch = Scheduler(m, S(obs_lat=51.5, obs_lon=-0.12, schedule=[])); sch.alive = False
    sch.sats.path = os.path.join(tmp, "t%d.json" % id(m))
    sch.sats.tle["59051"] = {"l1": L1, "l2": L2, "t": time.time()}
    sch._tle_check = time.time()
    sch.sats.set_cfg("meteor_m24", enabled=True)
    return m, sch

# ---- live view (a test look with no recording) and the screen's live data
devices.list_rtl_native = lambda: [dongle]
dongle.fail = False
rx.fail_until = 0.0
rx._found = ([], 0.0)
radio.events.clear()
rows_before = len(rx.passlog.rows)
check("not busy before the live view", not rx.busy() and rx.live()["on"] is False)
ok, msg = rx.begin_preview(131725000, 8000)
check("live view starts", ok and rx.busy() and rx.live()["preview"] is True, msg)
time.sleep(1.6)
lv = rx.live()
check("live view reports a signal reading and the width it measures", lv["on"] and lv["snr"] is not None and lv["bw"] == 8000, lv)
check("live view sends spectrum frames and live updates to the screens",
      any(k == "satspec" for k, _ in radio.events) and any(k == "satrx_live" for k, _ in radio.events))
sp = [v for k, v in radio.events if k == "satspec"][-1]
check("a spectrum frame is 2048 bytes around the tuned frequency", len(sp["data"]) == 2048 and sp["freq"] == 131725000, (len(sp["data"]), sp["freq"]))
check("live view records nothing and writes no pass report", rx.passlog.open_key is None and len(rx.passlog.rows) == rows_before)
check("a second live view is refused", rx.begin_preview()[0] is False)
g0 = rx.set_gain(42.5)
check("gain can be changed by hand while it is working", g0 == 42.5 and dongle.gain == 42.5 and 42.5 in dongle.gains_set and rx.live()["gain"] == 42.5, (dongle.gain, rx.live()["gain"]))
check("and is remembered for the next pass", radio.s["satrx_gain"] == 42.5)
check("a gain beyond the dongle's range is clamped", rx.set_gain(99) <= 49.6)
rx.set_gain(36.0)
check("an out-of-range frequency is refused", (rx.stop_preview(), rx.begin_preview(5e9)[0])[1] is False)
check("live view stops cleanly and lets the dongle go", not rx.busy() and rx.dev is None and rx.live()["on"] is False)
rx.begin_preview()
time.sleep(0.4)
job2 = dict(job, id="sat:meteor_m24:2")
check("a real pass takes over from a live view", rx.begin_job(job2) is True and rx.job.get("preview") is None)
time.sleep(0.8)
check("pass live data has the satellite name and no preview flag", rx.live()["preview"] is False and "Meteor" in str(rx.live()["name"]), rx.live())
rx.end_job()
check("and the dongle is free again", not rx.busy())
st = rx.status()
check("status carries live data and the last pass for the screen", "live" in st and "last" in st and "fail_in" in st)

stub = StubRx()
m, sch = make(stub)
p0 = sch.sats.passes("meteor_m24", time.time())[0]
when = dt.datetime.fromtimestamp(p0["aos"] + 10)
sch.tick(when)
check("pass goes to the second receiver, not the main radio", len(stub.started) == 1 and not m.started and sch.running2 and not sch.running)
check("with the second receiver's own gain", stub.started[0]["gain"] == 33.0)
sch.tick(when + dt.timedelta(seconds=20))
check("Doppler followed on the second receiver", len(stub.retunes) >= 1 and abs(stub.retunes[-1] - 137900000) < 4000, stub.retunes)
check("no double start", len(stub.started) == 1 and not m.started)
stt = sch.status()
check("status shows the second receiver's pass", stt["running2"] and stt["satrx"]["enabled"] is True and "next" in stt["satrx"])
sch.tick(dt.datetime.fromtimestamp(p0["los"] + 60))
check("second receiver pass ends after the pass", stub.ended == 1 and sch.running2 is None)
sch.tick(dt.datetime.fromtimestamp(p0["los"] + 70))
check("the pass is not re-run on either radio", len(stub.started) == 1 and not m.started)

bad_stub = StubRx(ok=False)
m2, sch2 = make(bad_stub)
sch2.tick(when)
check("if the second receiver cannot start, the main radio takes the pass", not sch2.running2 and len(m2.started) == 1 and sch2.running, (m2.started, sch2.running2))

m3, sch3 = make(None)
sch3.tick(when)
check("no second receiver at all: unchanged behaviour", len(m3.started) == 1 and sch3.running and not sch3.running2)

# ---- a live view must never make a pass go to the main radio (14:59 on 25 Nov: the Nooelec was in live view when the pass began)
good = FakeDongle("rtlsdr:TEST")
blank = FakeDongle("rtlsdr:0")                                # what a dongle that is already open looks like on a second scan
rx4 = SatRx(FakeRadio())
devices.list_rtl_native = lambda: [blank] if rx4.job is not None else [good]
rx4.configure(enabled=True, device="rtlsdr:TEST", gain=20.0, gain_mode="fixed")
check("live-view test: usable before the view", rx4.usable())
ok, msg = rx4.begin_preview()
check("live view starts", ok and rx4.busy(), msg)
rx4._found = (rx4._found[0], 0.0)                             # the list is due for a fresh look while the view is running
check("a live view does not hide the dongle from the next scan", rx4.usable())
check("so the second receiver is still offered the pass", rx4.choose() is good, rx4.choose())
job2 = dict(job, id="sat:meteor_m24:2", gain=20.0, gain_mode="fixed")
check("a pass that comes due during a live view takes over from it", rx4.begin_job(job2) is True and rx4.busy() and not rx4.job.get("preview"))
rx4.end_job()
check("after the pass the dongle is found again by a normal scan", rx4.usable() and rx4.choose() is good)
devices.list_rtl_native = lambda: [good]

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
