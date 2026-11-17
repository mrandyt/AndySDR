"""TETRA: synthetic downlink -> TetraDemod -> osmo-tetra tetra-rx.  Needs a tetra-rx binary
(TETRA_RX env var, or built by decoders_src/tetra/build.sh); skipped if there is none."""
import os, shutil, subprocess, sys, tempfile, time
os.environ["HOME"] = os.environ["APPDATA"] = tempfile.mkdtemp()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import numpy as np
from scipy import signal
from andysdr.tetrademod import TetraDemod, rrc_taps
from andysdr import decoders

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n, "" if ok else extra)
    fails += 0 if ok else 1

RX = os.environ.get("TETRA_RX") or next((p for p in ("/tmp/tx-linux/tetra-rx",) if os.path.exists(p)), shutil.which("tetra-rx"))
burst = np.fromfile(os.path.join(HERE, "data", "tetra_sync_burst.bin"), np.uint8)     # one real SYNC burst (osmo-tetra's own encoder)
check("fixture burst is 510 bits", len(burst) == 510)

def modulate(bits, off_hz=0.0, snr_db=30.0, seed=0):
    pairs = bits.reshape(-1, 2)
    step = np.array([1, 3, -1, -3])[pairs[:, 0] * 2 + pairs[:, 1]] * (np.pi / 4)     # 00:+pi/4 01:+3pi/4 10:-pi/4 11:-3pi/4
    sym = np.exp(1j * np.cumsum(step))
    up = np.zeros(len(sym) * 8, complex); up[::8] = sym
    x = signal.resample_poly(signal.lfilter(rrc_taps(144000), [1], up), 1, 3)
    x = x * np.exp(2j * np.pi * off_hz / 48000 * np.arange(len(x)))
    p = np.mean(np.abs(x) ** 2)
    rng = np.random.default_rng(seed)
    return (x + (rng.normal(size=len(x)) + 1j * rng.normal(size=len(x))) * np.sqrt(p / 10 ** (snr_db / 10) / 2)).astype(np.complex64)

bits = np.tile(burst, 4 * 40)
x = modulate(bits, off_hz=1500, snr_db=15)
d = TetraDemod()
steps = np.concatenate([d.process(x[i:i + 2400]) for i in range(0, len(x), 2400)])
check("about 18 000 symbols per second", abs(len(steps) - len(x) * 18000 / 48000) < 40, len(steps))
check("carrier offset tracked (1500 Hz)", abs(d.freq - 1500) < 30, d.freq)
nearest = np.where(np.abs(steps) > 2, 3.0, 1.0) * np.sign(steps)
check("symbols sit on +-1 / +-3", float(np.mean(np.abs(steps[2000:] - nearest[2000:]))) < 0.35, np.mean(np.abs(steps - nearest)))

if not RX:
    print("SKIP tetra-rx not available - end-to-end checks skipped")
else:
    lines = []
    class M(decoders.DecoderManager):
        pass
    got = []
    m = M({"decoder_paths": {"tetra-rx": RX}}, lambda k, v: got.append((k, v)))
    check("tetra decoder starts", m.start("tetra", "") is True, got)
    for i in range(0, len(x), 2400):
        m.feed({"iq48": x[i:i + 2400], "audio_ungated": np.zeros(2400, np.float32), "disc": np.zeros(2400, np.float32)})
        time.sleep(0.002)
    time.sleep(1.5)
    m.stop()
    texts = [v.get("text", "") for k, v in got if isinstance(v, dict)]
    check("network found: MCC 262 / MNC 42", any("MCC 262" in t and "MNC 42" in t for t in texts), texts[-5:])
    check("sync lines are not spammed", sum("TETRA network found" in t for t in texts) == 1)
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
