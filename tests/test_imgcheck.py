import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from PIL import Image
from andysdr import imgcheck
d = tempfile.mkdtemp(); ok = 0; bad = 0
def check(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name); ok += cond; bad += (not cond)
Image.fromarray(np.zeros((300, 400), np.uint8)).save(d + "/black.png")
Image.fromarray(np.full((300, 400, 3), 255, np.uint8)).save(d + "/white.png")
rng = np.random.default_rng(1)
Image.fromarray(rng.integers(0, 255, (300, 400), dtype=np.uint8)).save(d + "/noise.png")
g = np.zeros((300, 400), np.uint8); g[100:200, 50:300] = 180
Image.fromarray(g).save(d + "/pic.png")
check("black is blank", imgcheck.is_blank(d + "/black.png"))
check("white is blank", imgcheck.is_blank(d + "/white.png"))
check("noise is kept", not imgcheck.is_blank(d + "/noise.png"))
check("picture with black borders is kept", not imgcheck.is_blank(d + "/pic.png"))
check("discard removes file", imgcheck.discard_if_blank(d + "/black.png") and not os.path.exists(d + "/black.png"))
check("missing file is not 'blank'", not imgcheck.is_blank(d + "/nope.png"))
print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
