"""AERO (Inmarsat satellite ACARS): message conversion, statistics, carrier finder, and a full run against a fake SatDump."""
import os
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import aero  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = [sys.executable, os.path.join(HERE, "fake_satdump.py")]
ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if extra and not cond else ""))
    ok = ok and bool(cond)


# ---------------------------------------------------------------- convert_acars
js = {"mode": 50, "tak": 33, "label": "H1", "bi": 65, "plane_reg": ".G-EUUU", "has_text": True,
      "message": "TEST\r\nUPLINK<DEL>", "more_to_come": False, "msg_name": "ACARS",
      "signal_unit": {"aes_id": 0x4CA123, "ges_id": 5}}
m = aero.convert_acars(js)
check("ACARS message converted", m is not None and m["reg"] == "G-EUUU" and m["label"] == "H1" and m["via"] == "AERO", m)
check("text cleaned (CRLF, <DEL>)", m and m["text"] == "TEST\nUPLINK", m and m["text"])
check("aircraft ICAO address in hex", m and m["icao"] == "4CA123", m and m["icao"])
check("mode / ack / block characters", m and (m["mode"], m["ack"], m["bid"]) == ("2", "!", "A"), m)
check("uplink block id is a letter -> not 'down'", m and m["down"] is False)
check("NAK is named", aero.convert_acars(dict(js, tak=0x15))["ack"] == "NAK")
check("label DEL becomes d", aero.convert_acars(dict(js, label="Q\x7f"))["label"] == "Qd")
check("non-ACARS packet ignored", aero.convert_acars({"msg_name": "AES System Table Broadcast Index"}) is None)
check("garbage ignored", aero.convert_acars(None) is None and aero.convert_acars([]) is None)
check("missing fields do not crash", aero.convert_acars({"msg_name": "ACARS"}) is not None)
check("more_to_come carried", aero.convert_acars(dict(js, more_to_come=True))["more"] is True)

# ---------------------------------------------------------------- parse_stats
st = aero.parse_stats({"psk": {"snr": 6.2, "peak_snr": 8.0, "freq": -310.0},
                       "dec": {"lock_state": "SYNCED", "viterbi_ber": 0.04, "correlator_corr": 33.0},
                       "other": 5})
check("stats: lock, ber, snr, carrier",
      st == {"lock": True, "ber": 0.04, "corr": 33.0, "snr": 6.2, "peak_snr": 8.0, "carrier_hz": -310.0}, st)
check("stats: no lock", aero.parse_stats({"d": {"lock_state": "NOSYNC"}})["lock"] is False)
check("stats: garbage", aero.parse_stats(None) == {} and aero.parse_stats({"a": 1}) == {})


# ---------------------------------------------------------------- carrier finder
def burst(fs, secs, f0, bps, snr_db, seed=1):
    """BPSK at `bps` symbols/s on a carrier f0 Hz from DC, root-raised-cosine-ish shaped, in noise."""
    rng = np.random.default_rng(seed)
    n = int(fs * secs)
    sps = fs / bps
    sym = rng.choice([-1.0, 1.0], int(n / sps) + 4)
    x = np.repeat(sym, int(round(sps)))[:n].astype(np.float64)
    k = int(sps)
    win = np.hanning(2 * k)
    x = np.convolve(x, win / win.sum(), mode="same")
    t = np.arange(n) / fs
    sig = x * np.exp(2j * np.pi * f0 * t)
    p_sig = np.mean(np.abs(sig) ** 2)
    sigma = np.sqrt(p_sig / (10 ** (snr_db / 10)) / 2) * np.sqrt(fs / bps)   # snr_db is per-symbol-bandwidth
    noise = sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return (sig + noise).astype(np.complex64)


for bps, f0 in ((1200, 1234.0), (600, -2750.0), (10500, 3300.0)):
    iq = burst(240000, 1.5, f0, bps, snr_db=6.0)
    r = aero.estimate_offset(iq, 240000, {1200: 1700.0, 600: 850.0, 10500: 10500.0}[bps])
    tol = 120.0 if bps < 5000 else 600.0
    check(f"carrier finder {bps} bit/s at {f0:+.0f} Hz", r is not None and abs(r[0] - f0) < tol, r)
iq = burst(240000, 1.5, -1900.0, 1200, snr_db=3.0, seed=7)
r3 = aero.estimate_offset(iq, 240000, 1700.0)
check("carrier finder still sees a weak (3 dB) carrier", r3 is not None and abs(r3[0] + 1900.0) < 120.0, r3)
rng = np.random.default_rng(3)
noise = (rng.standard_normal(360000) + 1j * rng.standard_normal(360000)).astype(np.complex64)
check("carrier finder: pure noise -> nothing", aero.estimate_offset(noise, 240000, 1700.0) is None)
check("carrier finder: too little data -> nothing", aero.estimate_offset(noise[:1000], 240000, 1700.0) is None)
far = burst(240000, 1.5, 40000.0, 1200, 10.0)
check("carrier finder: signal outside the search span ignored", aero.estimate_offset(far, 240000, 1700.0) is None)

# ---------------------------------------------------------------- full run against the fake SatDump
def run(sub, wait, **kw):
    msgs, stats, logs = [], [], []
    out = tempfile.mkdtemp(prefix="aero_")
    r = aero.AeroRunner(FAKE, out, sub, on_message=msgs.append, on_status=stats.append, on_log=lambda t, k="decoder": logs.append(t),
                        poll=0.3, **kw)
    r.start()
    try:
        fs = 2000000
        t0 = time.time()
        rng = np.random.default_rng(5)
        while time.time() - t0 < wait:
            iq = (0.01 * (rng.standard_normal(100000) + 1j * rng.standard_normal(100000))).astype(np.complex64)
            r.feed(iq, fs, 12345.0)
            time.sleep(0.05)
            if msgs and stats and stats[-1].get("lock"):
                time.sleep(0.8)
                break
    finally:
        r.stop()
    return r, msgs, stats, logs


r, msgs, stats, logs = run("p600", 25)
check("fixed 600 bit/s: message arrives", len(msgs) == 1 and msgs[0]["reg"] == "G-EUUU" and msgs[0]["text"] == "TEST UPLINK 1", msgs)
check("fixed 600 bit/s: status says locked", any(s.get("state") == "locked" and s.get("lock") for s in stats), stats[-3:])
last = [s for s in stats if s.get("lock")][-1] if any(s.get("lock") for s in stats) else {}
check("status carries ber / snr / counts", last.get("ber") == 0.05 and last.get("snr") == 7.5 and last.get("messages") == 1, last)
check("non-ACARS packet counted as a frame", last.get("frames", 0) >= 2, last)
check("SatDump actually received IQ over the bridge", any(l.startswith("bytes=") and int(l[6:]) > 0 for l in logs), logs[-5:])
check("final status says stopped", stats[-1] == {"running": False}, stats[-1])
check("message files are deleted once read", not [f for _, _, fs_ in os.walk(r.out_dir) for f in fs_ if f.endswith(".json")], "left behind")
check("SatDump packet chatter kept out of the log", not any("Packet :" in l or "ACARS message (" in l or l == "TEST UPLINK 1" for l in logs), logs)
check("SatDump warnings still reach the log", any("Invalid CRC" in l for l in logs), logs)
check("fixed mode never changed rate", all(s.get("mode") == "p600" for s in stats if s.get("running")))

r, msgs, stats, logs = run("auto", 40, lock_wait=2)
modes = []
for s in stats:
    if s.get("running") and (not modes or modes[-1] != s["mode"]):
        modes.append(s["mode"])
check("AUTO starts on 1200 and steps to 600", modes[:2] == ["p1200", "p600"], modes)
check("AUTO ends up locked on 600 and decodes", any(s.get("lock") and s["mode"] == "p600" for s in stats) and len(msgs) == 1, (modes, len(msgs)))
check("AUTO explains the change in the log", any("trying" in l and "600" in l for l in logs), logs[-6:])

# ---------------------------------------------------------------- housekeeping of old session folders
base = tempfile.mkdtemp()
old, new = os.path.join(base, "aero_20200101_000000"), os.path.join(base, "aero_20261125_120000")
other = os.path.join(base, "lrpt_keep_me")
for d in (old, new, other):
    os.makedirs(os.path.join(d, "run1"))
t = time.time() - 3 * 86400
os.utime(old, (t, t))
os.utime(other, (t, t))
check("purge_old removes only old AERO folders", aero.purge_old(base) == 1 and not os.path.exists(old) and os.path.exists(new) and os.path.exists(other))

# ---------------------------------------------------------------- the measurement is dropped when the cursor moves
rr = aero.AeroRunner(FAKE, tempfile.mkdtemp(), "p1200")
rr.bridge = type("B", (), {"feed": lambda self, *a: None})()
rr._tap(np.ones(1000, np.complex64))
rr.off = (123.0, 9.0)
rr.feed(np.ones(10, np.complex64), 2000000, 1000.0)
check("first feed clears the old measurement", rr.measurement() is None and rr._buf_n == 0)
rr._tap(np.ones(1000, np.complex64))
rr.off = (123.0, 9.0)
rr.feed(np.ones(10, np.complex64), 2000000, 1000.5)
check("a sub-2 Hz jitter keeps it", rr.measurement() == (123.0, 9.0) and rr._buf_n == 1000)
rr.feed(np.ones(10, np.complex64), 2000000, 1250.0)
check("moving the VFO drops it", rr.measurement() is None and rr._buf_n == 0)

print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
