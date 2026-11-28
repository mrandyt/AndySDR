"""End-to-end: the real Radio engine, retuned over rigctl by a client on a socket.

This is the path a trunking decoder takes. DSD-FME reads the control channel, works out which frequency a call has
been given, and sends `F <hz>` to AndySDR's tuning server; the radio has to end up on that frequency. What matters
beyond that is *how* it got there: when the call lands inside the slice the receiver is already capturing, the
hardware must not move at all - only which part of the capture is being listened to - because retuning hardware
takes time and clips the start of a call.

Run:  python -I tests/test_radio_rigctl.py   (takes about 10 s)
"""
import os
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home

from andysdr.config import Settings  # noqa: E402
from andysdr.engine import Radio  # noqa: E402

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


def talk(port, line, wait=0.6):
    c = socket.create_connection(("127.0.0.1", port), timeout=3)
    c.settimeout(3)
    try:
        c.sendall((line + "\n").encode())
        time.sleep(wait)
        try:
            return c.recv(256).decode()
        except socket.timeout:
            return ""
    finally:
        c.close()


s = Settings()
s["device"] = "sim"
s["frequency"] = 451_200_000
r = Radio(s)
r.start()
time.sleep(2.5)
try:
    check("the radio has a tuning server", getattr(r, "rigctl", None) is not None)
    ok, why = r.rigctl.start()
    port = r.rigctl.port
    check("it starts", ok, why)

    got = talk(port, "f")
    check("a client is told the frequency", got.strip() == str(r.freq), (got.strip(), r.freq))

    # --- a call inside the slice already being captured: the hardware must not move -----------------------
    fs = r.dev.sample_rate
    centre0 = r.dev.center
    near = int(centre0 + fs * 0.25)                  # well inside the capture, and clear of the middle
    got = talk(port, "F %d" % near)
    time.sleep(0.8)
    check("a call inside the captured slice is followed", got.strip() == "RPRT 0" and r.freq == near,
          (got.strip(), r.freq, near))
    check("...without moving the receiver's hardware at all", r.dev.center == centre0,
          (r.dev.center, centre0))
    check("...so there is no retuning delay to clip the start of the call", abs(r.offset()) > 1000,
          r.offset())

    # --- a call outside it: the hardware has to move ------------------------------------------------------
    far = int(centre0 + fs * 3)
    got = talk(port, "F %d" % far)
    time.sleep(1.2)
    check("a call outside the captured slice is followed too", got.strip() == "RPRT 0" and r.freq == far,
          (got.strip(), r.freq, far))
    check("...and this time the hardware does move", r.dev.center != centre0, (r.dev.center, centre0))
    check("...landing with the call inside the new slice", abs(r.freq - r.dev.center) < fs * 0.5,
          (r.freq, r.dev.center, fs))

    # --- back to the control channel, the way a decoder does at the end of a call -------------------------
    got = talk(port, "F 451200000")
    time.sleep(0.8)
    check("it goes back to the control channel afterwards", r.freq == 451_200_000, r.freq)

    st = r.rigctl.status()
    check("the retunes are counted", st["tunes"] == 3, st["tunes"])
    check("...and the last one remembered", st["last"]["freq"] == 451_200_000, st["last"])

    # --- it must never be talked into something impossible -------------------------------------------------
    before = r.freq
    for bad in ("F 0", "F -1", "F banana", "F"):
        talk(port, bad, wait=0.3)
    time.sleep(0.5)
    check("nonsense does not move the radio", r.freq == before, (r.freq, before))
    out = talk(port, "F 99000000000")               # above anything the receiver can tune
    time.sleep(0.6)
    check("a frequency the receiver cannot reach is refused, not attempted", r.freq == before,
          (r.freq, before, out.strip()))
finally:
    try:
        r.rigctl.stop()
    except Exception:                                # noqa: BLE001
        pass
    r.stop()

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
