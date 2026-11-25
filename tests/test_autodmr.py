"""Auto-identify on a REAL recording: one second of a DMR repeater's data burst, recorded with AndySDR on 165.050 MHz
(25 Nov 2026, Harper Adams University repeater, colour code 1; 141 exact BS-data sync words were found in 4.6 s of it).
Before 1.22.6 the classifier called it 'Pager' (a weak 1200 Hz line from DMR's frame structure was mistaken for a 1200 Bd
fundamental) or 'FM voice', and Auto kept swapping decoders.  The data is the 48 kS/s IQ and audio the classifier is given."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402

from andysdr.autodetect import SignalClassifier  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if extra and not cond else ""))
    ok = ok and bool(cond)


z = np.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dmr_165050_burst_1s.npz"))
iq = (z["iq"][0::2].astype(np.float32) + 1j * z["iq"][1::2].astype(np.float32)) * float(z["scale"])
audio = z["audio"].astype(np.float32)
c = SignalClassifier()
r = c.classify(iq.astype(np.complex64), audio, "DIG", 165.05e6)
check("real DMR repeater burst is read as a 4800 Bd digital voice signal (P25 / DMR)", r and r["kind"] == "P25 / DMR", r)
check("... with the DSD-FME decoder", r and r["decoder"] == "dsd" and r["mode"] == "DIG", r)
check("... and the symbol rate it found is 4800 Bd", r and r.get("baud") == 4800, r and r.get("baud"))
c = SignalClassifier()
seq = [c.decide(c.classify(iq.astype(np.complex64), audio, "DIG", 165.05e6)) for _ in range(5)]
check("five seconds of it in a row: steady and stable", all(s["kind"] == "P25 / DMR" for s in seq) and seq[-1]["stable"], [s["kind"] for s in seq])

# a genuine 1200 Bd 2FSK pager signal must still be a pager (its 2400 Hz harmonic is exactly one octave up)
fs = 48000
rng = np.random.default_rng(3)
bits = rng.integers(0, 2, 1200)
sym = np.repeat(bits * 2.0 - 1.0, fs // 1200)
ph = 2 * np.pi * np.cumsum(sym * 4500.0) / fs
sig = np.exp(1j * ph) + (rng.normal(size=len(ph)) + 1j * rng.normal(size=len(ph))) * 0.03
aud = np.diff(np.unwrap(np.angle(sig)), prepend=0).astype(np.float32) * 0.1
r2 = SignalClassifier().classify(sig.astype(np.complex64), aud, "NFM", 153.35e6)
check("a clean 1200 Bd 2FSK signal is still a pager", r2 and r2["kind"] == "Pager", r2)
print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
