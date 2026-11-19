import os, sys, wave, io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import time, numpy as np
from collections import deque
from andysdr import engine
E = engine.Radio if hasattr(engine, "Radio") else None
cls = [v for k, v in vars(engine).items() if isinstance(v, type) and hasattr(v, "replay_pcm")][0]
r = cls.__new__(cls); r.replay = deque(maxlen=2400)
blk = (np.ones(2400, "<i2") * 1000).tobytes(); now = time.time(); ok = bad = 0
def check(n, c):
    global ok, bad
    print(("PASS " if c else "FAIL ") + n); ok += bool(c); bad += (not c)
for i in range(20): r.replay.append((now - 40 + i * 0.05, blk, 100e6, "NFM"))      # 1 s of speech 40 s ago
for i in range(40): r.replay.append((now - 5 + i * 0.05, blk, 101e6, "NFM"))       # 2 s of speech 5 s ago
check("30 s window holds only the recent transmission", len(r.replay_pcm(30)[0]) == 40 * 2400 * 2)
got = r.replay_pcm(60)
check("60 s window holds both, with a gap between", len(got[0]) == (60 * 2400 + 24000) * 2 and got[1] == 101e6)
e = cls.__new__(cls); e.replay = deque()
check("nothing heard -> None", e.replay_pcm(30) is None)
w = wave.open(io.BytesIO(r.replay_wav(60)), "rb")
check("valid wav", w.getframerate() == 48000 and w.getnchannels() == 1)
print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
