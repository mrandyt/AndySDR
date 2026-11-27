"""Auto ID regression benchmark on synthetic signals (see autoid_signals.py): every kind of signal at several strengths,
with hard limits on the mistakes that matter most - speech taken for a digital signal (it would start a decoder and
switch the mode), a digital signal taken for the wrong system, and a carrier with nobody talking being given a mode."""
import os, sys, collections, warnings
warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import numpy as np
import autoid_signals as G
from andysdr.autodetect import SignalClassifier

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    if not ok: fails.append(name)

DIGITAL = {"DMR", "P25 / DMR", "Digital 4FSK", "NXDN / dPMR", "dPMR", "Pager", "TETRA", "TETRAPOL", "APRS", "SSTV", "WEFAX", "DTMF"}
def read(gen, snr, seed):
    rng = np.random.default_rng(seed)
    iq, audio, mode, freq, truth = gen(rng, snr)
    r = SignalClassifier().classify(iq, audio, mode, freq)
    return (r["kind"] if r else None), (r.get("mode") if r else None), truth

# 1. speech is never taken for a digital signal (FM with and without a sub-tone, loud FM, AM, both sidebands)
voice_cases = [lambda r, s: G.fm_voice(r, s), lambda r, s: G.fm_voice(r, s, ctcss=100.0), lambda r, s: G.fm_voice(r, s, dev=5000.0),
               lambda r, s: G.am_voice(r, s), lambda r, s: G.ssb_voice(r, s, "USB"), lambda r, s: G.ssb_voice(r, s, "LSB")]
bad = collections.Counter(); n = 0
for snr in (9, 12, 16, 22, 30):
    for t in range(25):
        for g in voice_cases:
            k, m, tr = read(g, snr, 8000 + t * 31 + snr); n += 1
            if k in DIGITAL: bad[k] += 1
check(f"speech never read as a digital signal ({n} readings)", not bad, dict(bad))

# 2. every signal kind, right at good strength, wrong rarely at weaker strength
good = tot = 0; misses = []
for name, gen in G.CASES:
    for snr, need in ((30, 1.0), (22, 1.0), (16, 0.75)):
        ok = 0; trials = 6
        for t in range(trials):
            k, m, truth = read(gen, snr, 100 * t + snr + len(name))
            if k == truth or (truth in ("AM voice", "SSB voice") and k == "FM voice" and m is None and snr < 20):
                ok += 1
        tot += 1; good += ok / trials >= need
        if ok / trials < need: misses.append((name, snr, f"{ok}/{trials}"))
check(f"every signal kind identified at 16/22/30 dB ({good}/{tot} cells)", not misses, misses)

# 3. the analogue mode is named correctly when the evidence is clear
for nm, gen, want in (("FM", G.fm_voice, "NFM"), ("AM", G.am_voice, "AM"), ("USB", lambda r, s: G.ssb_voice(r, s, "USB"), "USB"), ("LSB", lambda r, s: G.ssb_voice(r, s, "LSB"), "LSB")):
    c = collections.Counter(read(gen, 24, 5000 + t)[1] for t in range(12))
    check(f"{nm} voice at 24 dB names mode {want}", c[want] >= 11, dict(c))

# 4. a carrier with nobody talking is never given a mode (it could be FM or AM)
rng = np.random.default_rng(7)
for label, mk in (("steady carrier", lambda: np.exp(1j * 2 * np.pi * 300 * np.arange(G.N) / G.FS)),
                  ("carrier with a little noise", lambda: np.exp(1j * 2 * np.pi * 300 * np.arange(G.N) / G.FS) + 0.1 * G._noise(rng, G.N))):
    modes = set()
    for t in range(6):
        iq = (mk() + 0.03 * G._noise(rng, G.N)).astype(np.complex64)
        r = SignalClassifier().classify(iq, G._fm_demod(iq), "NFM", 150e6)
        modes.add((r or {}).get("mode"))
    check(f"{label}: no mode named", modes <= {None}, modes)

# 5. the weak-signal FM voice false alarms measured before the kurtosis check (4 % of readings) are gone
c = 0
for t in range(120):
    k, m, _ = read(lambda r, s: G.fm_voice(r, s, ctcss=100.0), 18 if t % 2 else 32, 9000 + t)
    c += k in DIGITAL
check("FM voice with a sub-tone: no false digital readings in 120 runs", c == 0, c)
print("ALL OK" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
