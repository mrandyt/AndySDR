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

    def _make(self, S):
        """SoapySDR::Device::make() can say 'no match' for the very args enumerate() returned
        (seen with RTL dongles on Windows). Try progressively looser args before giving up."""
        tries = [dict(self.args)]
        tries.append({k: v for k, v in self.args.items() if k != "label"})
        if "driver" in self.args:
            tries.append({"driver": self.args["driver"]})
        try:
            for d in S.Device.enumerate({"driver": self.args.get("driver", "")}):
                full = {k: d[k] for k in d.keys()}
                if not self.args.get("serial") or full.get("serial") == self.args.get("serial"):
                    tries.insert(1, full)
        except Exception:
            pass
        err = None
        seen = []
        for a in tries:
            if a in seen:
                continue
            seen.append(a)
            try:
                return S.Device(a)
            except Exception as e:  # noqa: BLE001
                err = e
                log.warning("Soapy make(%s) failed: %s", a, e)
        raise err or RuntimeError("no device")

    def open(self):
        S = soapy()
        self.dev = self._make(S)
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
# RTL-SDR (Nooelec NESDR, RTL-SDR Blog, generic RTL2832U) straight through librtlsdr.
# This is how SDR++ / SDR# talk to the dongle and avoids SoapySDR's
# "Device::make() no match" problem with some RTL dongles on Windows.
# --------------------------------------------------------------------------------------
_rtl = None


def rtl_lib():
    global _rtl
    if _rtl is None:
        import ctypes
        import ctypes.util
        soapy()  # sets up the DLL search path to the bundled drivers
        names = ["rtlsdr.dll", "librtlsdr.dll"] if sys.platform.startswith("win") else \
            [ctypes.util.find_library("rtlsdr") or "librtlsdr.so.0", "librtlsdr.so"]
        _rtl = False
        for n in names:
            try:
                lib = ctypes.CDLL(n)
            except OSError:
                continue
            P, I, U = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
            lib.rtlsdr_get_device_count.restype = U
            lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
            lib.rtlsdr_get_device_name.argtypes = [U]
            lib.rtlsdr_get_device_usb_strings.argtypes = [U, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
            lib.rtlsdr_open.argtypes = [ctypes.POINTER(P), U]
            lib.rtlsdr_close.argtypes = [P]
            lib.rtlsdr_set_sample_rate.argtypes = [P, U]
            lib.rtlsdr_set_center_freq.argtypes = [P, U]
            lib.rtlsdr_set_freq_correction.argtypes = [P, I]
            lib.rtlsdr_set_tuner_gain_mode.argtypes = [P, I]
            lib.rtlsdr_set_tuner_gain.argtypes = [P, I]
            lib.rtlsdr_get_tuner_gains.argtypes = [P, ctypes.POINTER(I)]
            lib.rtlsdr_set_agc_mode.argtypes = [P, I]
            lib.rtlsdr_set_direct_sampling.argtypes = [P, I]
            lib.rtlsdr_get_tuner_type.argtypes = [P]
            lib.rtlsdr_reset_buffer.argtypes = [P]
            lib.rtlsdr_read_sync.argtypes = [P, ctypes.c_void_p, I, ctypes.POINTER(I)]
            if hasattr(lib, "rtlsdr_set_bias_tee"):
                lib.rtlsdr_set_bias_tee.argtypes = [P, I]
            _rtl = lib
            log.info("librtlsdr loaded: %s", n)
            break
        if not _rtl:
            log.warning("librtlsdr not found - RTL dongles will go through SoapySDR")
    return _rtl or None


def list_rtl_native():
    lib = rtl_lib()
    if not lib:
        return []
    import ctypes
    out = []
    for i in range(lib.rtlsdr_get_device_count()):
        m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
        lib.rtlsdr_get_device_usb_strings(i, m, p, s)
        man, prod, ser = (x.value.decode("latin-1", "replace").strip() for x in (m, p, s))
        name = " ".join(x for x in (man, prod) if x) or (lib.rtlsdr_get_device_name(i) or b"RTL-SDR").decode("latin-1")
        out.append(RtlNativeDevice(i, ser, f"{name} :: {ser}" if ser else name))
    return out


class RtlNativeDevice(BaseDevice):
    kind = "rtlsdr"
    RATES = [250000, 960000, 1200000, 1440000, 1920000, 2400000, 2880000]
    TUNERS = {1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T/R860", 6: "R828D"}

    def __init__(self, index, serial, label):
        super().__init__(f"rtlsdr:{serial or index}", label)
        self.index, self.serial = index, serial
        self.dev = None
        self.ppm = 0
        self.direct = 0
        self.bias = False
        self._gains = []
        self._buf = None

    def _find_index(self):
        # USB order can change between plug-ins - find our dongle again by serial
        lib = rtl_lib()
        import ctypes
        for i in range(lib.rtlsdr_get_device_count()):
            m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
            lib.rtlsdr_get_device_usb_strings(i, m, p, s)
            if self.serial and s.value.decode("latin-1", "replace").strip() == self.serial:
                return i
        return self.index

    def open(self):
        import ctypes
        lib = rtl_lib()
        if not lib:
            raise RuntimeError("RTL-SDR driver library (rtlsdr.dll) not found")
        h = ctypes.c_void_p()
        r = lib.rtlsdr_open(ctypes.byref(h), self._find_index())
        if r < 0 or not h.value:
            raise RuntimeError(
                f"rtlsdr_open failed ({r}). Check the dongle uses the WinUSB driver (Zadig: "
                "Options > List All Devices > 'Bulk-In, Interface (Interface 0)' > WinUSB) and that "
                "no other program (SDR#, SDR++, rtl_tcp) is using it")
        self.dev = h
        n = lib.rtlsdr_get_tuner_gains(h, None)
        if n > 0:
            arr = (ctypes.c_int * n)()
            lib.rtlsdr_get_tuner_gains(h, arr)
            self._gains = sorted(arr)
        lib.rtlsdr_set_sample_rate(h, int(self.sample_rate))
        if self.direct:
            lib.rtlsdr_set_direct_sampling(h, self.direct)
        lib.rtlsdr_set_center_freq(h, int(self.center))
        if self.ppm:
            lib.rtlsdr_set_freq_correction(h, int(self.ppm))
        if self.bias and hasattr(lib, "rtlsdr_set_bias_tee"):
            lib.rtlsdr_set_bias_tee(h, 1)
        self.set_agc(self.agc)
        lib.rtlsdr_reset_buffer(h)
        self._buf = (ctypes.c_uint8 * (1 << 18))()
        log.info("RTL-SDR open: %s, tuner %s, %d gain steps", self.label,
                 self.TUNERS.get(lib.rtlsdr_get_tuner_type(h), "?"), len(self._gains))

    def close(self):
        if self.dev:
            try:
                rtl_lib().rtlsdr_close(self.dev)
            except Exception:
                pass
        self.dev = None

    def read(self, n):
        import ctypes
        if not self.dev:
            return np.zeros(0, np.complex64)
        nbytes = min(2 * n, len(self._buf)) // 512 * 512 or 512
        got = ctypes.c_int(0)
        r = rtl_lib().rtlsdr_read_sync(self.dev, self._buf, nbytes, ctypes.byref(got))
        if r < 0 or got.value <= 0:
            time.sleep(0.01)
            return np.zeros(0, np.complex64)
        raw = np.frombuffer(self._buf, np.uint8, got.value & ~1).astype(np.float32)
        raw = (raw - 127.4) / 128.0
        return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)

    def set_sample_rate(self, sr):
        self.sample_rate = sr
        if self.dev:
            rtl_lib().rtlsdr_set_sample_rate(self.dev, int(sr))
            rtl_lib().rtlsdr_reset_buffer(self.dev)

    def set_center(self, f):
        self.center = f
        if self.dev:
            rtl_lib().rtlsdr_set_center_freq(self.dev, int(f))

    def set_gain(self, g):
        self.gain = g
        if self.dev and not self.agc:
            tenth = int(round(g * 10))
            if self._gains:
                tenth = min(self._gains, key=lambda x: abs(x - tenth))
            rtl_lib().rtlsdr_set_tuner_gain(self.dev, tenth)

    def set_agc(self, on):
        self.agc = bool(on)
        if self.dev:
            lib = rtl_lib()
            lib.rtlsdr_set_tuner_gain_mode(self.dev, 0 if self.agc else 1)
            lib.rtlsdr_set_agc_mode(self.dev, 1 if self.agc else 0)
            if not self.agc:
                self.set_gain(self.gain)

    def set_ppm(self, ppm):
        self.ppm = int(round(float(ppm or 0)))
        if self.dev:
            rtl_lib().rtlsdr_set_freq_correction(self.dev, self.ppm)  # returns -2 if unchanged: fine

    def antennas(self):
        return ["RX"]

    def gain_range(self):
        if self._gains:
            return (self._gains[0] / 10.0, self._gains[-1] / 10.0)
        return (0.0, 49.6)

    def sample_rates(self):
        return list(self.RATES)

    def freq_range(self):
        return (0.5e6 if self.direct else 24e6, 1766e6)

    def extra_settings(self):
        out = [{"key": "direct_samp", "name": "HF direct sampling", "type": 3,
                "value": {0: "Off", 1: "I branch", 2: "Q branch"}[self.direct],
                "options": ["Off", "I branch", "Q branch"],
                "description": "Receive 0.5-28.8 MHz on dongles wired for it (RTL-SDR Blog V3/V4: Q branch). "
                               "NESDR SMArt has no HF input - leave Off."}]
        if hasattr(rtl_lib() or object(), "rtlsdr_set_bias_tee"):
            out.append({"key": "biastee", "name": "Bias-T (antenna power)", "type": 0,
                        "value": "true" if self.bias else "false", "options": [],
                        "description": "Powers an LNA through the coax on dongles that support it."})
        return out

    def write_setting(self, key, value):
        lib = rtl_lib()
        if key == "direct_samp":
            self.direct = {"Off": 0, "I branch": 1, "Q branch": 2, "0": 0, "1": 1, "2": 2}.get(str(value), 0)
            if self.dev:
                lib.rtlsdr_set_direct_sampling(self.dev, self.direct)
                lib.rtlsdr_set_center_freq(self.dev, int(self.center))
        elif key == "biastee":
            self.bias = str(value).lower() in ("true", "1", "on")
            if self.dev and hasattr(lib, "rtlsdr_set_bias_tee"):
                lib.rtlsdr_set_bias_tee(self.dev, 1 if self.bias else 0)


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
        self.ref_fn = None         # set by the radio: returns the receiver (lat, lon) or None
        self._traffic = None       # simulated aircraft for ADS-B practice (1090 MHz)
        self._traffic_ref = None
        self._tr_t = 0.0
        self._pend = []

    def open(self):
        self.n = 0
        self._last = time.time()
        self._traffic, self._pend = None, []

    def _adsb_mix(self, x, i0, fs):
        """Add simulated ADS-B frames (10 aircraft) when 1090 MHz is inside the received band."""
        from .adsb import ADSB_FREQ
        from .adsb_sim import SimTraffic, modulate
        n = len(x)
        ref = self.ref_fn() if self.ref_fn else None
        if self._traffic is None or ref != self._traffic_ref:
            lat, lon = ref if ref else (51.50, -1.00)
            self._traffic, self._traffic_ref, self._pend = SimTraffic(lat, lon), ref, []
            self._tr_t = i0 / fs
        t1 = (i0 + n) / fs
        gain = 10 ** ((self.gain - 30) / 40)
        for tt, msg, amp in self._traffic.messages(self._tr_t, t1):
            self._pend.append((int(round(tt * fs)), modulate(msg, fs, amp * gain)))
        self._tr_t = t1
        off = ADSB_FREQ - self.center
        keep = []
        for s, env in self._pend:
            a, b = max(s, i0), min(s + len(env), i0 + n)
            if b > a:
                tt = np.arange(a, b) / fs
                x[a - i0:b - i0] += (env[a - s:b - s] * np.exp(2j * np.pi * off * tt)).astype(np.complex64)
            if s + len(env) > i0 + n:
                keep.append((s, env))
        self._pend = keep

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
        if abs(1090000000 - self.center) < fs * 0.45:
            self._adsb_mix(x, int(idx[0]), fs)
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
    # RTL-SDR dongles (Nooelec NESDR etc.): straight through librtlsdr (preferred)
    native_rtl = []
    try:
        native_rtl = list_rtl_native()
        out.extend(native_rtl)
    except Exception as e:  # noqa: BLE001
        log.warning("librtlsdr: %s", e)
    S = soapy()
    if S:
        try:
            for d in S.Device.enumerate():
                args = {k: d[k] for k in d.keys()}
                drv = args.get("driver", "?")
                if drv in ("audio",) or (native_rsp and "sdrplay" in drv) or (native_rtl and drv == "rtlsdr"):
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
