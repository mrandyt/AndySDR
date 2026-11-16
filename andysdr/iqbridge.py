"""rtl_tcp-compatible IQ server.

Lets external programs (SatDump for GOES HRIT, welle-cli for DAB) use the receiver AndySDR already
has open – RSP2pro, RTL-SDR, anything – by connecting to it as if it were an `rtl_tcp` server:

    4-byte "RTL0" magic, tuner type, gain count   (12 bytes)
    then unsigned 8-bit interleaved I/Q at the sample rate the client asked for.

The radio thread calls :meth:`feed` with each block of complex samples; the block is frequency-shifted so
the wanted signal sits at DC, resampled to the requested rate, converted to 8 bit and sent to the client.
A client that cannot keep up simply loses blocks – the radio is never slowed down.
"""
import logging
import queue
import socket
import struct
import threading

import numpy as np
from scipy import signal

log = logging.getLogger("andysdr.iqbridge")


class IqBridge:
    def __init__(self, out_rate=None, host="127.0.0.1", port=0):
        self.out_rate = out_rate          # Hz; None = same as the radio
        self.host, self.port = host, port
        self.srv = None
        self.client = None
        self.q = queue.Queue(maxsize=40)
        self.alive = False
        self.phase = 0.0
        self.sent = 0
        self.dropped = 0
        self._resamp = None
        self.cmds = []                    # (cmd, value) received from the client – informational

    # ---------------------------------------------------------------- server
    def start(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((self.host, self.port))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.alive = True
        threading.Thread(target=self._accept, name="iqbridge-accept", daemon=True).start()
        threading.Thread(target=self._send, name="iqbridge-send", daemon=True).start()
        return self.port

    def stop(self):
        self.alive = False
        for s in (self.client, self.srv):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self.client = self.srv = None
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def _accept(self):
        while self.alive:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                c.sendall(b"RTL0" + struct.pack(">II", 5, 29))        # R820T, 29 gain steps
            except OSError:
                continue
            self.client = c
            threading.Thread(target=self._read_cmds, args=(c,), daemon=True).start()

    def _read_cmds(self, c):
        buf = b""
        try:
            while self.alive:
                d = c.recv(256)
                if not d:
                    break
                buf += d
                while len(buf) >= 5:
                    cmd, val = struct.unpack(">BI", buf[:5])
                    buf = buf[5:]
                    self.cmds.append((cmd, val))
                    if cmd == 2 and not self.out_rate:                # set sample rate
                        self.out_rate = int(val)
        except OSError:
            pass
        if self.client is c:
            self.client = None

    def _send(self):
        while self.alive:
            b = self.q.get()
            if b is None:
                return
            c = self.client
            if not c:
                continue
            try:
                c.sendall(b)
                self.sent += len(b)
            except OSError:
                self.client = None

    # ---------------------------------------------------------------- samples
    def feed(self, iq, fs, offset=0.0):
        """iq: complex samples at fs Hz; the wanted signal is `offset` Hz away from DC."""
        if not self.alive or not self.client:
            return
        n = len(iq)
        if offset:
            w = -2 * np.pi * offset / fs
            ph = self.phase + w * np.arange(n)
            iq = iq * np.exp(1j * ph).astype(np.complex64)
            self.phase = float((self.phase + w * n) % (2 * np.pi))
        out = self.out_rate or fs
        if abs(out - fs) > 1:
            if self._resamp != (fs, out):
                self._resamp = (fs, out)
            g = int(np.gcd(int(fs), int(out)))
            iq = signal.resample_poly(iq, int(out) // g, int(fs) // g).astype(np.complex64)
        peak = max(float(np.max(np.abs(iq))), 1e-6) if len(iq) else 1.0
        scale = 127.0 / max(peak, 0.5)                    # raw ADC-ish scale; avoid blowing up quiet blocks
        u = np.empty(len(iq) * 2, np.uint8)
        u[0::2] = np.clip(iq.real * scale + 127.5, 0, 255)
        u[1::2] = np.clip(iq.imag * scale + 127.5, 0, 255)
        try:
            self.q.put_nowait(u.tobytes())
        except queue.Full:
            self.dropped += 1
