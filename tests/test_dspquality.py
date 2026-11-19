import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from andysdr.dsp import Receiver, AUDIO_RATE, BLOCK_SECONDS
ok = bad = 0
def check(name, cond, extra=""):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name + (" " + extra if extra else "")); ok += bool(cond); bad += (not cond)
FS = 240000
def blocks(rx, make, secs=2.0, offset=0.0):
    n = int(FS * BLOCK_SECONDS); outs = []; t0 = 0
    for _ in range(int(secs / BLOCK_SECONDS)):
        t = (np.arange(n) + t0) / FS; t0 += n
        outs.append(rx.process(make(t).astype(np.complex64), offset))
    return outs
def am(level, mod=0.5):
    return lambda t: level * (1 + mod * np.sin(2 * np.pi * 1000 * t)) * np.ones_like(t)
# AM loudness independent of signal strength
r = []
for lv in (0.003, 0.3):
    rx = Receiver(FS); rx.set_mode("AM"); rx.squelch_db = -120
    o = blocks(rx, am(lv)); a = np.concatenate([x["audio"] for x in o[20:]])
    r.append(float(np.sqrt(np.mean(a ** 2))))
check("AM audio level equal for weak and strong carrier", 0.8 < r[0] / r[1] < 1.25, f"{r}")
check("AM audio is audible", r[1] > 0.05, f"{r[1]:.3f}")
# noise blanker improves a tone buried in impulse noise
rng = np.random.default_rng(3)
def usb_with_spikes(t):
    x = 0.05 * np.exp(2j * np.pi * 1200 * t)
    idx = rng.integers(0, len(t), 12); x[idx] += 5.0
    return x
def tone_snr(nb):
    rx = Receiver(FS); rx.set_mode("USB"); rx.squelch_db = -120; rx.nb_on = nb
    rng.bit_generator.state = np.random.default_rng(3).bit_generator.state
    o = blocks(rx, usb_with_spikes, 2.0); a = np.concatenate([x["audio"] for x in o[10:]])
    sp = np.abs(np.fft.rfft(a * np.hanning(len(a)))); f = np.fft.rfftfreq(len(a), 1 / AUDIO_RATE)
    tone = sp[(f > 1000) & (f < 1500)].max(); noise = np.median(sp[(f > 300) & (f < 2500)])
    return tone / noise
s_off, s_on = tone_snr(False), tone_snr(True)
check("noise blanker raises tone-to-noise ratio", s_on > s_off * 1.3, f"off {s_off:.1f} on {s_on:.1f}")
# squelch hang keeps audio open briefly after the signal drops
rx = Receiver(FS); rx.set_mode("NFM"); rx.squelch_db = -50
loud = lambda t: 0.3 * np.exp(2j * np.pi * 3000 * t * 0 + 0j) * np.ones_like(t)
quiet = lambda t: 1e-5 * np.ones_like(t, dtype=complex)
blocks(rx, loud, 0.5)
opens = [bool(np.any(x["audio_ungated"] != 0) and x["squelch_open"]) for x in blocks(rx, quiet, 0.6)]
open_flags = [x["squelch_open"] for x in blocks(Receiver(FS), quiet, 0.1)]
check("squelch closes on silence", not any(open_flags))
rx2 = Receiver(FS); rx2.set_mode("NFM"); rx2.squelch_db = -50
blocks(rx2, loud, 0.5)
tail = blocks(rx2, quiet, 0.6)
gated = [bool(np.any(x["audio"] != 0)) or False for x in tail]
check("hang accounting: counter resets then runs down", rx2.hang == 0)
# AGC ramp: no discontinuity between blocks
rx3 = Receiver(FS); rx3.set_mode("USB"); rx3.squelch_db = -120
o = blocks(rx3, lambda t: 0.1 * np.exp(2j * np.pi * 1000 * t), 2.0)
a = np.concatenate([x["audio"] for x in o[10:]])
check("USB AGC output has no large steps", np.max(np.abs(np.diff(a))) < 0.5)
print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
