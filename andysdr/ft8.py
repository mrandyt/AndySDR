"""FT8 / FT4 decoding.

The heavy lifting (sync search, LDPC, CRC, message unpacking) is done by `decode_ft8`, the demo program of
Karlis Goba's MIT-licensed ft8_lib (https://github.com/kgoba/ft8_lib), which AndySDR builds and ships
next to the other decoders.  This module

  * knows the FT8 / FT4 dial frequencies (the band plan) so it can start by itself when a signal is found,
  * cuts the demodulated USB audio into the 15 s (FT4: 7.5 s) UTC time slots the modes use,
  * hands each finished slot to decode_ft8 as a 12 kHz WAV file, and
  * turns its text output into messages for the FT8 tab.
"""
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np
from scipy import signal

log = logging.getLogger("andysdr.ft8")

AUDIO_RATE = 48000
SLOT = {"ft8": 15.0, "ft4": 7.5}

# USB dial frequencies (Hz) – IARU / WSJT-X defaults
FT8_DIALS = [1840000, 3573000, 5357000, 7074000, 10136000, 14074000, 18100000, 21074000, 24915000, 28074000,
             50313000, 50323000, 70154000, 144174000]
FT4_DIALS = [1843000, 3575000, 5360000, 7047500, 10140000, 14080000, 18104000, 21140000, 24919000, 24922000, 28180000,
             50318000, 144150000, 144170000]

_LINE = re.compile(r"^(\d{6})\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s+(\d+)\s+~\s+(.*\S)\s*$")
_GRID = re.compile(r"^[A-R]{2}\d{2}([A-X]{2})?$")
_CALL = re.compile(r"^[A-Z0-9/<>.]{3,13}$")


def match_freq(freq, mode="USB"):
    """('ft8' | 'ft4', dial) if the tuned frequency is inside an FT8/FT4 channel (dial -0.5 kHz .. +3 kHz), else None."""
    if mode not in ("USB", "DIG", "DIG25"):
        return None
    for proto, dials in (("ft8", FT8_DIALS), ("ft4", FT4_DIALS)):
        for d in dials:
            if d - 500 <= freq <= d + 3000:
                return proto, d
    return None


def parse_line(line):
    """decode_ft8 output line -> dict, or None.  Format: 'HHMMSS snr dt freq ~  message'."""
    m = _LINE.match(line.strip())
    if not m:
        return None
    hhmmss, snr, dt, hz, text = m.groups()
    d = {"slot": hhmmss, "score": float(snr), "dt": float(dt), "hz": int(hz), "text": text,
         "cq": False, "call": "", "grid": "", "to": ""}
    toks = text.upper().split()
    if toks and toks[0] == "CQ":
        d["cq"] = True
        calls = [t for t in toks[1:] if _CALL.match(t) and any(ch.isdigit() for ch in t) and not _GRID.match(t)]
        d["call"] = calls[0] if calls else ""
    elif len(toks) >= 2:
        d["to"], d["call"] = toks[0], toks[1]
    for t in toks[::-1]:
        if _GRID.match(t) and t != "RR73":
            d["grid"] = t
            break
    return d


def run_decoder(exe, samples12k, proto="ft8", timeout=40):
    """Write 12 kHz samples to a temp WAV, run decode_ft8, return parsed message dicts."""
    pcm = np.clip(np.asarray(samples12k) * 32767, -32767, 32767).astype("<i2")
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="ft8_")
    os.close(fd)
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(12000)
            w.writeframes(pcm.tobytes())
        args = [exe] + (["-ft4"] if proto == "ft4" else []) + [path]
        kw = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, **kw)
        out = []
        for ln in p.stdout.decode("utf-8", "replace").splitlines():
            d = parse_line(ln)
            if d:
                out.append(d)
        return out
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class Ft8Decoder:
    """Feed it the 48 kHz USB audio; it emits one callback per decoded message."""

    def __init__(self, exe, proto="ft8", on_message=None, on_log=None, dial=None, clock=time.time, runner=None):
        self.exe, self.proto = exe, proto
        self.on_message = on_message or (lambda m: None)
        self.log = on_log or (lambda s: None)
        self.dial = dial                          # () -> current dial frequency in Hz, for the RF frequency column
        self.clock = clock
        self.runner = runner or run_decoder
        self.slot_len = SLOT[proto]
        self.buf = []
        self.n = 0
        self.slot = None
        self.decoded = 0
        self.slots = 0
        self.busy = False
        self.alive = True

    def process(self, audio):
        if not self.alive:
            return
        now = self.clock()
        slot = int(now // self.slot_len)
        if self.slot is None:
            self.slot = slot
        elif slot != self.slot:
            self._finish(self.slot)
            self.slot = slot
            self.buf, self.n = [], 0
        self.buf.append(np.asarray(audio, np.float32))
        self.n += len(audio)

    def _finish(self, slot):
        # a slot we only joined part-way through is too incomplete to be worth decoding
        if self.n < AUDIO_RATE * self.slot_len * 0.8:
            self.log(f"FT8: waiting for the next {self.slot_len:g} s slot boundary")
            return
        if self.busy:
            return
        x = np.concatenate(self.buf)[: int(AUDIO_RATE * self.slot_len)]
        start = slot * self.slot_len
        dial = self.dial() if self.dial else 0
        self.busy = True
        threading.Thread(target=self._decode, args=(x, start, dial), name="ft8-decode", daemon=True).start()

    def _decode(self, x, start, dial):
        try:
            y = signal.resample_poly(x, 1, 4)            # 48 kHz -> 12 kHz
            peak = float(np.max(np.abs(y))) if len(y) else 0
            if peak > 0:
                y = y * (0.5 / peak) if peak > 0.5 else y
            msgs = self.runner(self.exe, y, self.proto)
            tm = time.gmtime(start)
            label = time.strftime("%H:%M:%S", tm)
            self.slots += 1
            for m in msgs:
                m["utc"] = label
                m["rf"] = int(dial + m["hz"]) if dial else None
                m["proto"] = self.proto.upper()
                self.decoded += 1
                self.on_message(m)
            self.log(f"FT8 slot {label} UTC: {len(msgs)} message(s)" if self.proto == "ft8" else f"FT4 slot {label} UTC: {len(msgs)} message(s)")
        except Exception as e:                           # noqa: BLE001
            log.exception("ft8 decode")
            self.log(f"FT8 decoder error: {e}")
        finally:
            self.busy = False

    def stop(self):
        self.alive = False
