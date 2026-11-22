import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys
import numpy as np
from andysdr.dsp import AudioNotch, AUDIO_RATE as FS

def rms_db(x, f):
    X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    k = int(round(f * len(x) / FS))
    return 20 * np.log10(X[k - 2:k + 3].max() + 1e-9)

fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

rng = np.random.default_rng(1)
t = np.arange(FS * 6) / FS
speech = 0.15 * rng.standard_normal(len(t)) * (1 + np.sin(2 * np.pi * 3 * t)) / 2
tone = 0.3 * np.sin(2 * np.pi * 1234 * t)
sig = (speech + tone).astype(np.float32)

def run(n, x):
    out = []
    for i in range(0, len(x), 1024):
        out.append(n.process(x[i:i + 1024]))
    return np.concatenate(out)

n = AudioNotch(FS); n.configure(False, 1000, 20, True)
o = run(n, sig)
tail_in, tail_out = sig[-FS:], o[-FS:]
check("auto notch finds whistle", any(abs(f - 1234) < 40 for f in n.active))
check("auto notch cuts whistle >15 dB", rms_db(tail_in, 1234) - rms_db(tail_out, 1234) > 15)

n = AudioNotch(FS); n.configure(False, 1000, 20, True)
o = run(n, speech.astype(np.float32))
check("speech-like audio gets no notch", n.active == [])
check("speech passes unchanged", abs(np.std(o) / np.std(speech) - 1) < 0.02)

n = AudioNotch(FS); n.configure(True, 1234, 20, False)
o = run(n, sig)
check("manual notch cuts tone", rms_db(sig[-FS:], 1234) - rms_db(o[-FS:], 1234) > 15)

n = AudioNotch(FS); n.configure(False, 1000, 20, False)
check("disabled -> not enabled", not n.enabled)

import tempfile, time
os.environ["APPDATA"] = os.environ["HOME"] = tempfile.mkdtemp()
from andysdr.config import Settings
from andysdr.engine import Radio
st = Settings(); st["device"] = "sim"
r = Radio(st); r.start(); time.sleep(1.5)
r.set_notch(on=True, hz=900, q=40, auto=True); time.sleep(0.8)
n = r.state()["notch"]
check("engine state notch", n["on"] and n["hz"] == 900 and n["q"] == 40 and n["auto"])
check("receiver notch configured", r.rx.notch.enabled)
r.set_hotspot(name='Desk', freq=435000000, cc=3, ts=1, tg='91'); time.sleep(0.8)
h = r.state()['hotspot']
check('hotspot saved', h['name']=='Desk' and h['freq']==435000000 and h['cc']==3 and h['ts']==1 and h['tg']=='91')
r.stop()
sys.exit(1 if fails else 0)
