"""TETRAPOL: GMSK demodulator, frame-sync / polarity detection, automatic identification and the message summariser.
The decoder program (tetrapol_dump from Tetrapol-kit) is not needed here; an end-to-end check runs only if a
TETRAPOL_DUMP env var points to one."""
import os, shutil, subprocess, sys, tempfile
os.environ["HOME"] = os.environ["APPDATA"] = tempfile.mkdtemp()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import numpy as np
from scipy import signal
from andysdr import tetrapol, decoders
from andysdr.autodetect import SignalClassifier

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n, "" if ok else extra)
    fails += 0 if ok else 1

FS = 48000
BITS = tetrapol.make_frames(120, seed=5)


def run(iq, block=2400):
    d = tetrapol.TetrapolDemod(FS)
    out = [d.process(iq[i:i + block]) for i in range(0, len(iq), block)]
    return d, np.concatenate(out)


def ber_aligned(rx, tx):
    """Cross-correlate to find the alignment, then count errors."""
    rx = rx[len(rx) // 4:len(rx) // 4 + 6000]
    a = rx.astype(float) * 2 - 1
    t = tx.astype(float) * 2 - 1
    c = np.correlate(t, a, "valid")
    k = int(np.argmax(np.abs(c)))
    seg = tx[k:k + len(rx)]
    e = float(np.mean(rx != seg))
    return min(e, 1 - e)


# ---- frame-sync search ----------------------------------------------------------------------------------------
fs0 = tetrapol.find_sync(BITS)
check("find_sync finds the 160-bit frame pattern", fs0 is not None and fs0[0] == 0 and fs0[1] >= 100, str(fs0))
sh = tetrapol.find_sync(BITS[37:])
check("find_sync gives the frame offset", sh is not None and sh[0] == (160 - 37) % 160, str(sh))
check("find_sync sees the pattern in a mirrored stream only after flipping", tetrapol.find_sync(BITS ^ 1) is None
      and tetrapol.find_sync((BITS ^ 1) ^ 1) is not None)
rng = np.random.default_rng(3)
check("find_sync ignores random bits", all(tetrapol.find_sync(rng.integers(0, 2, 3200).astype(np.uint8)) is None
                                            for _ in range(20)))

# ---- demodulator ----------------------------------------------------------------------------------------------
for name, kw in (("clean", {}), ("+2.5 kHz offset", {"cfo": 2500}), ("-1.8 kHz offset", {"cfo": -1800}),
                 ("+25 ppm clock", {"ppm": 25}), ("-25 ppm clock", {"ppm": -25}), ("noise 0.08", {"noise": 0.08, "cfo": 700})):
    iq = tetrapol.modulate(BITS, **kw)
    d, rx = run(iq)
    e = ber_aligned(rx, BITS if not d.invert else BITS)
    check(f"demod BER < 1 % ({name})", e < 0.01 and d.synced, f"ber={e:.3f} synced={d.synced}")

# a mirrored spectrum (conjugated IQ) is detected and corrected
iq = np.conj(tetrapol.modulate(BITS, cfo=500))
d, rx = run(iq)
check("mirrored spectrum is detected", d.synced and d.invert)
check("mirrored spectrum output is corrected (BER < 1 %)", ber_aligned(rx, BITS) < 0.01)

# one tiny block at a time (as the engine feeds it: 50 ms)
iq = tetrapol.modulate(BITS, cfo=-900)
d, rx = run(iq, block=2400)
d2, rx2 = run(iq, block=1000)
check("block size does not matter", d2.synced and ber_aligned(rx2, BITS) < 0.01)

# ---- probe ----------------------------------------------------------------------------------------------------
iq1 = tetrapol.modulate(BITS, cfo=600, noise=0.05)[:FS]
p = tetrapol.probe(iq1)
check("probe finds Tetrapol in 1 s of IQ", p is not None and p["frames"] >= 6, str(p))
noise = (rng.standard_normal(FS) + 1j * rng.standard_normal(FS)).astype(np.complex64) * 0.3
check("probe: noise only -> nothing", tetrapol.probe(noise) is None)
a = signal.lfilter(*signal.butter(2, 3000, fs=FS), rng.standard_normal(FS)); a /= np.abs(a).max()
fm = (0.5 * np.exp(1j * np.cumsum(a * 2 * np.pi * 3000 / FS))).astype(np.complex64)
check("probe: FM voice-like signal -> nothing", tetrapol.probe(fm) is None)
# other GMSK/FSK data at 8000 Bd that does NOT carry the Tetrapol frame pattern
other = tetrapol.modulate(rng.integers(0, 2, 8000 * 3).astype(np.uint8))[:FS]
check("probe: 8000 Bd data without the frame sync -> nothing", tetrapol.probe(other) is None)
# 4800 Bd 4FSK (DMR / P25 like)
sym = rng.integers(0, 4, 4800)
lv = np.array([-1800, -600, 600, 1800.0])[sym]
f = np.repeat(lv, 10)[:FS]
fsk = (0.5 * np.exp(1j * np.cumsum(2 * np.pi * f / FS))).astype(np.complex64)
check("probe: 4FSK 4800 Bd -> nothing", tetrapol.probe(fsk) is None)

# ---- automatic identification ---------------------------------------------------------------------------------
def classify(iq, mode="FM", freq=380.0e6, audio=None):
    c = SignalClassifier()
    return c.classify(iq, np.zeros(len(iq), np.float32) if audio is None else audio, mode, freq)

r = classify(tetrapol.modulate(BITS, cfo=400, noise=0.03)[:FS])
check("auto-ID: Tetrapol is recognised", r and r["kind"] == "TETRAPOL" and r["decoder"] == "tetrapol" and r["mode"] == "DIG", str(r))
r = classify(np.conj(tetrapol.modulate(BITS, cfo=-1200, noise=0.03)[:FS]))
check("auto-ID: mirrored Tetrapol is recognised", r and r["kind"] == "TETRAPOL", str(r))
r = classify(tetrapol.modulate(BITS)[:FS], mode="AM")
check("auto-ID: not claimed in AM", not r or r["kind"] != "TETRAPOL")
r = classify(fm, audio=a.astype(np.float32))
check("auto-ID: FM voice is not Tetrapol", not r or r["kind"] != "TETRAPOL", str(r))
r = classify(fsk)
check("auto-ID: 4FSK 4800 Bd is not Tetrapol", not r or r["kind"] != "TETRAPOL", str(r))
r = classify(other)
check("auto-ID: other 8000 Bd data is not Tetrapol", not r or r["kind"] != "TETRAPOL", str(r))
r = classify(noise)
check("auto-ID: noise is not Tetrapol", not r or r["kind"] != "TETRAPOL", str(r))

# ---- message summariser (what tetrapol_dump prints) -----------------------------------------------------------
logs = []
dm = decoders.DecoderManager({}, lambda k, p: logs.append((k, p)))
dm.log = lambda text, level="info": logs.append(text)
dm.tetrapol_state = {"t0": 0, "cur": None, "kv": {}, "said": {}, "count": {}}
dm.tetrapol_demod = tetrapol.TetrapolDemod(FS)
for ln in ("CODOP=0x90 (D_SYSTEM_INFO)", "    COUNTRY_CODE=208", "    NETWORK=1", "    LOC_ID=12", "    CELL_ID=7",
           "    U_CH_SCRAMBLING=1", "CODOP=0x3a (D_GROUP_ACTIVATION)", "    ORIGIN=5", "    ID=42",
           "CODOP=0x12 (D_NEIGHBOURING_CELL)", "    CCR_CONFIG=3", "CODOP=0x01 (D_SOMETHING_ELSE)"):
    dm._tetrapol_line(ln)
dm._tetrapol_flush()
txt = "\n".join(str(x) for x in logs)
check("summary: cell information is reported", "country code 208" in txt and "network 1" in txt and "cell 7" in txt, txt)
check("summary: call signalling is reported", "D_GROUP_ACTIVATION" in txt and "ID=42" in txt, txt)
check("summary: neighbour cells are reported", "neighbour cells listed: 3" in txt, txt)
check("summary: other messages are named", "D_SOMETHING_ELSE" in txt, txt)
n0 = len(logs)
dm._tetrapol_line("CODOP=0x90 (D_SYSTEM_INFO)")
dm._tetrapol_line("    COUNTRY_CODE=208")
dm._tetrapol_flush()
check("summary: repeated cell info is not repeated in the log", len(logs) == n0, str(logs[n0:]))

# ---- whole chain with a stand-in decoder program (POSIX only): IQ -> demod -> pipe -> lines -> summary ---------------
if sys.platform != "win32":
    import stat, time
    fake = os.path.join(tempfile.mkdtemp(), "tetrapol_dump")
    with open(fake, "w") as fh:
        fh.write("#!%s\nimport sys\nn = 0\nsent = False\nwhile True:\n    b = sys.stdin.buffer.read1(4096)\n    if not b: break\n"
                 "    n += len(b)\n    if n > 1500 and not sent:\n        sent = True\n"
                 "        print('CODOP=0x90 (D_SYSTEM_INFO)', flush=True)\n        print('    COUNTRY_CODE=208', flush=True)\n"
                 "        print('    NETWORK=3', flush=True)\n        print('    LOC_ID=9', flush=True)\n"
                 "        print('    CELL_ID=4', flush=True)\n        print('CODOP=0x0c (D_GROUP_ACTIVATION)', flush=True)\n" % sys.executable)
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
    clog = []
    cm = decoders.DecoderManager({"decoder_paths": {"tetrapol_dump": fake}}, lambda k, p: clog.append(p))
    cm.freq_fn = lambda: 380e6
    check("chain: decoder starts", cm.start("tetrapol", "") and cm.active == "tetrapol")
    iqc = tetrapol.modulate(BITS, cfo=500)
    zero = np.zeros(2400, np.float32)
    for i in range(0, len(iqc) - 2400, 2400):
        cm.feed({"iq48": iqc[i:i + 2400], "audio_ungated": zero, "disc": zero})
        time.sleep(0.005)
    time.sleep(1.0)
    cm.feed({"iq48": iqc[:2400], "audio_ungated": zero, "disc": zero})
    cm.stop()
    ctxt = "\n".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in clog)
    check("chain: frame sync is announced", "frame sync found" in ctxt, ctxt)
    check("chain: cell info from the decoder program reaches the log", "country code 208" in ctxt and "cell 4" in ctxt, ctxt)

mlog = []
mm = decoders.DecoderManager({"decoder_paths": {}}, lambda k, p: mlog.append((k, p)))
_find = decoders.find_exe
decoders.find_exe = lambda base, ov=None: None
check("missing tetrapol_dump: start() fails cleanly (so Auto ID can report it)", mm.start("tetrapol", "") is False and mm.active is None)
decoders.find_exe = _find

# ---- end to end, if a Tetrapol-kit build is around -------------------------------------------------------------
EXE = os.environ.get("TETRAPOL_DUMP") or shutil.which("tetrapol_dump")
if EXE:
    d, rx = run(tetrapol.modulate(BITS, cfo=300))
    pr = subprocess.run([EXE, "-i", "-", "-b", "UHF", "-t", "CCH", "-d", "DOWN"], input=rx.tobytes(),
                        capture_output=True, timeout=60)
    out = (pr.stdout + pr.stderr).decode("latin1")
    check("tetrapol_dump runs on our bit stream (no crash)", pr.returncode in (0, 1), out[-300:])
    check("tetrapol_dump itself finds the frame sync in our demodulated bits", "Frame sync found" in out, out[-300:])
else:
    print("SKIP end-to-end (no tetrapol_dump)")

print("\n%d failure(s)" % fails)
sys.exit(1 if fails else 0)
