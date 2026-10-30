"""Receiver hardware layer.

* SoapySDR covers practically every SDR: RTL-SDR (Nooelec etc.), SDRplay RSP1A/RSP2/RSP2pro/
  RSPdx/RSPduo (through SDRplay's own API via the SoapySDRPlay3 module), Airspy, HackRF,
  LimeSDR, PlutoSDR, USRP, and SoapyRemote network devices.
* rtl_tcp lets AndySDR use an RTL dongle plugged into another computer.
* "Simulator" produces test signals so every part of the program can be tried without hardware.
"""
import glob
import logging
import os
import socket
import struct
import sys
import threading
import time

import numpy as np

log = logging.getLogger("andysdr.devices")

# --------------------------------------------------------------------------------------
# SoapySDR discovery (bundled with the exe, radioconda, or PothosSDR)
# --------------------------------------------------------------------------------------
_soapy = None
_soapy_error = ""


def _prepare_soapy_paths():
    cands = []
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None
    meipass = getattr(sys, "_MEIPASS", None)
    for base in filter(None, [meipass, exe_dir]):
        cands.append(base)
        cands.append(os.path.join(base, "soapy"))
    # radioconda / miniconda installs
    for env in ("CONDA_PREFIX",):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], "Library"))
    home = os.path.expanduser("~")
    for p in (os.path.join(home, "radioconda", "Library"),
              r"C:\ProgramData\radioconda\Library",
              r"C:\Program Files\PothosSDR",
              r"C:\Program Files (x86)\PothosSDR"):
        cands.append(p)
    # SDRplay API DLL folder (installed by SDRplay's API installer)
    for p in (r"C:\Program Files\SDRplay\API\x64", r"C:\Program Files\SDRplay\API\x86"):
        if os.path.isdir(p) and hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(p)
            except OSError:
                pass
    plugin_paths = []
    for base in cands:
        if not os.path.isdir(base):
            continue
        bindir = os.path.join(base, "bin")
        for d in (base, bindir):
            if os.path.isdir(d) and hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass
                # driver modules load their own DLLs (libusb, rtlsdr...) via the normal search path
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        for mod in glob.glob(os.path.join(base, "lib", "SoapySDR", "modules*")) + \
                glob.glob(os.path.join(base, "SoapySDR", "modules*")):
            plugin_paths.append(mod)
        for sp in glob.glob(os.path.join(base, "lib", "python3*", "site-packages")):
            if sp not in sys.path:
                sys.path.append(sp)
    if plugin_paths and not os.environ.get("SOAPY_SDR_PLUGIN_PATH"):
        os.environ["SOAPY_SDR_PLUGIN_PATH"] = os.pathsep.join(plugin_paths)


def soapy():
    global _soapy, _soapy_error
    if _soapy is None:
        try:
            _prepare_soapy_paths()
            import SoapySDR  # noqa
            _soapy = SoapySDR
        except Exception as e:  # pragma: no cover - depends on host
            _soapy = False
            _soapy_error = str(e)
            log.warning("SoapySDR not available: %s", e)
    return _soapy or None


# --------------------------------------------------------------------------------------
class BaseDevice:
    kind = "base"

    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.sample_rate = 2400000
        self.center = 100e6
        self.gain = 30.0
        self.agc = False

    # hardware description for the GUI
    def info(self):
        lo, hi = self.gain_range()
        return {
            "key": self.key, "label": self.label, "kind": self.kind,
            "gain_min": lo, "gain_max": hi,
            "antennas": self.antennas(), "rates": self.sample_rates(),
            "freq_min": self.freq_range()[0], "freq_max": self.freq_range()[1],
            "settings": self.extra_settings(),
        }

    def open(self): ...
    def close(self): ...
    def read(self, n): raise NotImplementedError
    def set_sample_rate(self, sr): self.sample_rate = sr
    def set_center(self, f): self.center = f
    def set_gain(self, g): self.gain = g
    def set_agc(self, on): self.agc = on
    def set_ppm(self, ppm): ...
    def set_antenna(self, name): ...
    def antennas(self): return []
    def gain_range(self): return (0.0, 50.0)
    def sample_rates(self): return [2400000]
    def freq_range(self): return (24e6, 1766e6)
    def extra_settings(self): return []
    def write_setting(self, key, value): ...


# --------------------------------------------------------------------------------------
class SoapyDevice(BaseDevice):
    kind = "soapy"

    def __init__(self, args, label):
        super().__init__("soapy:" + ",".join(f"{k}={v}" for k, v in args.items()), label)
        self.args = dict(args)
        self.dev = None
        self.stream = None
        self._buf = None
        self.is_sdrplay = "sdrplay" in args.get("driver", "")

    def open(self):
        S = soapy()
        self.dev = S.Device(self.args)
        self.dev.setSampleRate(S.SOAPY_SDR_RX, 0, self.sample_rate)
        try:
            self.dev.setBandwidth(S.SOAPY_SDR_RX, 0, self.sample_rate * 0.8)
        except Exception:
            pass
        self.dev.setFrequency(S.SOAPY_SDR_RX, 0, self.center)
        self.set_agc(self.agc)
        self.set_gain(self.gain)
        self.stream = self.dev.setupStream(S.SOAPY_SDR_RX, S.SOAPY_SDR_CF32, [0])
        self.dev.activateStream(self.stream)
        self._buf = np.zeros(1 << 17, np.complex64)

    def close(self):
        if self.dev is not None:
            S = soapy()
            try:
                self.dev.deactivateStream(self.stream)
                self.dev.closeStream(self.stream)
            except Exception:
                pass
            try:
                S.Device.unmake(self.dev)
            except Exception:
                pass
        self.dev = None

    def read(self, n):
        n = min(n, len(self._buf))
        sr = self.dev.readStream(self.stream, [self._buf], n, timeoutUs=500000)
        if sr.ret <= 0:
            return np.zeros(0, np.complex64)
        return self._buf[:sr.ret].copy()

    def set_sample_rate(self, sr):
        self.sample_rate = sr
        if self.dev:
            S = soapy()
            self.dev.setSampleRate(S.SOAPY_SDR_RX, 0, sr)

    def set_center(self, f):
        self.center = f
        if self.dev:
            self.dev.setFrequency(soapy().SOAPY_SDR_RX, 0, f)

    def _sdrplay_gain(self, g):
        """RSP gain is set as *reductions*: LNA state (RFGR) + IF gain reduction (IFGR 20-59).
        Map our 0..max 'more is louder' value onto them."""
        S = soapy()
        lo, hi = self.gain_range()
        p = 0.0 if hi <= lo else (g - lo) / (hi - lo)
        p = min(1.0, max(0.0, p))
        try:
            r = self.dev.getGainRange(S.SOAPY_SDR_RX, 0, "RFGR")
            lna_max = int(r.maximum())
            self.dev.setGain(S.SOAPY_SDR_RX, 0, "RFGR", round((1 - p) * lna_max))
        except Exception:
            pass
        try:
            self.dev.setGain(S.SOAPY_SDR_RX, 0, "IFGR", 59 - p * (59 - 20))
        except Exception:
            pass

    def set_gain(self, g):
        self.gain = g
        if not self.dev or self.agc:
            return
        S = soapy()
        if self.is_sdrplay:
            self._sdrplay_gain(g)
        else:
            self.dev.setGain(S.SOAPY_SDR_RX, 0, g)

    def set_agc(self, on):
        self.agc = on
        if self.dev:
            S = soapy()
            try:
                self.dev.setGainMode(S.SOAPY_SDR_RX, 0, bool(on))
            except Exception:
                pass
            if not on:
                self.set_gain(self.gain)

    def set_ppm(self, ppm):
        if self.dev:
            try:
                self.dev.setFrequencyCorrection(soapy().SOAPY_SDR_RX, 0, float(ppm))
            except Exception:
                pass

    def set_antenna(self, name):
        if self.dev and name:
            try:
                self.dev.setAntenna(soapy().SOAPY_SDR_RX, 0, name)
            except Exception as e:
                log.warning("antenna: %s", e)

    def antennas(self):
        if not self.dev:
            return []
        try:
            return list(self.dev.listAntennas(soapy().SOAPY_SDR_RX, 0))
        except Exception:
            return []

    def gain_range(self):
        if self.is_sdrplay:
            return (0.0, 100.0)  # percent style for RSP
        if not self.dev:
            return (0.0, 50.0)
        try:
            r = self.dev.getGainRange(soapy().SOAPY_SDR_RX, 0)
            return (float(r.minimum()), float(r.maximum()))
        except Exception:
            return (0.0, 50.0)

    def sample_rates(self):
        if not self.dev:
            return [2400000]
        try:
            rates = [int(r) for r in self.dev.listSampleRates(soapy().SOAPY_SDR_RX, 0)]
            return sorted(set(rates)) or [2400000]
        except Exception:
            return [2400000]

    def freq_range(self):
        if not self.dev:
            return (24e6, 1766e6)
        try:
            rr = self.dev.getFrequencyRange(soapy().SOAPY_SDR_RX, 0)
            return (min(r.minimum() for r in rr), max(r.maximum() for r in rr))
        except Exception:
            return (1e3, 6e9)

    def extra_settings(self):
        """Device-specific switches (bias-T, notch filters, RSP2 Hi-Z port, direct sampling...)."""
        out = []
        if not self.dev:
            return out
        try:
            for a in self.dev.getSettingInfo():
                try:
                    val = self.dev.readSetting(a.key)
                except Exception:
                    val = a.value
                out.append({"key": a.key, "name": a.name or a.key, "type": int(a.type),
                            "value": val, "options": list(a.options or []),
                            "description": a.description})
        except Exception:
            pass
        return out

    def write_setting(self, key, value):
        if self.dev:
            self.dev.writeSetting(key, str(value))


# --------------------------------------------------------------------------------------
class RtlTcpDevice(BaseDevice):
    """Network RTL-SDR served by rtl_tcp on another computer."""
    kind = "rtl_tcp"

    def __init__(self, host, port=1234):
        super().__init__(f"rtltcp:{host}:{port}", f"rtl_tcp {host}:{port}")
        self.host, self.port = host, int(port)
        self.sock = None
        self._rest = b""

    def _cmd(self, c, v):
        if self.sock:
            self.sock.sendall(struct.pack(">BI", c, int(v) & 0xFFFFFFFF))

    def open(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.recv(12)  # dongle info header
        self._cmd(0x02, self.sample_rate)
        self._cmd(0x01, self.center)
        self.set_agc(self.agc)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def read(self, n):
        need = n * 2
        data = self._rest
        while len(data) < need:
            chunk = self.sock.recv(need - len(data))
            if not chunk:
                raise IOError("rtl_tcp connection closed")
            data += chunk
        self._rest = b""
        a = np.frombuffer(data, np.uint8).astype(np.float32)
        a = (a - 127.5) / 127.5
        return (a[0::2] + 1j * a[1::2]).astype(np.complex64)

    def set_sample_rate(self, sr):
        self.sample_rate = sr
        self._cmd(0x02, sr)

    def set_center(self, f):
        self.center = f
        self._cmd(0x01, f)

    def set_gain(self, g):
        self.gain = g
        if not self.agc:
            self._cmd(0x04, int(g * 10))

    def set_agc(self, on):
        self.agc = on
        self._cmd(0x03, 0 if on else 1)
        self._cmd(0x08, 1 if on else 0)
        if not on:
            self._cmd(0x04, int(self.gain * 10))

    def set_ppm(self, ppm):
        self._cmd(0x05, int(ppm))

    def gain_range(self):
        return (0.0, 49.6)

    def sample_rates(self):
        return [1024000, 1920000, 2048000, 2400000, 2880000]

    def extra_settings(self):
        return [{"key": "biastee", "name": "Bias-T", "type": 0, "value": "false",
                 "options": [], "description": "Power for an active antenna/LNA"}]

    def write_setting(self, key, value):
        if key == "biastee":
            self._cmd(0x0E, 1 if str(value).lower() in ("1", "true", "on") else 0)


# --------------------------------------------------------------------------------------
def _cis(ph):
    """exp(j*ph) as complex64 – cos/sin in float32 is many times faster than numpy's complex exp."""
    p = np.asarray(ph, np.float32)
    out = np.empty(p.shape, np.complex64)
    out.real = np.cos(p)
    out.imag = np.sin(p)
    return out


class SimDevice(BaseDevice):
    """Built-in signal generator for demonstrating / testing AndySDR without hardware."""
    kind = "sim"

    SIGNALS = [
        # freq Hz, kind, level (amplitude), on/off seconds (0 = continuous)
        (145500000, "nfm_voice", 0.05, 4.0),
        (145550000, "nfm_tone", 0.02, 0.0),
        (145612500, "nfm_voice", 0.01, 3.0),
        (118700000, "am_voice", 0.05, 5.0),
        (98500000, "wfm", 0.2, 0.0),
        (153350000, "fsk", 0.04, 2.0),
        (446006250, "nfm_voice", 0.03, 3.5),
        (8040000, "wefax", 0.05, 0.0),
        (145437500, "dmr", 0.03, 6.0),       # DMR handset: 4FSK 4800 Bd, 30 ms TDMA bursts
        (144800000, "aprs", 0.03, 2.5),      # APRS AFSK 1200
        (145800000, "sstv", 0.04, 0.0),      # SSTV Robot 36 test card, repeating (like the ISS)
    ]

    def __init__(self):
        super().__init__("sim", "Simulator (test signals)")
        self.t0 = time.time()
        self.n = 0
        self._last = time.time()
        self._ph = {}
        self._osc = {}

    def open(self):
        self.n = 0
        self._last = time.time()

    def gain_range(self):
        return (0.0, 50.0)

    def sample_rates(self):
        return [960000, 1920000, 2400000]

    def freq_range(self):
        return (1e5, 2e9)

    def _audio(self, kind, t):
        if kind in ("nfm_voice", "am_voice", "wfm"):
            # warbling "voice-like" multi-tone
            return 0.5 * np.sin(2 * np.pi * (600 + 250 * np.sin(2 * np.pi * 3 * t)) * t) + \
                0.3 * np.sin(2 * np.pi * 1300 * t)
        if kind == "nfm_tone":
            return np.sin(2 * np.pi * 1000 * t)
        if kind == "fsk":
            bits = (np.floor(t * 1200) * 2654435761 % 7 > 3)
            return np.where(bits, 1.0, -1.0)
        if kind == "dmr":
            sym = (np.floor(t * 4800).astype(np.int64) * 2654435761 >> 7) % 4
            return np.array([-1.0, -1 / 3, 1 / 3, 1.0])[sym]
        if kind == "aprs":
            bits = (np.floor(t * 1200).astype(np.int64) * 2654435761 >> 5) % 2
            f = np.where(bits == 1, 1200.0, 2200.0)
            ph = getattr(self, "_aprs_ph", 0.0) + 2 * np.pi * np.cumsum(f) / self.sample_rate
            self._aprs_ph = float(ph[-1] % (2 * np.pi))
            return np.sin(ph)
        if kind == "sstv":
            if not hasattr(self, "_sstv_audio"):
                from .sstv import encode
                self._sstv_audio = np.concatenate((encode("Robot 36"), np.zeros(48000 * 3, np.float32)))
            L = len(self._sstv_audio)
            return self._sstv_audio[(t * 48000).astype(np.int64) % L]
        return np.zeros_like(t)

    def read(self, n):
        fs = self.sample_rate
        # pace at real time
        now = time.time()
        due = self._last + n / fs
        if due > now:
            time.sleep(due - now)
        self._last = max(due, time.time() - 0.5)
        idx = self.n + np.arange(n)
        self.n += n
        t = idx / fs
        rng = np.random.default_rng()
        noise_amp = 0.002 * (10 ** ((50 - self.gain) / 40))
        x = np.empty(n, np.complex64)
        x.real = rng.standard_normal(n, np.float32) * noise_amp
        x.imag = rng.standard_normal(n, np.float32) * noise_amp
        for f, kind, amp, period in self.SIGNALS:
            off = f - self.center
            if abs(off) > fs * 0.48:
                continue
            if period > 0 and (int(t[0] / period) % 2 == 1):
                continue
            if kind == "wefax":
                # USB audio tone 1500 Hz (black) .. 2300 Hz (white); 120 lines/min test card
                pos = (t % 0.5) / 0.5
                line = np.floor(t / 0.5)
                val = (((pos * 8 + line / 40.0) % 1.0) > 0.5).astype(float)
                val = np.where(pos < 0.05, 1.0, val)
                f_audio = 1500.0 + 800.0 * val
                ph0 = getattr(self, "_wf_ph", 0.0)
                ph = ph0 + 2 * np.pi * np.cumsum(f_audio) / fs
                self._wf_ph = float(ph[-1] % (2 * np.pi))
                bb = _cis(ph)
            elif kind in ("nfm_voice", "nfm_tone", "fsk", "dmr", "aprs", "sstv"):
                a = self._audio(kind, t)
                if kind == "dmr":       # smooth the symbols a little and key the TDMA bursts
                    c = np.concatenate(([0.0], np.cumsum(a)))
                    a = np.concatenate((a[:250], (c[250:] - c[:-250])[: len(a) - 250] / 250))
                dev = {"fsk": 4500, "dmr": 1944, "sstv": 3000}.get(kind, 2500)
                ph = self._ph.get(f, 0.0) + 2 * np.pi * dev * np.cumsum(a) / fs
                self._ph[f] = float(ph[-1] % (2 * np.pi))
                bb = None                       # FM: one complex exponential for phase + carrier
            elif kind == "am_voice":
                bb = (1 + 0.6 * self._audio(kind, t)).astype(np.complex128)
            elif kind == "wfm":
                a = self._audio(kind, t)
                ph = self._ph.get(f, 0.0) + 2 * np.pi * 60000 * np.cumsum(a) / fs
                self._ph[f] = float(ph[-1] % (2 * np.pi))
                bb = _cis(ph)
            else:
                continue
            g = amp * (10 ** ((self.gain - 30) / 40))
            # carrier: cached oscillator for this offset, rotated to the block start
            key = (off, n, fs)
            osc = self._osc.get(key)
            if osc is None:
                if len(self._osc) > 64:
                    self._osc.clear()
                osc = self._osc[key] = _cis(np.mod(2 * np.pi * off / fs * np.arange(n), 2 * np.pi))
            rot = np.complex64(np.exp(1j * ((2 * np.pi * off / fs * idx[0]) % (2 * np.pi))) * g)
            if bb is None:
                sig = _cis(ph)
                if kind == "dmr":
                    sig *= ((t % 0.06) < 0.0275)
                x += sig * osc * rot
            else:
                x += (bb * osc * rot).astype(np.complex64)
        return x.astype(np.complex64)


# --------------------------------------------------------------------------------------
def list_devices(extra_rtltcp=None):
    """Enumerate every receiver we can find."""
    out = []
    # SDRplay RSPs: talk to SDRplay's own API directly (preferred)
    native_rsp = []
    try:
        from . import sdrplay
        native_rsp = sdrplay.list_sdrplay()
        out.extend(native_rsp)
    except Exception as e:
        log.warning("SDRplay API: %s", e)
    S = soapy()
    if S:
        try:
            for d in S.Device.enumerate():
                args = {k: d[k] for k in d.keys()}
                drv = args.get("driver", "?")
                if drv in ("audio",) or (native_rsp and "sdrplay" in drv):
                    continue
                label = args.get("label") or f"{drv} {args.get('serial', '')}".strip()
                keep = {k: args[k] for k in ("driver", "serial", "label") if k in args}
                if "sdrplay" in drv and "label" in keep:
                    keep.pop("label")
                dev = SoapyDevice(keep, label)
                out.append(dev)
        except Exception as e:
            log.warning("Soapy enumerate failed: %s", e)
    for spec in extra_rtltcp or []:
        try:
            host, port = spec.rsplit(":", 1)
            out.append(RtlTcpDevice(host, int(port)))
        except ValueError:
            pass
    out.append(SimDevice())
    return out


def soapy_status():
    try:
        from . import sdrplay
        sp = sdrplay.api_status()
    except Exception as e:
        sp = {"available": False, "error": str(e)}
    S = soapy()
    if S:
        try:
            return {"available": True, "version": S.getAPIVersion(),
                    "modules": list(S.listModules()), "sdrplay": sp}
        except Exception:
            return {"available": True, "sdrplay": sp}
    return {"available": False, "error": _soapy_error, "sdrplay": sp}
