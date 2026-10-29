"""Decoder manager.

Built in:   WEFAX weather fax images.
External (free programs, put them in the 'decoders' folder next to AndySDR.exe):
  * DSD-FME     – DMR, P25 Phase 1/2, NXDN, dPMR, YSF, M17, ProVoice, EDACS, X2-TDMA (voice + data)
  * multimon-ng – POCSAG 512/1200/2400, FLEX, APRS/AFSK1200, DTMF, ZVEI, EAS, Morse, Hapn ...
  * SatDump     – Meteor-M LRPT weather satellite images (137.1 / 137.9 MHz)
  * TETRA       – complex baseband is streamed over UDP for an external TETRA demodulator.
    UK emergency-service TETRA (Airwave) is encrypted: only unencrypted networks can be decoded.

AndySDR feeds DSD-FME exactly like SDR++'s network audio sink (TCP, 48 kHz, 16-bit mono),
so `dsd-fme -i tcp:127.0.0.1:7355` works unchanged.
"""
import glob
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import wave

import numpy as np
from scipy import signal

from .config import app_dir, data_dir
from .wefax import WefaxDecoder

log = logging.getLogger("andysdr.decoders")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

DSD_MODES = {
    "auto": ["-ft"], "dmr": ["-fs"], "p25p1": ["-f1"], "p25p2": ["-f2"],
    "nxdn48": ["-fi"], "nxdn96": ["-fn"], "dpmr": ["-fm"], "ysf": ["-fy"], "m17": ["-fz"],
    "provoice": ["-fp"], "x2tdma": ["-fx"],
}

DECODERS = {
    "dsd":    {"name": "Digital voice (DSD-FME)", "exe": "dsd-fme",
               "help": "DMR, P25, NXDN, dPMR, YSF, M17. Pick the sub-mode or Auto."},
    "pager":  {"name": "Pagers POCSAG/FLEX (multimon-ng)", "exe": "multimon-ng",
               "help": "Shows pager address and message text."},
    "data":   {"name": "APRS / DTMF / ZVEI / EAS (multimon-ng)", "exe": "multimon-ng",
               "help": "Packet radio (AFSK1200), touch-tones, selcall, EAS alerts."},
    "wefax":  {"name": "WEFAX weather fax (built in)", "exe": None,
               "help": "HF fax charts e.g. Northwood 2618.5/4610/8040/11086.5 kHz USB."},
    "lrpt":   {"name": "Meteor-M LRPT satellite (SatDump)", "exe": "satdump",
               "help": "Records the pass, then SatDump makes the images. 137.9 MHz, DIG25 mode."},
    "tetra":  {"name": "TETRA baseband out (UDP)", "exe": None,
               "help": "Streams 48 kHz complex IQ to UDP 127.0.0.1:42000 for a TETRA decoder."},
}


def find_exe(base, overrides=None):
    if overrides and overrides.get(base) and os.path.exists(overrides[base]):
        return overrides[base]
    exe = base + (".exe" if sys.platform.startswith("win") else "")
    roots = [os.path.join(app_dir(), "decoders"), os.path.join(data_dir(), "decoders"), app_dir()]
    for r in roots:
        hits = glob.glob(os.path.join(r, "**", exe), recursive=True)
        if hits:
            return hits[0]
    return shutil.which(base)


def _popen(args, **kw):
    flags = 0
    if sys.platform.startswith("win"):
        flags = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.Popen(args, creationflags=flags, cwd=os.path.dirname(args[0]) or None, **kw)


class _Reader(threading.Thread):
    """Reads a decoder's text output and passes cleaned lines on."""

    def __init__(self, stream, cb, filt=None):
        super().__init__(daemon=True)
        self.stream, self.cb, self.filt = stream, cb, filt

    def run(self):
        for raw in iter(self.stream.readline, b""):
            line = ANSI.sub("", raw.decode("utf-8", "replace")).strip()
            if line and (self.filt is None or self.filt(line)):
                self.cb(line)


class TcpAudioServer(threading.Thread):
    """SDR++ style network sink: any client connecting gets 48 kHz s16le mono."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.clients = []
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(4)
        self.running = True

    def run(self):
        while self.running:
            try:
                c, _ = self.srv.accept()
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.clients.append(c)
            except OSError:
                break

    def send(self, pcm):
        for c in list(self.clients):
            try:
                c.sendall(pcm)
            except OSError:
                self.clients.remove(c)

    def stop(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass


class DecoderManager:
    def __init__(self, settings, emit):
        self.settings = settings
        self.emit = emit           # emit(kind, payload) -> GUI
        self.active = None
        self.sub = "auto"
        self.proc = None
        self.feed_q = None
        self.wefax = None
        self.tcp = None
        self.udp = None
        self.rec = None
        self.rec_path = None
        self.resample_state = None
        self.images_dir = os.path.join(data_dir(), "images")
        self.status = "off"

    # ------------------------------------------------------------------
    def available(self):
        ov = self.settings.get("decoder_paths", {})
        out = []
        for k, d in DECODERS.items():
            path = find_exe(d["exe"], ov) if d["exe"] else "built-in"
            out.append({"id": k, "name": d["name"], "help": d["help"], "path": path or "",
                        "ready": bool(path)})
        return out

    def log(self, text, kind="decoder"):
        self.emit(kind, {"t": time.strftime("%H:%M:%S"), "text": text, "decoder": self.active})

    # ------------------------------------------------------------------
    def start(self, dec_id, sub="auto"):
        self.stop()
        if not dec_id:
            return True
        ov = self.settings.get("decoder_paths", {})
        self.active, self.sub = dec_id, sub
        try:
            if dec_id == "wefax":
                self.wefax = WefaxDecoder(self.images_dir, on_event=lambda k, v: (
                    self.emit("image", {"file": v}) if k == "image" else self.log(v)))
                self.log("WEFAX decoder running – waiting for start tone (free-running meanwhile)")
            elif dec_id == "dsd":
                exe = find_exe("dsd-fme", ov)
                if not exe:
                    raise FileNotFoundError("dsd-fme.exe not found – see Help > Decoders")
                self.tcp = TcpAudioServer(7355)
                self.tcp.start()
                args = [exe, "-i", "tcp:127.0.0.1:7355"] + DSD_MODES.get(sub, ["-ft"])
                extra = self.settings.get("dsd_extra_args", "")
                if extra:
                    args += extra.split()
                self.proc = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
                _Reader(self.proc.stdout, self.log, self._dsd_filter).start()
                self.log("DSD-FME started: " + " ".join(os.path.basename(a) for a in args))
            elif dec_id in ("pager", "data"):
                exe = find_exe("multimon-ng", ov)
                if not exe:
                    raise FileNotFoundError("multimon-ng.exe not found – see Help > Decoders")
                if dec_id == "pager":
                    modes = ["POCSAG512", "POCSAG1200", "POCSAG2400", "FLEX"]
                else:
                    modes = ["AFSK1200", "DTMF", "ZVEI1", "EEA", "EAS", "MORSE_CW"]
                args = [exe, "-t", "raw", "-f", "alpha", "--timestamp"]
                for m in modes:
                    args += ["-a", m]
                args.append("-")
                self.proc = _popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
                _Reader(self.proc.stdout, self.log,
                        lambda l: not l.startswith(("multimon-ng", "Enabled", "Available", "(C)"))).start()
                self._start_feeder()
                self.log("multimon-ng started: " + ", ".join(modes))
            elif dec_id == "lrpt":
                exe = find_exe("satdump", ov)
                ts = time.strftime("%Y%m%d_%H%M%S")
                self.rec_path = os.path.join(data_dir(), "recordings", f"lrpt_{ts}_240k.wav")
                self.rec = wave.open(self.rec_path, "wb")
                self.rec.setnchannels(2)
                self.rec.setsampwidth(2)
                self.rec.setframerate(240000)
                self.log("Recording Meteor LRPT pass (240 kS/s IQ). Press the decoder OFF "
                         "when the pass ends to make the pictures." +
                         ("" if exe else " SatDump not found – recording only."))
            elif dec_id == "tetra":
                self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                port = int(self.settings.get("tetra_udp_port", 42000))
                self.log(f"Streaming 48 kHz complex float32 IQ to UDP 127.0.0.1:{port}. "
                         "Note: encrypted TETRA (e.g. Airwave) cannot be decoded.")
            self.status = "on"
            return True
        except Exception as e:
            self.log(f"Could not start decoder: {e}", "error")
            self.active = None
            self.status = "error"
            return False

    def _dsd_filter(self, line):
        return not line.startswith(("Digital Speech Decoder", "Build", "mbelib", "Audio In",
                                    "Audio Out", "TCP"))

    def _start_feeder(self):
        self.feed_q = queue.Queue(maxsize=200)
        proc = self.proc

        def feeder():
            while proc.poll() is None:
                try:
                    data = self.feed_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:
                    break
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except OSError:
                    break
        threading.Thread(target=feeder, daemon=True).start()

    # ------------------------------------------------------------------
    def stop(self):
        if self.active == "lrpt" and self.rec:
            self.rec.close()
            self.rec = None
            self._run_satdump(self.rec_path)
        if self.feed_q:
            try:
                self.feed_q.put_nowait(None)
            except queue.Full:
                pass
            self.feed_q = None
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None
        if self.tcp:
            self.tcp.stop()
            self.tcp = None
        if self.wefax:
            self.wefax.save()
            self.wefax = None
        if self.udp:
            self.udp.close()
            self.udp = None
        if self.active:
            self.log("Decoder stopped")
        self.active = None
        self.status = "off"

    def _run_satdump(self, path):
        exe = find_exe("satdump", self.settings.get("decoder_paths", {}))
        if not exe or not path:
            return
        out = os.path.join(self.images_dir, os.path.splitext(os.path.basename(path))[0])
        args = [exe, "meteor_m2-x_lrpt", "baseband", path, out,
                "--samplerate", "240000", "--baseband_format", "s16"]
        self.log("Running SatDump – pictures will appear in the Images tab when done")

        def run():
            p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for raw in iter(p.stdout.readline, b""):
                pass
            p.wait()
            pngs = glob.glob(os.path.join(out, "**", "*.png"), recursive=True)
            self.log(f"SatDump finished – {len(pngs)} images")
            for f in pngs[:30]:
                self.emit("image", {"file": os.path.relpath(f, self.images_dir).replace("\\", "/")})
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    def feed(self, out):
        """Called by the radio engine for every 50 ms block."""
        a = self.active
        if not a:
            return
        try:
            if a == "wefax" and self.wefax:
                self.wefax.process(out["audio_ungated"].astype(np.float64))
            elif a == "dsd" and self.tcp:
                d = out["disc"]
                pcm = np.clip(d * 6000, -32767, 32767).astype("<i2").tobytes()
                self.tcp.send(pcm)
            elif a in ("pager", "data") and self.feed_q:
                d = out["disc"]
                r = signal.resample_poly(d, 147, 320)        # 48000 -> 22050
                pcm = np.clip(r * 8000, -32767, 32767).astype("<i2").tobytes()
                try:
                    self.feed_q.put_nowait(pcm)
                except queue.Full:
                    pass
            elif a == "lrpt" and self.rec is not None and out.get("iq240") is not None:
                iq = out["iq240"]
                inter = np.empty(len(iq) * 2, np.float32)
                inter[0::2], inter[1::2] = iq.real, iq.imag
                self.rec.writeframes(np.clip(inter * 16000, -32767, 32767).astype("<i2").tobytes())
            elif a == "tetra" and self.udp and out.get("iq48") is not None:
                port = int(self.settings.get("tetra_udp_port", 42000))
                b = out["iq48"].astype(np.complex64).tobytes()
                for i in range(0, len(b), 8192):
                    self.udp.sendto(b[i:i + 8192], ("127.0.0.1", port))
        except Exception as e:  # never let a decoder crash the radio
            log.exception("decoder feed: %s", e)
