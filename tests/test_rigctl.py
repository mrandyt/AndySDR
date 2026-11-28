"""The rigctl server: the line protocol, and a real client over a real socket.

The other end of this protocol is somebody else's program, so the protocol handling is tested directly rather than
only through a socket, and anything unrecognised has to come back as an error a client can carry on from - never
silence, which would leave it waiting for ever.
"""
import os, socket, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr.rigctl import RigctlServer

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


class Radio:
    def __init__(self, f=145_000_000):
        self.freq = f
        self.sets = []

    def get(self):
        return self.freq

    def set(self, hz):
        self.freq = int(hz)
        self.sets.append(int(hz))


# --- the commands a decoder uses --------------------------------------------------------------------------
r = Radio()
s = RigctlServer(r.get, r.set, port=0)
check("it reports the frequency", s.handle("f") == "145000000\n", s.handle("f"))
check("it retunes", s.handle("F 451200000") == "RPRT 0\n" and r.freq == 451_200_000, (r.freq, r.sets))
check("...and says so afterwards", s.handle("f") == "451200000\n", s.handle("f"))
check("a client that names a VFO is understood too", s.handle("F VFOA 452000000") == "RPRT 0\n"
      and r.freq == 452_000_000, r.freq)
check("the long form works as well", s.handle("\\set_freq 453000000") == "RPRT 0\n" and r.freq == 453_000_000, r.freq)
check("...in both directions", s.handle("\\get_freq") == "453000000\n")
check("mode is answered", s.handle("m").startswith("FM\n"), s.handle("m"))
check("...and setting it is accepted without disturbing the radio",
      s.handle("M FM 12000") == "RPRT 0\n" and r.freq == 453_000_000, r.freq)
check("the VFO is answered", s.handle("v") == "VFOA\n", s.handle("v"))
check("a receiver never claims to transmit", s.handle("t") == "0\n", s.handle("t"))
check("...and refuses to be keyed up", s.handle("T 1") == "RPRT -1\n", s.handle("T 1"))
check("dump_state is answered and ends in a newline", s.handle("\\dump_state").endswith("\n")
      and len(s.handle("\\dump_state")) > 40)
check("chk_vfo is answered", s.handle("\\chk_vfo") == "CHKVFO 0\n")
check("quitting is recognised", s.handle("q") is None)

# --- nothing is allowed to hang the client -----------------------------------------------------------------
for junk in ("Z", "\\frobnicate", "F", "F wibble", "F -5", "!!", "\\set_level RFPOWER 0.5"):
    out = s.handle(junk)
    check(f"{junk!r} gets an answer, not silence", isinstance(out, str) and out.endswith("\n"), repr(out))
check("a bad frequency does not move the radio", r.freq == 453_000_000, r.freq)
check("...and the unrecognised ones are remembered", "Z" in s.status()["not_understood"],
      s.status()["not_understood"])
check("an empty line is ignored quietly", s.handle("") == "" and s.handle("  \r\n") == "")
check("an extended-response prefix is stripped", s.handle("+f") == "453000000\n", s.handle("+f"))

# --- what it tells the screen --------------------------------------------------------------------------------
st = s.status()
check("it counts what it was asked", st["commands"].get("f", 0) >= 2, st["commands"])
check("...and how many times it retuned", st["tunes"] == 3, st["tunes"])
check("...and what the last one was", st["last"] and st["last"]["freq"] == 453_000_000, st["last"])

# --- a real client over a real socket ---------------------------------------------------------------------
r2 = Radio()
srv = RigctlServer(r2.get, r2.set, port=0, host="127.0.0.1")
ok, why = srv.start()
check("the server starts", ok, why)
port = srv.sock.getsockname()[1] if srv.sock else 0
srv.port = port
try:
    c = socket.create_connection(("127.0.0.1", port), timeout=3)
    c.settimeout(3)
    c.sendall(b"f\n")
    check("a client gets the frequency", c.recv(64).strip() == b"145000000")
    c.sendall(b"F 430125000\nf\n")                       # two commands in one write, as a busy client sends them
    time.sleep(0.3)
    got = c.recv(128)
    check("both commands in one write are answered", got == b"RPRT 0\n430125000\n", got)
    check("...and the radio really moved", r2.freq == 430_125_000, r2.freq)
    check("the server counts its client", srv.status()["clients"] == 1, srv.status()["clients"])
    c.sendall(b"q\n")
    time.sleep(0.4)
    check("...and lets go when the client says goodbye", srv.status()["clients"] == 0, srv.status()["clients"])
    c.close()

    # a client that disappears without saying goodbye must not leave the count wrong
    c2 = socket.create_connection(("127.0.0.1", port), timeout=3)
    c2.sendall(b"f\n")
    c2.recv(64)
    c2.close()
    time.sleep(1.4)
    check("a client that just vanishes is let go too", srv.status()["clients"] == 0, srv.status()["clients"])
finally:
    srv.stop()
time.sleep(0.2)
check("stopping closes the port", not srv.alive)
try:
    socket.create_connection(("127.0.0.1", port), timeout=1).close()
    check("...and nothing answers on it any more", False, "still accepting")
except OSError:
    check("...and nothing answers on it any more", True)

# --- it will not fight over a port it cannot have -------------------------------------------------------------
hold = socket.socket()
hold.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
hold.bind(("127.0.0.1", 0))
hold.listen(1)
taken = hold.getsockname()[1]
busy = RigctlServer(r2.get, r2.set, port=taken)
ok2, why2 = busy.start()
if ok2:                                  # some systems allow the second bind: then there is nothing to test here
    busy.stop()
    print("note: this system allowed a second bind on the same port, so the clash check was skipped")
else:
    check("a port already in use is reported, not crashed on", "port" in why2.lower(), why2)
    check("...and the server knows it is not running", not busy.alive and busy.status()["error"])
hold.close()

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
