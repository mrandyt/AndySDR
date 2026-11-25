"""Auto-identify must not flip between decoders on a signal it cannot make up its mind about.
The kind sequences below are taken from a real diagnostics log (AndySDR 1.22.5, 17:00-17:16 on 25 Nov 2026): a steady carrier on
165.050 MHz and 167.925 MHz that the classifier called P25/DMR, FM voice, NXDN, Pager and TETRA in turn - about 280 changes."""
import os
import sys
import tempfile

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from andysdr.autodetect import SignalClassifier, UNSETTLED  # noqa: E402
from andysdr.config import Settings  # noqa: E402
from andysdr.engine import Radio  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if extra and not cond else ""))
    ok = ok and bool(cond)


# what classify() returns for each guess (decoder, sub, mode, conf) - the values the real classifier uses
G = {"P25 / DMR": ("dsd", "auto", "DIG", 0.8), "FM voice": ("", "auto", None, 0.6), "NXDN / dPMR": ("dsd", "nxdn48", "DIG", 0.75),
     "Pager": ("pager", "auto", None, 0.85), "TETRA": ("tetra", "", "DIG25", 0.8), "DMR": ("dsd", "dmr", "DIG", 0.9),
     "TETRAPOL": ("tetrapol", "", "DIG", 0.92)}


def res(kind):
    d, sub, mode, conf = G[kind]
    return {"kind": kind, "label": kind, "decoder": d, "sub": sub, "mode": mode, "conf": conf, "snr": 25.0, "bw": 7.5, "duty": 1.0, "cv": 0.12}


# 167.925 MHz 17:15:00-17:16:13, in the order they were logged (one entry per change)
REAL = ["P25 / DMR", "FM voice", "P25 / DMR", "FM voice", "P25 / DMR", "FM voice", "NXDN / dPMR", "FM voice", "P25 / DMR", "FM voice",
        "NXDN / dPMR", "P25 / DMR", "FM voice", "P25 / DMR", "FM voice", "NXDN / dPMR", "P25 / DMR", "FM voice", "P25 / DMR", "FM voice",
        "NXDN / dPMR", "Pager", "P25 / DMR", "NXDN / dPMR", "Pager", "P25 / DMR", "FM voice", "P25 / DMR", "NXDN / dPMR", "FM voice"] * 3

c = SignalClassifier()
out = [c.decide(res(k)) for k in REAL]
unsettled = [r for r in out if r["kind"] == UNSETTLED]
check("real flip-flop log: ends as 'Unidentified signal', stable", out[-1]["kind"] == UNSETTLED and out[-1]["stable"], out[-1]["kind"])
check("it settles within about 6 results", next(i for i, r in enumerate(out) if r["kind"] == UNSETTLED) <= 6)
check("once unsettled it stays unsettled on the same flipping", all(r["kind"] == UNSETTLED for r in out[8:]))
check("unsettled result starts no decoder", all(r["decoder"] == "" and r["mode"] is None for r in unsettled))
check("the label says what to do", "Unidentified" in out[-1]["label"] and "by hand" in out[-1]["label"])

# ---- things that must still work exactly as before
c = SignalClassifier()
seq = [c.decide(res("DMR")) for _ in range(10)]
check("steady DMR stays DMR and stable", all(r["kind"] == "DMR" and r["stable"] for r in seq[1:]))
c = SignalClassifier()
seq = [c.decide(res("FM voice")) for _ in range(10)]
check("steady FM voice stays FM voice", all(r["kind"] == "FM voice" for r in seq))
c = SignalClassifier()
seq = [c.decide(res(k)) for k in ["FM voice"] * 5 + ["Pager"] * 5]
check("a real change (voice, then pager) is followed", seq[-1]["kind"] == "Pager" and seq[-1]["stable"])
check("it was never called unidentified on the way", all(r["kind"] != UNSETTLED for r in seq))
c = SignalClassifier()
seq = [c.decide(res(k)) for k in ["FM voice", "P25 / DMR"] * 6]
check("two alternating guesses are left to the old two-in-three rule (not 'unidentified')", all(r["kind"] != UNSETTLED for r in seq))
c = SignalClassifier()
seq = [c.decide(res(k)) for k in ["P25 / DMR", "FM voice", "NXDN / dPMR", "FM voice", "P25 / DMR"] + ["TETRAPOL"] * 3]
check("a confident answer (TETRAPOL, 0.92) is never held back", seq[-1]["kind"] == "TETRAPOL" and seq[-1]["stable"])
c = SignalClassifier()
c.decide(res("FM voice"))
c.decide(None)
check("no signal clears the history", c.history == [])

# ---- through the engine: how many times is a decoder started?
s = Settings()
s["device"] = "sim"
s["auto_identify"] = False      # the engine must not run its own classifier on the simulator while the test feeds results
r = Radio(s)
r.start()
import time  # noqa: E402
time.sleep(1.0)
r.set_frequency(167925000, "NFM")
starts, stops = [], []


def fake_start(d, sub="auto"):
    starts.append(d)
    r.decoders.active, r.decoders.sub = d, sub
    return True


def fake_stop():
    stops.append(1)
    r.decoders.active = None


r.decoders.start = fake_start
r.decoders.stop = fake_stop
r.decoders.dsd_recent_sync = lambda n: False
r.s["auto_identify"] = True
r.auto_gen += 1
r.job = None
for k in REAL:
    r._auto_result(res(k), r.auto_gen)
sig = r.signal
check("engine: a decoder is started at most twice while it flip-flops (it used to swap constantly)", len(starts) <= 2, starts)
check("engine: the screen shows 'Unidentified signal'", sig and sig["kind"] == UNSETTLED, (sig, starts, len(stops)))
# ---- a real DSD-FME lock beats "Unidentified signal" (the 17:15 log: a Harper Adams DMR repeater, colour code 1)
def flip_with(dsd_locked):
    r.decoders.active, r.decoders.sub = "dsd", "auto"
    r.auto_owned = True
    r.decoders.dsd_recent_sync = lambda n: dsd_locked
    r.classifier = SignalClassifier()
    r.auto_gen += 1
    n0, s0 = len(starts), len(stops)
    for k in REAL:
        r._auto_result(res(k), r.auto_gen)
    return r.signal, len(starts) - n0, len(stops) - s0


sig_l, st_l, sp_l = flip_with(True)
check("locked DSD-FME: the screen says digital voice, decoder locked", sig_l and sig_l["kind"] == "Digital voice" and "locked" in sig_l["label"], sig_l)
check("locked DSD-FME: that decoder is never restarted or stopped", st_l == 0, (st_l, sp_l))
sig_u, st_u, sp_u = flip_with(False)
check("no lock: it still says 'Unidentified signal'", sig_u and sig_u["kind"] == UNSETTLED, sig_u)
r.stop()
print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
