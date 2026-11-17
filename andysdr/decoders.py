"""Decoder manager.

Built in:   WEFAX weather fax images, SSTV pictures (Martin, Scottie, Robot, PD modes).
External (free programs, put them in the 'decoders' folder next to AndySDR.exe):
  * DSD-FME     – DMR, P25 Phase 1/2, NXDN, dPMR, YSF, M17, ProVoice, EDACS, X2-TDMA (voice + data)
  * multimon-ng – POCSAG 512/1200/2400, FLEX, APRS/AFSK1200, DTMF, ZVEI, EAS, Morse, Hapn ...
  * SatDump     – Meteor-M LRPT weather satellite images (137.1 / 137.9 MHz)
  * ADS-B       – built in: 1090 MHz Mode S / ADS-B aircraft tracking (see adsb.py)
  * TETRA       – complex baseband is streamed over UDP for an external TETRA demodulator.
    UK emergency-service TETRA (Airwave) is encrypted: only unencrypted networks can be decoded.

AndySDR feeds DSD-FME exactly like SDR++'s network audio sink (TCP, 48 kHz, 16-bit mono),
so `dsd-fme -i tcp:127.0.0.1:7355` works unchanged.
"""
import glob
import logging
import math
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
from .iqbridge import IqBridge
from .ft8 import Ft8Decoder, match_freq
from . import dab as dablib
from .sstv import SstvDecoder
from .wefax import WefaxDecoder
from .tetrademod import TetraDemod

log = logging.getLogger("andysdr.decoders")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

DSD_MODES = {
    "auto": ["-ft"], "dmr": ["-fs"], "p25p1": ["-f1"], "p25p2": ["-f2"],
    "nxdn48": ["-fi"], "nxdn96": ["-fn"], "dpmr": ["-fm"], "ysf": ["-fy"], "m17": ["-fz"],
    "provoice": ["-fp"], "x2tdma": ["-fx"],
    "dstar": ["-fd"],                                   # D-STAR voice
    "dmr_inv": ["-fs", "-xr"], "dpmr_inv": ["-fm", "-xd"],   # inverted-polarity DMR / dPMR (some radios and RTL dongles)
}

DECODERS = {
    "dsd":    {"name": "Digital voice (DSD-FME)", "exe": "dsd-fme",
               "help": "DMR, P25, NXDN, dPMR, YSF, M17, D-STAR. Pick the sub-mode or Auto. Use the Inverted entries if a DMR/dPMR signal shows sync but garbage."},
    "pager":  {"name": "Pagers POCSAG/FLEX (multimon-ng)", "exe": "multimon-ng",
               "help": "Shows pager address and message text."},
    "data":   {"name": "APRS / DTMF / ZVEI / EAS (multimon-ng)", "exe": "multimon-ng",
               "help": "Packet radio (AFSK1200), touch-tones, selcall, EAS alerts."},
    "wefax":  {"name": "WEFAX weather fax (built in)", "exe": None,
               "help": "HF fax charts e.g. Northwood 2618.5/4610/8040/11086.5 kHz USB."},
    "sstv":   {"name": "SSTV pictures (built in)", "exe": None,
               "help": "Slow-scan TV: ISS 145.800 NFM, 2 m 144.500 NFM, 20 m 14.230 USB. Mode found from the VIS header."},
    "lrpt":   {"name": "Meteor-M LRPT satellite (SatDump)", "exe": "satdump",
               "help": "Records the pass, then SatDump makes the images. 137.9 MHz, DIG25 mode."},
    "ft8":    {"name": "FT8 / FT4 weak-signal digital (ft8_lib)", "exe": "decode_ft8",
               "help": "Decodes FT8 (15 s) or FT4 (7.5 s) in USB mode – tune the dial frequency (e.g. 14.074 MHz). Starts by itself on FT8/FT4 channels from the band plan."},
    "dab":    {"name": "DAB / DAB+ digital radio (welle-cli)", "exe": "welle-cli",
               "help": "UK Band III DAB radio. Choose the multiplex in the DAB tab; stations appear there with a player. Experimental."},
    "goes":   {"name": "GOES HRIT weather satellite (SatDump)", "exe": "satdump",
               "help": "1694.1 MHz GOES-East/West images and EMWIN. Needs a dish + L-band LNA; the receiver is shared with SatDump over a local rtl_tcp link."},
    "adsb":   {"name": "ADS-B aircraft 1090 MHz (built in)", "exe": None,
               "help": "Tunes to 1090 MHz and tracks aircraft (callsign, position, altitude, speed). See the ADS-B tab."},
    "meshsdr": {"name": "MeshCore with just an SDR (built in, receive only)", "exe": None,
               "help": "Decodes MeshCore LoRa packets straight from the SDR: lists repeaters, room servers and other nodes from their adverts and reads public / #hashtag channel messages. No LoRa board needed. See the MESHCORE tab."},
    "tetra":  {"name": "TETRA (built-in demodulator + osmo-tetra)", "exe": "tetra-rx",
               "help": "Decodes the TETRA downlink (25 kHz, DIG25 mode, tune to the carrier centre): network codes (MCC/MNC), "
                       "cell and call set-up and text messages. Encrypted traffic (UK Airwave) shows signalling only, no speech."},
}


def _free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def find_exe(base, overrides=None):
    if overrides and overrides.get(base) and os.path.exists(overrides[base]):
        return overrides[base]
    exe = base + (".exe" if sys.platform.startswith("win") else "")
    roots = [os.path.join(app_dir(), "decoders"), os.path.join(data_dir(), "decoders"), app_dir()]
    for r in roots:
        hits = glob.glob(os.path.join(r, "**", exe), recursive=True)
        # top-most copy first (DSD-FME zips also contain patched variants in sub-folders)
        hits.sort(key=lambda h: (h.count(os.sep), len(h)))
        if hits:
            return hits[0]
    return shutil.which(base)


_PULSE = {"proc": None}


def _ensure_pulse(dsd_exe, log):
    """DSD-FME's Windows release is a Cygwin build that plays voice through PulseAudio.
    If the release ships pulseaudio.exe, start it (once) so decoded voice is heard."""
    if not sys.platform.startswith("win"):
        return
    p = _PULSE["proc"]
    if p and p.poll() is None:
        return
    root = os.path.dirname(dsd_exe)
    dec_root = os.path.normcase(os.path.join(app_dir(), "decoders"))
    for _ in range(3):  # look in the exe folder and up to two parents inside decoders/
        hits = sorted(glob.glob(os.path.join(root, "**", "pulseaudio.exe"), recursive=True),
                      key=lambda h: h.count(os.sep))
        if hits:
            try:
                _PULSE["proc"] = _popen([hits[0], "--exit-idle-time=-1"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL)
                time.sleep(1.5)
                log("Started DSD-FME's PulseAudio for voice output")
            except OSError as e:
                log(f"PulseAudio did not start ({e}) – DSD-FME will decode without sound")
            return
        parent = os.path.dirname(root)
        if parent == root or not os.path.normcase(parent).startswith(dec_root):
            return
        root = parent


def stop_pulse():
    p = _PULSE["proc"]
    if p and p.poll() is None:
        try:
            p.terminate()
        except OSError:
            pass
    _PULSE["proc"] = None


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


class DiscConditioner:
    """Cleans the FM-discriminator audio before DSD-FME sees it.

    A 4FSK signal (DMR, P25, NXDN, dPMR, M17, YSF) has symbols spread evenly either side of zero, so
    any steady offset in the discriminator output is purely tuning error (a few ppm of an RTL
    dongle's crystal at 450 MHz is already ~1 kHz, about half the DMR outer-symbol deviation).
    A slow, signal-gated average of that offset is subtracted so the decoder gets a centred eye.
    No filtering is applied (DSD-FME wants unfiltered discriminator audio)."""

    def __init__(self, fs=48000, tau=1.0):
        self.alpha = 1.0 - math.exp(-1.0 / (fs * tau))
        self.dc = 0.0

    def process(self, d, signal_present=True):
        if len(d) == 0:
            return d
        if signal_present:
            m = float(np.mean(d))
            # block mean folded into the running estimate (block = 50 ms ~ 2400 samples)
            k = 1.0 - (1.0 - self.alpha) ** len(d)
            self.dc += k * (m - self.dc)
        return d - np.float32(self.dc)


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
        self.sstv = None
        self.tcp = None
        self.udp = None
        self.tetra_demod = None
        self.tetra_state = {}
        self.rec = None
        self.rec_path = None
        self.resample_state = None
        self.images_dir = os.path.join(data_dir(), "images")
        self.status = "off"
        self.on_pager = None       # set by the radio: on_pager(line) -> Pager Messages tab
        self.ft8 = None
        self.dab_port = 0
        self.freq_fn = None        # set by the radio: () -> tuned frequency (Hz)
        self.dial_fn = None        # set by the radio: () -> FT8 dial frequency of the tuned channel (Hz)
        self.ft8_history = []
        self.bridge = None         # IqBridge (rtl_tcp server) while GOES / DAB decoders run
        self.rate_fn = lambda: 2400000   # set by the radio: current receiver sample rate
        self._watch_stop = True
        self.adsb = None           # set by the radio: the ADS-B engine (takes IQ, not audio)
        self.meshsdr = None        # set by the radio: the SDR-only MeshCore receiver (takes IQ)

    # ------------------------------------------------------------------
    def available(self):
        ov = self.settings.get("decoder_paths", {})
        out = []
        for k, d in DECODERS.items():
            path = find_exe(d["exe"], ov) if d["exe"] else "built-in"
            out.append({"id": k, "name": d["name"], "help": d["help"], "path": path or "",
                        "ready": bool(path)})
        return out

    def download_missing(self, done=None):
        """Fetch DSD-FME / multimon-ng / SatDump from GitHub into %APPDATA%\\AndySDR\\decoders."""
        if getattr(self, "_fetching", False):
            self.log("Decoder download already running")
            return
        self._fetching = True

        def run():
            from . import fetchdecoders
            dest = os.path.join(data_dir(), "decoders")
            try:
                for prog in fetchdecoders.PROGRAMS:
                    base = fetchdecoders.PROGRAMS[prog]["exe"]
                    if find_exe(base, self.settings.get("decoder_paths", {})):
                        self.log(f"{prog}: already installed")
                        continue
                    fetchdecoders.fetch(prog, dest, log=self.log)
                fetchdecoders.write_licences(dest)
                self.log("Decoder download finished")
            except Exception as e:  # noqa: BLE001
                self.log(f"Decoder download failed: {e}", "error")
            finally:
                self._fetching = False
                if done:
                    done()
        threading.Thread(target=run, daemon=True).start()

    def log(self, text, kind="decoder"):
        self.emit(kind, {"t": time.strftime("%H:%M:%S"), "text": text, "decoder": self.active})

    # ------------------------------------------------------------------
    def _ft8_msg(self, m):
        self.ft8_history = (self.ft8_history + [m])[-300:]
        self.emit("ft8", m)

    def _img_event(self, k, v):
        if k == "image":
            self.emit("image", {"file": v})
        else:
            self.log(v)

    def adopt_sstv(self, dec):
        """Take over an SSTV decoder that already found a VIS header (auto-detect)."""
        self.stop()
        self.active, self.sub = "sstv", "auto"
        dec.on_event = self._img_event
        dec.promote()
        self.sstv = dec
        self.status = "on"

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
            elif dec_id == "sstv":
                self.sstv = SstvDecoder(self.images_dir, on_event=self._img_event)
                self.log("SSTV decoder running – waiting for a picture (VIS header)")
            elif dec_id == "dsd":
                exe = find_exe("dsd-fme", ov)
                if not exe:
                    raise FileNotFoundError("dsd-fme.exe not found – see Help > Decoders")
                _ensure_pulse(exe, self.log)
                self.tcp = TcpAudioServer(7355)
                self.tcp.start()
                self.dsd_cond = DiscConditioner()
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
                out_cb = self._pager_line if dec_id == "pager" else self.log
                _Reader(self.proc.stdout, out_cb,
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
            elif dec_id == "ft8":
                exe = find_exe("decode_ft8", ov)
                if not exe:
                    raise FileNotFoundError("decode_ft8.exe not found – see Help > Decoders")
                proto = sub if sub in ("ft8", "ft4") else (self.freq_fn and (match_freq(self.freq_fn(), "USB") or ("ft8",))[0]) or "ft8"
                self.sub = proto
                self.ft8 = Ft8Decoder(exe, proto, on_message=self._ft8_msg, on_log=self.log, dial=self.dial_fn)
                self.log(f"{proto.upper()} decoder running – decoding at the end of each {self.ft8.slot_len:g} s UTC slot. "
                         "Keep the PC clock accurate (Windows: Settings > Time > Sync now).")
            elif dec_id == "dab":
                exe = find_exe("welle-cli", ov)
                if not exe:
                    raise FileNotFoundError("welle-cli.exe not found – see Help > Decoders")
                ch = sub.upper() if sub and sub.upper() in dablib.CHANNELS else "12B"
                self.sub = ch
                self.bridge = IqBridge(out_rate=dablib.DAB_RATE)
                port = self.bridge.start()
                self.dab_port = _free_port()
                args = [exe, "-c", ch, "-w", str(self.dab_port), "-F", f"rtl_tcp,127.0.0.1:{port}"]
                self.proc = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                _Reader(self.proc.stdout, self.log, lambda l: any(k in l for k in ("Ensemble", "ensemble", "rror", "Service", "Found", "Lock", "SNR"))).start()
                self.log(f"DAB: welle-cli started on block {ch} ({dablib.CHANNELS[ch] / 1e6:.3f} MHz) – stations appear in the DAB tab")
            elif dec_id == "goes":
                exe = find_exe("satdump", ov)
                if not exe:
                    raise FileNotFoundError("satdump.exe not found – see Help > Decoders")
                self.bridge = IqBridge(out_rate=None)
                port = self.bridge.start()
                out = os.path.join(self.images_dir, "goes_" + time.strftime("%Y%m%d_%H%M%S"))
                fs = int(self.rate_fn())
                self.bridge.out_rate = fs
                args = [exe, "live", "goes_hrit", out, "--source", "rtltcp", "--ip_address", "127.0.0.1",
                        "--port", str(port), "--samplerate", str(fs), "--frequency", "1694.1e6", "--gain", "30"]
                self.proc = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                _Reader(self.proc.stdout, self.log, lambda l: "(D)" not in l).start()
                self._watch_images(out)
                self.log("SatDump (GOES HRIT) started on 1694.1 MHz – images appear in Weather images as they complete")
            elif dec_id == "meshsdr":
                if self.meshsdr is None:
                    raise RuntimeError("MeshCore SDR receiver not available")
                self.meshsdr.start()
                self.log("MeshCore SDR receiver running – nodes and messages appear in the MESHCORE tab (receive only).")
            elif dec_id == "adsb":
                if self.adsb is None:
                    raise RuntimeError("ADS-B engine not available")
                self.adsb.start()
                self.log("ADS-B decoder running on 1090 MHz – needs a receiver running at 2 MS/s or more. "
                         "Aircraft appear in the ADS-B tab.")
            elif dec_id == "tetra":
                exe = find_exe("tetra-rx", ov)
                if exe:
                    self.tetra_demod = TetraDemod(48000.0)
                    self.tetra_state = {"t0": time.time(), "synced": False, "last": {}, "recent": {}}
                    self.proc = _popen([exe, "-i", "-a", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
                    _Reader(self.proc.stdout, self._tetra_line, self._tetra_keep).start()
                    self._start_feeder()
                    self.log("TETRA decoder started – tune to the centre of a 25 kHz TETRA carrier (DIG25 mode). "
                             "Encrypted networks (UK Airwave) show signalling only, not speech.")
                else:
                    self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    port = int(self.settings.get("tetra_udp_port", 42000))
                    self.log("tetra-rx not found (Help > Decoders). Streaming 48 kHz complex float32 IQ to "
                             f"UDP 127.0.0.1:{port} for an external TETRA decoder instead.")
            self.status = "on"
            return True
        except Exception as e:
            self.log(f"Could not start decoder: {e}", "error")
            self.active = None
            self.status = "error"
            return False

    def _pager_line(self, line):
        self.log(line)
        if self.on_pager:
            try:
                self.on_pager(line)
            except Exception as e:  # noqa: BLE001 - never let the message list stop the decoder
                log.exception("pager: %s", e)

    _TETRA_NOISE = ("BURST", "found SYNC", "####", "CRC COMP", "### AFC", "TMV-UNITDATA", "TMB-SAP", "SB1 ", "EOF")

    def _tetra_keep(self, line):
        st = self.tetra_state
        if line.startswith("TMB-SAP SYNC"):
            m = re.search(r"CC \d+\(0x([0-9a-fA-F]+)\).*?FN\s*\d+\(\s*(\d+)\).*?MCC \d+\((\d+)\) MNC \d+\((\d+)\)", line)
            if m:
                cc, fn, mcc, mnc = int(m.group(1), 16), int(m.group(2)), int(m.group(3)), int(m.group(4))
                st["last_sync"] = time.time()
                info = {"mcc": mcc, "mnc": mnc, "cc": cc}
                if info != st.get("last"):
                    st["last"] = info
                    st["synced"] = True
                    self.log(f"TETRA network found: MCC {mcc}  MNC {mnc}  colour code {cc}  (frame {fn})")
                elif not st.get("synced"):
                    st["synced"] = True
            return False
        if line.startswith(self._TETRA_NOISE):
            return False
        # identical lines repeat every frame: show each once per 5 s
        now = time.time()
        rec = st.setdefault("recent", {})
        if now - rec.get(line, 0) < 5:
            return False
        rec[line] = now
        if len(rec) > 400:
            for k in [k for k, t in rec.items() if now - t > 5]:
                rec.pop(k, None)
        return True

    def _tetra_line(self, line):
        self.log(line)

    def _tetra_watch(self):
        st = self.tetra_state
        now = time.time()
        if st.get("synced") and now - st.get("last_sync", now) > 20 and not st.get("lost"):
            st["lost"] = True
            self.log("TETRA sync lost – signal gone or too weak.")
        elif st.get("synced") and now - st.get("last_sync", 0) <= 20:
            st["lost"] = False
        if not st.get("synced") and now - st.get("hint", st.get("t0", now)) > 20:
            st["hint"] = now
            q = int(100 * self.tetra_demod.locked_metric)
            self.log(f"TETRA: no sync yet (symbol quality {q}%, frequency error {self.tetra_demod.freq:+.0f} Hz). "
                     "Check you are on the exact carrier centre in DIG25 mode, the gain, and that the network is "
                     "a TETRA downlink.")

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
        if self.sstv:
            if self.sstv.state == "image":
                self.sstv.save()
            self.sstv = None
        if self.udp:
            self.udp.close()
            self.udp = None
        self.tetra_demod = None
        if self.active == "adsb" and self.adsb:
            self.adsb.stop()
        if self.active == "meshsdr" and self.meshsdr:
            self.meshsdr.stop()
        if self.bridge:
            self.bridge.stop()
            self.bridge = None
        self._watch_stop = True
        if self.ft8:
            self.ft8.stop()
            self.ft8 = None
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
    def feed_iq(self, iq, fs, offset=0.0):
        """Decoders that work on the raw IQ stream (ADS-B, GOES) instead of demodulated audio."""
        if self.active == "adsb" and self.adsb:
            self.adsb.feed(iq, fs)
        elif self.active == "meshsdr" and self.meshsdr:
            self.meshsdr.feed(iq, fs, offset)
        elif self.active in ("goes", "dab") and self.bridge:
            self.bridge.feed(iq, fs, offset)

    def _watch_images(self, out):
        """Report new PNGs a long-running SatDump process writes."""
        self._watch_stop = False
        seen = set()

        def run():
            while not self._watch_stop:
                for f in glob.glob(os.path.join(out, "**", "*.png"), recursive=True):
                    if f not in seen and time.time() - os.path.getmtime(f) > 5:
                        seen.add(f)
                        self.emit("image", {"file": os.path.relpath(f, self.images_dir).replace("\\", "/")})
                time.sleep(10)
        threading.Thread(target=run, name="goes-images", daemon=True).start()

    def feed(self, out):
        """Called by the radio engine for every 50 ms block."""
        a = self.active
        if not a:
            return
        try:
            if a == "wefax" and self.wefax:
                self.wefax.process(out["audio_ungated"].astype(np.float64))
            elif a == "sstv" and self.sstv:
                self.sstv.process(out["audio_ungated"])
            elif a == "ft8" and self.ft8:
                self.ft8.process(out["audio_ungated"])
            elif a == "dsd" and self.tcp:
                d = out["disc"]
                if self.settings.get("dsd_dc_remove", True) and getattr(self, "dsd_cond", None):
                    d = self.dsd_cond.process(d, bool(out.get("squelch_open", True)))
                gain = float(self.settings.get("dsd_gain", 6000) or 6000)
                pcm = np.clip(d * gain, -32767, 32767).astype("<i2").tobytes()
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
            elif a == "tetra" and self.tetra_demod is not None and self.feed_q and out.get("iq48") is not None:
                steps = self.tetra_demod.process(out["iq48"])
                if len(steps):
                    try:
                        self.feed_q.put_nowait(steps.astype("<f4").tobytes())
                    except queue.Full:
                        pass
                self._tetra_watch()
            elif a == "tetra" and self.udp and out.get("iq48") is not None:
                port = int(self.settings.get("tetra_udp_port", 42000))
                b = out["iq48"].astype(np.complex64).tobytes()
                for i in range(0, len(b), 8192):
                    self.udp.sendto(b[i:i + 8192], ("127.0.0.1", port))
        except Exception as e:  # never let a decoder crash the radio
            log.exception("decoder feed: %s", e)
