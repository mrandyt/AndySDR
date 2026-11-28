"""A rigctl server, so a decoder can retune AndySDR to follow a trunked call.

On a trunked system the conversation is not on the frequency you are listening to. A control channel carries a
steady stream of signalling, and when somebody presses the key the system says, in effect, "this call is on channel
17 for the next thirty seconds". To hear it you have to go to channel 17 and come back afterwards.

DSD-FME already knows how to do that: it reads the control channel, works out which frequency a call has gone to,
and then asks something to retune. What it asks is a **rigctl** server - the control protocol Hamlib defines for
radios, which SDR++ and others implement so that DSD-FME can drive them. AndySDR already feeds DSD-FME its audio,
so a rigctl server is the one piece missing, and the whole of trunk following falls out of it:

    dsd-fme -fs -i tcp:127.0.0.1:7355 -U 4532 -T -C channel_map.csv

The retune is usually free. :meth:`andysdr.engine.Radio._tune` moves the receiver's hardware only when the wanted
frequency falls outside the slice already being captured; inside it, the radio just moves which part of the capture
it is listening to. An RSP2 at 7.2 MS/s covers most of a trunked site in one go, so a call grant is followed in the
time it takes to move a number, with no retuning delay and nothing missed.

**On the protocol.** rigctl is a plain line protocol: a lower-case letter asks, the upper-case letter sets. This
implements the handful of commands a decoder needs - frequency, mode, VFO, PTT - and answers anything else with a
polite error rather than silence, because a client left waiting for a reply hangs instead of carrying on. Every
command received is counted and the ones not recognised are logged by name, so what a particular client actually
needs can be seen rather than guessed at.

**On safety.** Anything that can connect can retune the radio, so the server listens on this machine only unless it
is deliberately opened up, and it is only running while trunking is turned on.
"""
import logging
import socket
import threading
import time

log = logging.getLogger("andysdr.rigctl")

PORT = 4532                 # what rigctld uses, and what DSD-FME's -U expects
MAX_CLIENTS = 4

# The reply to \dump_state. Clients read it to learn what the radio can do; the numbers below say "one VFO, one
# band, from 1 kHz to 2 GHz, all modes", which is true enough of an SDR. A client that wants more detail gets a
# well-formed answer rather than a hang, which is what matters.
DUMP_STATE = "\n".join([
    "0",                                        # protocol version
    "1",                                        # rig model: 1 is the dummy rig, which is what we are pretending to be
    "0",                                        # ITU region
    "1000.000000 2000000000.000000 0x2ff -1 -1 0x10000003 0x3",    # receive range
    "0 0 0 0 0 0 0",                            # end of the receive list
    "0 0 0 0 0 0 0",                            # no transmit ranges
    "0 0",                                      # end of the tuning-step list
    "0 0",                                      # end of the filter list
    "0", "0", "0", "0",                         # max RIT / XIT / IF shift, announces
    "0x0", "0x0", "0x0", "0x0", "0x0", "0x0",   # preamp, attenuator, and the get/set capability words
    "",
])


class RigctlServer:
    """Listens for rigctl clients and turns their commands into tuning.

    `get_freq()` returns the frequency now in Hz; `set_freq(hz)` moves to one. `set_freq` is called from the
    server's own thread, so it must be safe to call from anywhere - `Radio.set_frequency` queues the work, which is
    exactly right.
    """

    def __init__(self, get_freq, set_freq, port=PORT, host="127.0.0.1", log_cb=None, get_mode=None):
        self.get_freq = get_freq
        self.set_freq = set_freq
        self.get_mode = get_mode
        self.port = int(port)
        self.host = host
        self.log_cb = log_cb
        self.sock = None
        self.alive = False
        self.clients = 0
        self.seen = {}                  # command -> how many times, for showing what a client really asks for
        self.unknown = set()            # ...and which of them were not recognised, logged once each
        self.last_tune = None           # (when, Hz) of the last retune asked for
        self.tunes = 0
        self.error = ""

    # ------------------------------------------------------------------ running
    def start(self):
        if self.alive:
            return True, ""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(MAX_CLIENTS)
            s.settimeout(0.5)
        except OSError as e:
            self.error = str(e)
            log.warning("cannot listen on %s:%d - %s", self.host, self.port, e)
            return False, f"Could not start the tuning server on port {self.port}: {e}"
        self.sock = s
        self.alive = True
        self.error = ""
        threading.Thread(target=self._accept, daemon=True, name="rigctl").start()
        log.info("rigctl server listening on %s:%d", self.host, self.port)
        self._say(f"Tuning server ready on port {self.port} – a decoder can now retune the radio to follow calls")
        return True, ""

    def stop(self):
        self.alive = False
        s, self.sock = self.sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass

    def _say(self, text):
        if self.log_cb:
            try:
                self.log_cb(text)
            except Exception:                   # noqa: BLE001
                pass

    def _accept(self):
        while self.alive:
            try:
                c, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.clients >= MAX_CLIENTS:
                try:
                    c.close()
                except OSError:
                    pass
                continue
            self.clients += 1
            log.info("rigctl client connected from %s", addr[0])
            threading.Thread(target=self._serve, args=(c, addr), daemon=True, name="rigctl-client").start()

    def _serve(self, c, addr):
        buf = b""
        try:
            c.settimeout(1.0)
            while self.alive:
                try:
                    d = c.recv(512)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not d:
                    break
                buf += d
                if len(buf) > 8192:
                    buf = buf[-1024:]           # a client talking nonsense must not grow the buffer for ever
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    reply = self.handle(line.decode("utf-8", "replace"))
                    if reply is None:           # the client said goodbye
                        raise StopIteration
                    if reply:
                        c.sendall(reply.encode())
        except (StopIteration, OSError):
            pass
        finally:
            self.clients = max(0, self.clients - 1)
            try:
                c.close()
            except OSError:
                pass
            log.info("rigctl client from %s disconnected", addr[0])

    # ------------------------------------------------------------------ the protocol
    def handle(self, line):
        """One command in, the reply out. None means the client asked to finish.

        Kept apart from the socket so it can be tested directly, which is the only way to be sure of a protocol
        whose other end is somebody else's program.
        """
        line = line.strip("\r\n \t;")
        if not line:
            return ""
        if line[0] in "+|,;":                   # an "extended response" prefix: the command follows it
            line = line[1:].strip()
            if not line:
                return ""
        parts = line.split()
        cmd = parts[0]
        self.seen[cmd] = self.seen.get(cmd, 0) + 1

        if cmd in ("q", "Q", "quit", "\\quit"):
            return None
        if cmd == "\\dump_state":
            return DUMP_STATE
        if cmd == "\\chk_vfo":
            return "CHKVFO 0\n"
        if cmd in ("f", "\\get_freq"):
            return "%d\n" % int(self._freq())
        if cmd in ("F", "\\set_freq"):
            return self._set(parts[1:])
        if cmd in ("m", "\\get_mode"):
            return "%s\n2800\n" % (self._mode() or "FM")
        if cmd in ("M", "\\set_mode"):
            return "RPRT 0\n"                   # the decoder tells AndySDR what it is listening to; it already knows
        if cmd in ("v", "\\get_vfo"):
            return "VFOA\n"
        if cmd in ("V", "\\set_vfo"):
            return "RPRT 0\n"
        if cmd in ("t", "\\get_ptt"):
            return "0\n"                        # a receiver never transmits, and must not pretend it might
        if cmd in ("T", "\\set_ptt"):
            return "RPRT -1\n"
        if cmd in ("s", "\\get_split_vfo"):
            return "0\nVFOA\n"
        if cmd in ("\\get_powerstat",):
            return "1\n"
        if cmd not in self.unknown:
            self.unknown.add(cmd)
            log.info("rigctl: client sent %r, which this server does not implement - answering with an error", line)
            self._say(f"Tuning server: the decoder asked for '{cmd}', which AndySDR does not answer yet")
        return "RPRT -11\n"                     # "not implemented": a client can carry on, where silence would hang it

    def _freq(self):
        try:
            return int(self.get_freq() or 0)
        except Exception:                       # noqa: BLE001
            return 0

    def _mode(self):
        try:
            return (self.get_mode() if self.get_mode else "") or ""
        except Exception:                       # noqa: BLE001
            return ""

    def _set(self, args):
        if not args:
            return "RPRT -1\n"
        try:
            hz = int(float(args[-1]))           # "F 451200000", and "F VFOA 451200000" from a client using VFOs
        except ValueError:
            return "RPRT -1\n"
        if hz <= 0:
            return "RPRT -1\n"
        try:
            self.set_freq(hz)
        except Exception as e:                  # noqa: BLE001
            log.warning("rigctl: could not tune to %d Hz: %s", hz, e)
            return "RPRT -1\n"
        self.last_tune = (time.time(), hz)
        self.tunes += 1
        return "RPRT 0\n"

    # ------------------------------------------------------------------ for the screen
    def status(self):
        last = None
        if self.last_tune:
            last = {"when": int(self.last_tune[0]), "freq": self.last_tune[1],
                    "ago": round(time.time() - self.last_tune[0], 1)}
        return {"on": bool(self.alive), "port": self.port, "host": self.host, "clients": self.clients,
                "tunes": self.tunes, "last": last, "error": self.error,
                "commands": dict(sorted(self.seen.items())), "not_understood": sorted(self.unknown)}
