"""Inmarsat AERO – aircraft ACARS relayed through an Inmarsat L-band satellite (1545 MHz band).

The heavy lifting (BPSK / OQPSK demodulation, FEC, ACARS framing) is done by SatDump's Inmarsat pipelines, which AndySDR
already downloads for the weather satellites.  This module:

  * feeds SatDump the receiver's IQ over the local rtl_tcp bridge, with the wanted channel shifted to DC and the sample rate
    brought down to 240 kS/s (an AERO channel is only 1 - 12 kHz wide);
  * reads the messages SatDump writes (one small JSON file per message) and turns them into the same records the VDL2 and
    ACARS decoders produce, so they appear in the AIRCRAFT TEXT tab;
  * polls SatDump's own statistics (correlator lock, bit-error rate, signal-to-noise) so the screen can say whether it is
    locked on - the number to watch while fine-tuning an antenna;
  * measures where the carrier really is inside the 240 kS/s stream, so the VFO can be centred on it with one press;
  * in AUTO mode steps through the data rates (1200, 600 and 10500 bit/s) until one locks.

Only the forward link (satellite to aircraft) is received - the ground station sends to the aircraft on a continuous
"P-channel" - so the messages are the ones airlines send to their aircraft (weather, load sheets, flight plans, link tests).
Voice (the 8.4 kbit/s AERO-C channels) is deliberately not decoded.
"""
import glob
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

import numpy as np
from scipy import signal

from .iqbridge import IqBridge

log = logging.getLogger("andysdr.aero")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

RATE = 240000          # IQ rate handed to SatDump: wide enough for the 10.5 kbit/s channel, light enough for any PC
AGC = 20.0             # IqBridge slow gain: keeps the background noise at about this many 8-bit steps (stable level for the PLL)
POLL_S = 2.0           # how often statistics and message files are read
LOCK_WAIT_S = 75       # AUTO: how long one data rate gets to lock before the next is tried (SatDump needs ~10 s to start)
LOSS_RESUME_S = 240    # AUTO: after a lock is lost for this long the search starts again
ROTATE_S = 6 * 3600    # SatDump keeps a soft-symbol file open while it runs: restart into a fresh folder now and then and delete the old one

# key -> SatDump pipeline, bit rate, bandwidth of the signal (Hz, for the carrier finder) and a plain-English name.
# 8.4 kbit/s AERO-C (voice) is not offered on purpose.
MODES = {
    "p1200":  {"pipe": "inmarsat_aero_12",  "bps": 1200,  "bw": 1700.0,  "name": "P-channel 1200 bit/s (Classic Aero)"},
    "p600":   {"pipe": "inmarsat_aero_6",   "bps": 600,   "bw": 850.0,   "name": "P-channel 600 bit/s (Classic Aero)"},
    "p10500": {"pipe": "inmarsat_aero_105", "bps": 10500, "bw": 10500.0, "name": "P-channel 10500 bit/s (Aero H / H+)"},
}
AUTO_ORDER = ["p1200", "p600", "p10500"]

# Channels listeners have reported on Alphasat (25 deg E, the satellite a UK antenna points at).  They are NOT guaranteed:
# Inmarsat moves them, so confirm on the waterfall that there is a narrow carrier at the frequency before trusting a preset.
CHANNELS = [
    {"mhz": 1546.100, "mode": "p1200",  "name": "Alphasat 25E  1200 bit/s"},
    {"mhz": 1546.115, "mode": "p600",   "name": "Alphasat 25E  600 bit/s"},
    {"mhz": 1546.130, "mode": "p10500", "name": "Alphasat 25E  10500 bit/s"},
]
BAND_MHZ = (1518.0, 1560.0)      # Inmarsat downlink: the AERO forward channels sit in 1525-1559 MHz


# --------------------------------------------------------------------------------------------------------------------
def _ch(v):
    """A byte (or already a character) from SatDump's JSON -> one printable character, '' if there is none."""
    if isinstance(v, int):
        v = chr(v & 0x7F)
    v = str(v or "")[:1]
    return v if v.isprintable() else ""


def convert_acars(js):
    """One message file written by SatDump's inmarsat_aero_parser -> the record the AIRCRAFT TEXT tab uses (or None).

    Fields in SatDump's JSON: plane_reg, label, mode, tak, bi, message, more_to_come and signal_unit.aes_id (the aircraft's
    24-bit ICAO address).  Everything but ACARS (system tables, log-on packets) is not a message and returns None."""
    if not isinstance(js, dict) or js.get("msg_name") != "ACARS":
        return None
    label = str(js.get("label") or "")
    label = "".join("d" if c == "\x7f" else c for c in label)
    label = "".join(c for c in label if c.isprintable())[:2]
    tak = js.get("tak")
    ack = "NAK" if tak == 0x15 else (_ch(tak) or "?")
    bid = _ch(js.get("bi")) or "?"
    text = str(js.get("message") or "").replace("<DEL>", "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c if (c.isprintable() or c == "\n") else " " for c in text).strip("\n")
    su = js.get("signal_unit") if isinstance(js.get("signal_unit"), dict) else {}
    aes = su.get("aes_id")
    return {"mode": _ch(js.get("mode")) or "2", "reg": str(js.get("plane_reg") or "").lstrip(".").strip(), "ack": ack,
            "label": label, "bid": bid, "down": bid.isdigit(), "msgno": "", "flight": "", "text": text,
            "more": bool(js.get("more_to_come")), "via": "AERO",
            "icao": ("%06X" % aes) if isinstance(aes, int) else ""}


def parse_stats(js):
    """SatDump's /api statistics (a dict per module) -> {lock, ber, corr, snr, peak_snr, carrier_hz} for the keys present."""
    out = {}
    if not isinstance(js, dict):
        return out
    for v in js.values():
        if not isinstance(v, dict):
            continue
        if "lock_state" in v:
            out["lock"] = v.get("lock_state") == "SYNCED"
            if isinstance(v.get("viterbi_ber"), (int, float)):
                out["ber"] = float(v["viterbi_ber"])
            if isinstance(v.get("correlator_corr"), (int, float)):
                out["corr"] = float(v["correlator_corr"])
        if "snr" in v and isinstance(v.get("snr"), (int, float)):
            out["snr"] = float(v["snr"])
            if isinstance(v.get("peak_snr"), (int, float)):
                out["peak_snr"] = float(v["peak_snr"])
            if isinstance(v.get("freq"), (int, float)):
                out["carrier_hz"] = float(v["freq"])
    return out


def estimate_offset(iq, fs, bw_hz, span_hz=9000.0, min_snr_db=2.5):
    """Where is the carrier inside this stream?  Returns (offset_hz, snr_db) or None when nothing stands out.

    A BPSK / QPSK data signal is a hump of power about `bw_hz` wide centred on the carrier.  The power spectrum is smoothed
    with a window that wide and the strongest place within +-span_hz of the cursor is taken (the centre of the hump), then
    refined by the power-weighted centre of the hump.  snr_db compares the hump with the median noise floor."""
    iq = np.asarray(iq)
    if len(iq) < 16384 or bw_hz <= 0:
        return None
    nper = 4096
    f, p = signal.welch(iq, fs, nperseg=nper, return_onesided=False, detrend=False)
    f, p = np.fft.fftshift(f), np.fft.fftshift(p)
    df = fs / nper
    w = max(3, int(round(bw_hz / df)))
    sm = np.convolve(p, np.ones(w) / w, mode="same")
    floor = float(np.median(sm))
    if floor <= 0:
        return None
    zone = np.abs(f) <= span_hz
    idx = np.flatnonzero(zone)
    if idx.size == 0:
        return None
    k = int(idx[np.argmax(sm[idx])])
    snr = 10 * np.log10(max(sm[k], 1e-30) / floor)
    if snr < min_snr_db:
        return None
    lo, hi = max(0, k - w // 2 - w // 4), min(len(p), k + w // 2 + w // 4 + 1)
    seg = np.clip(p[lo:hi] - floor, 0, None)
    off = float(np.sum(f[lo:hi] * seg) / np.sum(seg)) if np.sum(seg) > 0 else float(f[k])
    return off, float(snr)


def purge_old(images_dir, days=1.0):
    """Delete the working folders (aero_YYYYMMDD_HHMMSS) of earlier sessions: the messages are in the daily CSV already."""
    n = 0
    for d in glob.glob(os.path.join(images_dir, "aero_*")):
        try:
            if os.path.isdir(d) and time.time() - os.path.getmtime(d) > days * 86400:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


# --------------------------------------------------------------------------------------------------------------------
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class AeroRunner:
    """One AERO session: IQ bridge + SatDump process + the readers around them."""

    def __init__(self, exe, out_dir, sub="auto", on_message=None, on_status=None, on_log=None, popen=None,
                 lock_wait=LOCK_WAIT_S, poll=POLL_S):
        self.exe = list(exe) if isinstance(exe, (list, tuple)) else [exe]
        self.out_dir = out_dir
        self.auto = sub not in MODES
        self.mode = sub if sub in MODES else AUTO_ORDER[0]
        self.on_message = on_message or (lambda m: None)
        self.on_status = on_status or (lambda s: None)
        self.on_log = on_log or (lambda t, k="decoder": None)
        self.popen = popen or (lambda args, **kw: subprocess.Popen(args, **kw))
        self.lock_wait, self.poll = lock_wait, poll
        self.bridge = None
        self.proc = None
        self.http_port = 0
        self.run_dir = out_dir
        self.alive = False
        self.seen = set()
        self.frames = 0
        self.messages = 0
        self.stats = {}
        self.t_start = 0.0          # when the current SatDump process was launched
        self.t_lock = 0.0           # last time the correlator was locked
        self.locked_once = False
        self.off = None             # (offset_hz, snr_db) of the carrier relative to the VFO
        self._buf = []
        self._buf_n = 0
        self._lock = threading.Lock()
        self._gen = 0
        self._last_offset = None

    # ----------------------------------------------------------------------------------------------------------- life
    def start(self):
        os.makedirs(self.out_dir, exist_ok=True)
        self.bridge = IqBridge(out_rate=RATE, agc_target=AGC)
        self.bridge.tap = self._tap
        self.alive = True
        self.bridge.start()
        self._launch()
        threading.Thread(target=self._loop, name="aero-poll", daemon=True).start()

    def stop(self):
        self.alive = False
        self._kill()
        if self.bridge:
            self.bridge.stop()
            self.bridge = None
        self.on_status({"running": False})

    def feed(self, iq, fs, offset=0.0):
        b = self.bridge
        if not b:
            return
        if self._last_offset is None or abs(offset - self._last_offset) > 2.0:
            self._reset_measure()                 # the cursor moved: what was measured so far refers to the old place
        self._last_offset = offset
        b.feed(iq, fs, offset)

    def _reset_measure(self):
        with self._lock:
            self._buf, self._buf_n = [], 0
        self.off = None

    def measurement(self):
        """(offset_hz, snr_db) of the carrier relative to the VFO, or None."""
        return self.off

    def _kill(self):
        p, self.proc = self.proc, None
        if p:
            try:
                p.terminate()
                p.wait(timeout=4)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _launch(self):
        self._kill()
        old = self.run_dir if self._gen else None
        if old:
            self._scan_files()                    # last look for messages before the folder is thrown away
            shutil.rmtree(old, ignore_errors=True)
        m = MODES[self.mode]
        self._gen += 1
        self.run_dir = os.path.join(self.out_dir, f"run{self._gen}")
        os.makedirs(self.run_dir, exist_ok=True)
        self.http_port = _free_port()
        self.stats = {}
        self.t_start = time.time()
        args = self.exe + ["live", m["pipe"], self.run_dir, "--source", "rtltcp", "--ip_address", "127.0.0.1",
                           "--port", str(self.bridge.port), "--samplerate", str(RATE), "--frequency", "1546.1e6",
                           "--gain", "30", "--http_server", f"127.0.0.1:{self.http_port}"]
        self.proc = self.popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        threading.Thread(target=self._read_out, args=(self.proc,), name="aero-out", daemon=True).start()
        self.on_log(f"AERO: SatDump started for {m['name']}")

    def _read_out(self, proc):
        """SatDump's console, minus the chatter: a line per packet and the message text (which AndySDR lists itself)."""
        in_msg = False
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = ANSI.sub("", raw.decode("utf-8", "replace")).strip()
                if not line:
                    continue
                stamped = line.startswith("[")
                if stamped:
                    in_msg = False
                if "(D)" in line or "(T)" in line or (not stamped and in_msg):
                    continue
                if "ACARS message (" in line:
                    in_msg = True                 # the lines that follow are the message text
                    continue
                if "Packet :" in line:
                    continue
                self.on_log(line)
        except (OSError, ValueError):
            pass

    # ---------------------------------------------------------------------------------------------------- carrier finder
    def _tap(self, iq):
        """Called by the bridge with the 240 kS/s stream (wanted channel at DC): keep about 1.5 s of it."""
        with self._lock:
            self._buf.append(iq.copy())
            self._buf_n += len(iq)
            while self._buf_n > 1.5 * RATE and len(self._buf) > 1:
                self._buf_n -= len(self._buf.pop(0))

    def _measure_offset(self):
        with self._lock:
            data = np.concatenate(self._buf) if self._buf else None
        if data is None:
            self.off = None
            return
        self.off = estimate_offset(data, RATE, MODES[self.mode]["bw"])

    # ------------------------------------------------------------------------------------------------------- the loop
    def _loop(self):
        while self.alive:
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001
                log.warning("aero poll: %s", e)
            end = time.time() + self.poll
            while self.alive and time.time() < end:
                time.sleep(0.1)

    def _read_stats(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.http_port}/api", timeout=1.5) as r:
                return parse_stats(json.loads(r.read().decode("utf-8", "replace")))
        except Exception:  # noqa: BLE001
            return {}

    def _scan_files(self):
        """Read every message file SatDump has finished, then delete it (AndySDR keeps its own record in the daily CSV)."""
        for path in glob.glob(os.path.join(self.out_dir, "**", "*.json"), recursive=True):
            if path in self.seen:
                continue
            try:
                if time.time() - os.path.getmtime(path) < 0.5:
                    continue                      # still being written
                with open(path, "r", encoding="utf-8") as f:
                    js = json.load(f)
            except (OSError, ValueError):
                continue
            self.frames += 1
            m = convert_acars(js)
            if m:
                self.messages += 1
                self.on_message(m)
            try:
                os.remove(path)
            except OSError:
                self.seen.add(path)

    def _poll_once(self):
        st = self._read_stats()
        if st:
            self.stats = st
        self._scan_files()
        self._measure_offset()
        now = time.time()
        locked = bool(self.stats.get("lock"))
        if locked:
            self.t_lock = now
            self.locked_once = True
        up = now - self.t_start
        if self.alive and up > ROTATE_S:
            self.on_log("AERO: starting a fresh SatDump session (housekeeping)")
            self._launch()
            return
        if self.auto and self.alive:
            lost = self.locked_once and now - self.t_lock > LOSS_RESUME_S
            if (not locked and up > self.lock_wait and not self.locked_once) or lost:
                self._next_mode(lost)
        self._publish(locked, up)

    def _next_mode(self, lost):
        i = (AUTO_ORDER.index(self.mode) + 1) % len(AUTO_ORDER)
        self.mode = AUTO_ORDER[i]
        self.locked_once = False
        self.on_log(("AERO: lock lost - " if lost else "AERO: no lock yet - ") + f"trying {MODES[self.mode]['name']}")
        self._launch()

    def _publish(self, locked, up):
        m = MODES[self.mode]
        if up < 12 and not self.stats:
            state = "starting"
        elif locked:
            state = "locked"
        elif self.off:
            state = "carrier"                    # a carrier is there, the decoder has not locked to it (yet)
        else:
            state = "searching"
        s = {"running": True, "mode": self.mode, "mode_name": m["name"], "bps": m["bps"], "auto": self.auto, "state": state,
             "lock": locked, "frames": self.frames, "messages": self.messages, "up": int(up)}
        for k in ("ber", "snr", "peak_snr", "corr"):
            if k in self.stats:
                s[k] = round(self.stats[k], 3)
        if self.off:
            s["off_hz"], s["off_snr"] = round(self.off[0], 1), round(self.off[1], 1)
        self.on_status(s)
