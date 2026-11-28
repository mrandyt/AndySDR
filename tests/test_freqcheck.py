"""Measuring how far off frequency a receiver really is, and saying which kind of error it is.

The measurement is tested against synthetic spectra with a known offset built in - that is the only way to know
the answer. The verdicts are tested against the three cases that matter and look alike from a single reading: a
crystal that is a little fast, an orbit prediction that is out, and a pass too weak to tell.
"""
import math, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
from andysdr import freqcheck as F

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


def spectrum(center, fs, sig_hz, half_bw, snr_db=14.0, n=2048, seed=1, floor_db=-95.0):
    """One spectrum frame in dB: noise, plus a flat-topped carrier `half_bw` wide centred on `sig_hz`."""
    rng = np.random.default_rng(seed)
    hz = fs / n
    f = (center - fs / 2.0) + np.arange(n) * hz
    noise = 10.0 ** (floor_db / 10.0) * (0.6 + 0.8 * rng.random(n))
    band = np.abs(f - sig_hz) <= half_bw
    sig = np.zeros(n)
    sig[band] = 10.0 ** ((floor_db + snr_db) / 10.0)
    # soften the edges the way a real filtered carrier has them
    edge = np.abs(f - sig_hz)
    skirt = (edge > half_bw) & (edge < half_bw * 1.25)
    sig[skirt] = 10.0 ** ((floor_db + snr_db * 0.35) / 10.0)
    return 10.0 * np.log10(noise + sig)


FS, CENTRE, HALF = 1_200_000.0, 138_201_920.0, 60_000.0
TUNED = 137_900_000.0

# --- the measurement itself -------------------------------------------------------------------------------
for truth in (0.0, 500.0, -500.0, 1400.0, -1400.0, 4000.0, -4000.0, 12000.0):
    s = spectrum(CENTRE, FS, TUNED + truth, HALF)
    got = F.carrier_offset(s, CENTRE, FS, TUNED, HALF)
    ok = got is not None and abs(got - truth) < 350.0
    check(f"an offset of {truth:+.0f} Hz is measured as {got if got is None else round(got)}", ok, (truth, got))

check("a bin is about 586 Hz, so it is reading finer than one bin",
      abs(F.carrier_offset(spectrum(CENTRE, FS, TUNED + 300.0, HALF), CENTRE, FS, TUNED, HALF) - 300.0) < 350.0)

# --- it refuses when there is nothing to measure ---------------------------------------------------------------
noise_only = 10.0 * np.log10(10.0 ** (-9.5) * (0.6 + 0.8 * np.random.default_rng(2).random(2048)))
check("pure noise gives no reading", F.carrier_offset(noise_only, CENTRE, FS, TUNED, HALF) is None)
weak = spectrum(CENTRE, FS, TUNED, HALF, snr_db=1.0)
check("a signal buried in the noise gives no reading", F.carrier_offset(weak, CENTRE, FS, TUNED, HALF) is None)
check("a spectrum too short to mean anything gives no reading",
      F.carrier_offset(np.zeros(64), CENTRE, FS, TUNED, HALF) is None)
check("a signal at the very edge of the capture is not guessed at",
      F.carrier_offset(spectrum(CENTRE, FS, CENTRE + FS / 2 - 1000, HALF), CENTRE, FS, CENTRE + FS / 2 - 1000, HALF)
      is None)

# --- a crystal that is a little fast ------------------------------------------------------------------------------
# A 10 ppm receiver at 137.9 MHz is 1379 Hz out, the same at every point of the pass.
t = F.Tracker(TUNED, "rx2")
for i in range(20):
    dop = 3000.0 * math.cos(i / 19.0 * math.pi)        # the real Doppler, big at the ends, zero in the middle
    t.add(i * 20.0, 1379.0 + (i % 3 - 1) * 40.0, el=10 + i, doppler=dop)
r = t.result()
check("a steady offset is called the oscillator", r and r["verdict"] == "oscillator", r)
check("...with the right ppm", r and abs(r["ppm"] - 10.0) < 0.5, r and r["ppm"])
check("...and it says what to set", r and "+10.0 ppm" in r["why"] and "-10.0 ppm" in r["why"], r and r["why"])
check("...and what it costs at 1090 MHz", r and "10900 Hz" in r["why"].replace("+", ""), r and r["why"])

# --- an orbit prediction that is out ------------------------------------------------------------------------------
# The error is a fraction of the Doppler shift, so it swings with the pass instead of staying put.
t2 = F.Tracker(TUNED, "rx1")
for i in range(20):
    dop = 3000.0 * math.cos(i / 19.0 * math.pi)
    t2.add(i * 20.0, dop * 0.25, el=10 + i, doppler=dop)
r2 = t2.result()
check("an error that swings with the pass is blamed on the orbit", r2 and r2["verdict"] == "orbit", r2)
check("...and says a ppm correction would not help", r2 and "would not fix it" in r2["why"], r2 and r2["why"])

# --- a receiver that is simply right ------------------------------------------------------------------------------
t3 = F.Tracker(TUNED, "rx2")
for i in range(20):
    t3.add(i * 20.0, (i % 5 - 2) * 25.0, el=10 + i, doppler=3000.0 * math.cos(i / 19.0 * math.pi))
r3 = t3.result()
check("a receiver on frequency is left alone", r3 and r3["verdict"] == "good", r3)
check("...and nothing is suggested", r3 and "not worth correcting" in r3["why"], r3 and r3["why"])

# --- a pass too weak to tell --------------------------------------------------------------------------------------
t4 = F.Tracker(TUNED, "rx2")
rng = np.random.default_rng(7)
for i in range(20):
    t4.add(i * 20.0, float(rng.normal(0, 2500)), el=10 + i, doppler=None)
r4 = t4.result()
check("scattered readings are not turned into a correction", r4 and r4["verdict"] == "unsteady", r4)

# --- it will not conclude from two readings -------------------------------------------------------------------------
t5 = F.Tracker(TUNED, "rx2")
t5.add(0, 1379.0); t5.add(20, 1379.0)
check("two readings conclude nothing", t5.result() is None)
check("readings that failed are not counted", (lambda: (t5.add(40, None), t5.add(60, float('nan')),
                                                        len(t5.rows) == 2)[-1])())

# --- the whole way through, from spectra to a verdict ------------------------------------------------------------
# A pass as it really arrives: the Doppler moves the carrier through the pass, the receiver follows it, and the
# crystal is 8 ppm fast on top. The tracker must find the crystal and not be fooled by the Doppler.
PPM = 8.0
t6 = F.Tracker(TUNED, "rx2")
for i in range(16):
    dop = 3200.0 * math.cos(i / 15.0 * math.pi)
    tuned = TUNED + dop                                 # where the radio has tuned, Doppler already allowed for
    real = tuned + TUNED * PPM / 1e6                    # where the carrier actually turns up: the crystal is fast
    s = spectrum(CENTRE, FS, real, HALF, snr_db=16.0, seed=i)
    off = F.carrier_offset(s, CENTRE, FS, tuned, HALF)
    t6.add(i * 25.0, off, el=12 + i * 3, doppler=dop)
r6 = t6.result()
check("from real spectra: a fast crystal is found", r6 and r6["verdict"] == "oscillator", r6)
check("...at about the right ppm", r6 and abs(r6["ppm"] - PPM) < 1.0, r6 and r6["ppm"])
check("...and every reading was usable", r6 and r6["n"] == 16, r6 and r6["n"])
check("a sentence comes out of it", "frequency check" in F.sentence(r6) and "ppm" in F.sentence(r6), F.sentence(r6))
check("nothing to say about nothing", F.sentence(None) == "")

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
