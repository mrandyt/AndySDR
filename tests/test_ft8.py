"""FT8 / FT4: band-plan matching, slot cutting, decode_ft8 wrapper (stand-in executable), engine auto-start.
decode_ft8 itself (ft8_lib) is compiled by the release workflow; here a tiny stand-in prints the same line format."""
import os, stat, sys, tempfile, time, wave
import sys as _s
if _s.platform == "win32":
    print("skipped: this test uses POSIX shell-script stand-in programs")
    raise SystemExit(0)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home
import numpy as np
from andysdr import ft8

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print("  ok  " if cond else "  FAIL", name, "" if cond else extra)

# --- band plan
check("14.074 USB is FT8", ft8.match_freq(14074000, "USB") == ("ft8", 14074000))
check("audio-offset inside the channel still matches", ft8.match_freq(14075500, "USB")[0] == "ft8")
check("14.080 is FT4", ft8.match_freq(14080000, "USB") == ("ft4", 14080000))
check("wrong mode does not match", ft8.match_freq(14074000, "NFM") is None)
check("random frequency does not match", ft8.match_freq(14200000, "USB") is None and ft8.match_freq(145500000, "USB") is None)
check("2 m FT8", ft8.match_freq(144174000, "USB")[0] == "ft8")

# --- parser (format taken from ft8_lib's decode_ft8.c printf)
a = ft8.parse_line("000000 +07.5 +0.32 1234 ~  CQ DX K1ABC FN42")
check("CQ message parsed", a and a["cq"] and a["call"] == "K1ABC" and a["grid"] == "FN42" and a["hz"] == 1234 and a["dt"] == 0.32, a)
b = ft8.parse_line("000000 +04.0 -0.10  987 ~  G4XYZ M0ABC -12")
check("reply parsed", b and b["to"] == "G4XYZ" and b["call"] == "M0ABC" and not b["cq"] and b["grid"] == "", b)
c = ft8.parse_line("000000 +04.0 +0.00 1500 ~  M0ABC G4XYZ RR73")
check("RR73 is not a grid", c and c["grid"] == "", c)
check("noise lines ignored", ft8.parse_line("Sample rate 12000 Hz") is None and ft8.parse_line("") is None)

# --- stand-in decode_ft8: checks the WAV it is given and prints two lines
stub = os.path.join(home, "decode_ft8_stub.py")
open(stub, "w").write('''#!%s
import sys, wave
p = sys.argv[-1]
w = wave.open(p)
assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 12000, "bad wav"
n = w.getnframes()
print("Sample rate 12000 Hz, %%d samples" %% n, file=sys.stderr)
print("000000 +12.0 +0.30 1000 ~  CQ M0TST IO91")
print("000000 +08.5 -0.20 2100 ~  M0TST G4ABC -05")
''' % sys.executable)
os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
tone = (np.sin(2 * np.pi * 1000 * np.arange(12000 * 15) / 12000) * 0.3).astype(np.float32)
msgs = ft8.run_decoder(stub, tone, "ft8")
check("run_decoder: WAV accepted, 2 messages", len(msgs) == 2 and msgs[0]["cq"], msgs)

# --- slot cutting with a fake clock
t = [1000.0 * 15 + 0.1]                         # just after a slot boundary
got, logs = [], []
dec = ft8.Ft8Decoder(stub, "ft8", on_message=got.append, on_log=logs.append, dial=lambda: 14074000, clock=lambda: t[0])
blk = np.zeros(2400, np.float32)                # 50 ms @ 48 kHz
def run_slot(seconds):
    for _ in range(int(seconds / 0.05)):
        dec.process(blk); t[0] += 0.05
run_slot(15.0)                                  # first slot: joined at a boundary, complete
run_slot(0.2)                                   # crosses into the next slot -> previous one is decoded
time.sleep(1.5)
check("a full slot is decoded after the boundary", len(got) == 2, (len(got), logs))
check("RF frequency = dial + audio offset", got and got[0]["rf"] == 14075000 and got[1]["rf"] == 14076100, got and got[0])
check("slot label is UTC of the slot start", got and got[0]["utc"] == time.strftime("%H:%M:%S", time.gmtime(1000 * 15)), got and got[0].get("utc"))
got.clear()
t[0] = 2000.0 * 15 + 9.0                        # joined 9 s into a slot
dec2 = ft8.Ft8Decoder(stub, "ft8", on_message=got.append, on_log=logs.append, clock=lambda: t[0])
for _ in range(int(7.0 / 0.05)):
    dec2.process(blk); t[0] += 0.05
t[0] += 0.1
dec2.process(blk)
time.sleep(0.5)
check("a partial first slot is not decoded", not got)
# FT4 slot length
check("FT4 slot is 7.5 s", ft8.Ft8Decoder(stub, "ft4").slot_len == 7.5)

# --- engine: tune a dial frequency, squelch open -> decoder starts by itself; tune away -> stops
from andysdr.config import Settings
from andysdr.engine import Radio
s = Settings()
s["device"] = "sim"
s["decoder_paths"] = {"decode_ft8": stub}
r = Radio(s)
r.start()
time.sleep(1.5)
r.set_squelch(-120.0)
r.set_frequency(14074000, "USB")
t0 = time.time()
while time.time() - t0 < 8 and r.decoders.active != "ft8":
    time.sleep(0.1)
check("FT8 decoder auto-started on 14.074 MHz USB", r.decoders.active == "ft8" and r.decoders.sub == "ft8", r.decoders.active)
check("marked as auto-owned and signal reported", r.auto_owned and r.signal and r.signal["kind"] == "FT8", r.signal)
r.set_frequency(14200000, "USB")
time.sleep(1.0)
check("tuning away stops it", r.decoders.active in (None, ""), r.decoders.active)
r.set_frequency(14080000, "USB")
t0 = time.time()
while time.time() - t0 < 8 and r.decoders.active != "ft8":
    time.sleep(0.1)
check("FT4 channel starts FT4", r.decoders.active == "ft8" and r.decoders.sub == "ft4", r.decoders.sub)
r.set_frequency(145500000, "NFM")
time.sleep(0.5)
s2 = Settings(); s2["device"] = "sim"
r2 = Radio(s2); r2.start(); time.sleep(1.2)
r2.set_squelch(-120.0); r2.set_frequency(14074000, "USB"); time.sleep(3)
check("missing decoder program: no crash, warning only", r2.decoders.active in (None, "") and "ft8" in r2.auto_failed, r2.decoders.active)
print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
