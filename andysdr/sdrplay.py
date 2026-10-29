"""Native SDRplay support through SDRplay's own API (sdrplay_api.dll, version 3.x).

Covers RSP1, RSP1A, RSP1B, RSP2 / RSP2pro, RSPduo (single tuner), RSPdx and RSPdx-R2.
Install "SDRplay API 3.x for Windows" from sdrplay.com once; AndySDR then talks to the
RSP directly – no SoapySDR module needed.

Structure layouts follow sdrplay_api*.h from API 3.15.
"""
import ctypes as C
import logging
import os
import queue
import sys
import threading

import numpy as np

from .devices import BaseDevice

log = logging.getLogger("andysdr.sdrplay")

# ------------------------------------------------------------------ structures (API 3.15)
c_enum = C.c_int
uchar = C.c_ubyte


class GainValuesT(C.Structure):
    _fields_ = [("curr", C.c_float), ("max", C.c_float), ("min", C.c_float)]


class GainT(C.Structure):
    _fields_ = [("gRdB", C.c_int), ("LNAstate", uchar), ("syncUpdate", uchar),
                ("minGr", c_enum), ("gainVals", GainValuesT)]


class RfFreqT(C.Structure):
    _fields_ = [("rfHz", C.c_double), ("syncUpdate", uchar)]


class DcOffsetTunerT(C.Structure):
    _fields_ = [("dcCal", uchar), ("speedUp", uchar), ("trackTime", C.c_int), ("refreshRateTime", C.c_int)]


class TunerParamsT(C.Structure):
    _fields_ = [("bwType", c_enum), ("ifType", c_enum), ("loMode", c_enum), ("gain", GainT),
                ("rfFreq", RfFreqT), ("dcOffsetTuner", DcOffsetTunerT)]


class DcOffsetT(C.Structure):
    _fields_ = [("DCenable", uchar), ("IQenable", uchar)]


class DecimationT(C.Structure):
    _fields_ = [("enable", uchar), ("decimationFactor", uchar), ("wideBandSignal", uchar)]


class AgcT(C.Structure):
    _fields_ = [("enable", c_enum), ("setPoint_dBfs", C.c_int), ("attack_ms", C.c_ushort),
                ("decay_ms", C.c_ushort), ("decay_delay_ms", C.c_ushort),
                ("decay_threshold_dB", C.c_ushort), ("syncUpdate", C.c_int)]


class ControlParamsT(C.Structure):
    _fields_ = [("dcOffset", DcOffsetT), ("decimation", DecimationT), ("agc", AgcT), ("adsbMode", c_enum)]


class Rsp1aTunerParamsT(C.Structure):
    _fields_ = [("biasTEnable", uchar)]


class Rsp2TunerParamsT(C.Structure):
    _fields_ = [("biasTEnable", uchar), ("amPortSel", c_enum), ("antennaSel", c_enum), ("rfNotchEnable", uchar)]


class RspDuoResetSlaveFlagsT(C.Structure):
    _fields_ = [("resetGainUpdate", uchar), ("resetRfUpdate", uchar)]


class RspDuoTunerParamsT(C.Structure):
    _fields_ = [("biasTEnable", uchar), ("tuner1AmPortSel", c_enum), ("tuner1AmNotchEnable", uchar),
                ("rfNotchEnable", uchar), ("rfDabNotchEnable", uchar), ("resetSlaveFlags", RspDuoResetSlaveFlagsT)]


class RspDxTunerParamsT(C.Structure):
    _fields_ = [("hdrBw", c_enum)]


class RxChannelParamsT(C.Structure):
    _fields_ = [("tunerParams", TunerParamsT), ("ctrlParams", ControlParamsT),
                ("rsp1aTunerParams", Rsp1aTunerParamsT), ("rsp2TunerParams", Rsp2TunerParamsT),
                ("rspDuoTunerParams", RspDuoTunerParamsT), ("rspDxTunerParams", RspDxTunerParamsT)]


class FsFreqT(C.Structure):
    _fields_ = [("fsHz", C.c_double), ("syncUpdate", uchar), ("reCal", uchar)]


class SyncUpdateT(C.Structure):
    _fields_ = [("sampleNum", C.c_uint), ("period", C.c_uint)]


class ResetFlagsT(C.Structure):
    _fields_ = [("resetGainUpdate", uchar), ("resetRfUpdate", uchar), ("resetFsUpdate", uchar)]


class Rsp1aParamsT(C.Structure):
    _fields_ = [("rfNotchEnable", uchar), ("rfDabNotchEnable", uchar)]


class Rsp2ParamsT(C.Structure):
    _fields_ = [("extRefOutputEn", uchar)]


class RspDuoParamsT(C.Structure):
    _fields_ = [("extRefOutputEn", C.c_int)]


class RspDxParamsT(C.Structure):
    _fields_ = [("hdrEnable", uchar), ("biasTEnable", uchar), ("antennaSel", c_enum),
                ("rfNotchEnable", uchar), ("rfDabNotchEnable", uchar)]


class DevParamsT(C.Structure):
    _fields_ = [("ppm", C.c_double), ("fsFreq", FsFreqT), ("syncUpdate", SyncUpdateT),
                ("resetFlags", ResetFlagsT), ("mode", c_enum), ("samplesPerPkt", C.c_uint),
                ("rsp1aParams", Rsp1aParamsT), ("rsp2Params", Rsp2ParamsT),
                ("rspDuoParams", RspDuoParamsT), ("rspDxParams", RspDxParamsT)]


class DeviceParamsT(C.Structure):
    _fields_ = [("devParams", C.POINTER(DevParamsT)), ("rxChannelA", C.POINTER(RxChannelParamsT)),
                ("rxChannelB", C.POINTER(RxChannelParamsT))]


class DeviceT(C.Structure):
    _fields_ = [("SerNo", C.c_char * 64), ("hwVer", uchar), ("tuner", c_enum), ("rspDuoMode", c_enum),
                ("valid", uchar), ("rspDuoSampleFreq", C.c_double), ("dev", C.c_void_p)]


class StreamCbParamsT(C.Structure):
    _fields_ = [("firstSampleNum", C.c_uint), ("grChanged", C.c_int), ("rfChanged", C.c_int),
                ("fsChanged", C.c_int), ("numSamples", C.c_uint)]


StreamCb = C.CFUNCTYPE(None, C.POINTER(C.c_short), C.POINTER(C.c_short), C.POINTER(StreamCbParamsT),
                       C.c_uint, C.c_uint, C.c_void_p)
EventCb = C.CFUNCTYPE(None, c_enum, c_enum, C.c_void_p, C.c_void_p)


class CallbackFnsT(C.Structure):
    _fields_ = [("StreamACbFn", StreamCb), ("StreamBCbFn", StreamCb), ("EventCbFn", EventCb)]


# ------------------------------------------------------------------ constants
HW = {1: "RSP1", 255: "RSP1A", 2: "RSP2", 3: "RSPduo", 4: "RSPdx", 6: "RSP1B", 7: "RSPdx-R2"}
LNA_STATES = {1: 4, 255: 10, 6: 10, 2: 9, 3: 10, 4: 28, 7: 28}
TUNER_A = 1
AGC_DISABLE, AGC_50HZ = 0, 2
IF_ZERO = 0
DUO_SINGLE = 1

UPD_DEV_FS = 0x1
UPD_DEV_PPM = 0x2
UPD_RSP1A_BIAST = 0x10
UPD_RSP1A_NOTCH = 0x20
UPD_RSP1A_DAB = 0x40
UPD_RSP2_BIAST = 0x80
UPD_RSP2_AMPORT = 0x100
UPD_RSP2_ANT = 0x200
UPD_RSP2_NOTCH = 0x400
UPD_GR = 0x8000
UPD_FRF = 0x20000
UPD_BW = 0x40000
UPD_AGC = 0x01000000
UPD_OVERLOAD_ACK = 0x04000000
UPD_DUO_BIAST = 0x08000000
UPD_DUO_NOTCH = 0x40000000
UPD_DUO_DAB = 0x80000000
EXT_DX_BIAST = 0x2
EXT_DX_ANT = 0x4
EXT_DX_NOTCH = 0x8
EXT_DX_DAB = 0x10

RATES = [2400000, 4800000, 6000000, 7200000, 9600000]


def _bw_for(fs):
    if fs <= 2.5e6:
        return 1536
    if fs <= 6.1e6:
        return 5000
    if fs <= 7.3e6:
        return 6000
    return 8000


# ------------------------------------------------------------------ library
_lib = None
_lib_err = ""
_open_count = 0
_lock = threading.Lock()


def _load():
    global _lib, _lib_err
    if _lib is not None:
        return _lib or None
    cands = [os.environ["ANDYSDR_SDRPLAY_LIB"]] if os.environ.get("ANDYSDR_SDRPLAY_LIB") else []
    if sys.platform.startswith("win"):
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        arch = "x64" if C.sizeof(C.c_void_p) == 8 else "x86"
        cands += [os.path.join(pf, "SDRplay", "API", arch, "sdrplay_api.dll"), "sdrplay_api.dll"]
    elif sys.platform == "darwin":
        cands += ["/usr/local/lib/libsdrplay_api.so.3", "libsdrplay_api.so.3"]
    else:
        cands += ["libsdrplay_api.so.3", "/usr/local/lib/libsdrplay_api.so.3"]
    for p in cands:
        try:
            lib = C.CDLL(p)
            break
        except OSError as e:
            _lib_err = str(e)
    else:
        _lib = False
        return None
    E = C.c_int
    sig = {
        "sdrplay_api_Open": ([], E), "sdrplay_api_Close": ([], E),
        "sdrplay_api_ApiVersion": ([C.POINTER(C.c_float)], E),
        "sdrplay_api_LockDeviceApi": ([], E), "sdrplay_api_UnlockDeviceApi": ([], E),
        "sdrplay_api_GetDevices": ([C.POINTER(DeviceT), C.POINTER(C.c_uint), C.c_uint], E),
        "sdrplay_api_SelectDevice": ([C.POINTER(DeviceT)], E),
        "sdrplay_api_ReleaseDevice": ([C.POINTER(DeviceT)], E),
        "sdrplay_api_GetErrorString": ([E], C.c_char_p),
        "sdrplay_api_GetDeviceParams": ([C.c_void_p, C.POINTER(C.POINTER(DeviceParamsT))], E),
        "sdrplay_api_Init": ([C.c_void_p, C.POINTER(CallbackFnsT), C.c_void_p], E),
        "sdrplay_api_Uninit": ([C.c_void_p], E),
        "sdrplay_api_Update": ([C.c_void_p, c_enum, C.c_uint, C.c_uint], E),
    }
    for name, (args, res) in sig.items():
        f = getattr(lib, name)
        f.argtypes, f.restype = args, res
    _lib = lib
    return lib


def _err(code):
    try:
        return _lib.sdrplay_api_GetErrorString(code).decode()
    except Exception:
        return f"error {code}"


def _check(code, what):
    if code != 0:
        raise RuntimeError(f"SDRplay {what}: {_err(code)}")


def _api_open():
    global _open_count
    lib = _load()
    if not lib:
        raise RuntimeError("SDRplay API not installed")
    with _lock:
        if _open_count == 0:
            _check(lib.sdrplay_api_Open(), "Open (is the SDRplay API service running?)")
            ver = C.c_float(0)
            lib.sdrplay_api_ApiVersion(C.byref(ver))
            if ver.value < 3.07:
                log.warning("SDRplay API %.2f is old – please install 3.14 or newer", ver.value)
        _open_count += 1


def _api_close():
    global _open_count
    with _lock:
        _open_count = max(0, _open_count - 1)
        if _open_count == 0 and _lib:
            _lib.sdrplay_api_Close()


def api_status():
    lib = _load()
    if not lib:
        return {"available": False, "error": _lib_err or "sdrplay_api not found"}
    try:
        _api_open()
        ver = C.c_float(0)
        lib.sdrplay_api_ApiVersion(C.byref(ver))
        _api_close()
        return {"available": True, "version": round(ver.value, 2)}
    except Exception as e:
        return {"available": False, "error": str(e)}


def enumerate_rsp():
    """List connected RSPs as dicts {serial, hw, name}."""
    lib = _load()
    if not lib:
        return []
    out = []
    try:
        _api_open()
        devs = (DeviceT * 16)()
        n = C.c_uint(0)
        lib.sdrplay_api_LockDeviceApi()
        try:
            _check(lib.sdrplay_api_GetDevices(devs, C.byref(n), 16), "GetDevices")
        finally:
            lib.sdrplay_api_UnlockDeviceApi()
        for i in range(n.value):
            d = devs[i]
            out.append({"serial": d.SerNo.decode(errors="ignore"), "hw": d.hwVer,
                        "name": HW.get(d.hwVer, f"RSP(hw {d.hwVer})")})
    except Exception as e:
        log.warning("SDRplay enumerate: %s", e)
    finally:
        try:
            _api_close()
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ device
class SdrplayDevice(BaseDevice):
    kind = "sdrplay"

    def __init__(self, serial, hw, name):
        super().__init__(f"sdrplay:{serial}", f"SDRplay {name} ({serial})")
        self.serial, self.hw, self.name = serial, hw, name
        self.devT = None
        self.params = None
        self.q = queue.Queue(maxsize=400)
        self.pending = np.zeros(0, np.complex64)
        self.antenna = ""
        self.flags = {"bias_t": False, "fm_notch": False, "dab_notch": False}
        self._cbs = None
        self.overloads = 0
        self.ppm = 0.0

    # ----- streaming callbacks (run on the API's thread)
    def _stream(self, xi, xq, params, num, reset, ctx):
        try:
            i = np.ctypeslib.as_array(xi, shape=(num,)).astype(np.float32)
            q = np.ctypeslib.as_array(xq, shape=(num,)).astype(np.float32)
            self.q.put_nowait(((i + 1j * q) * (1.0 / 32768.0)).astype(np.complex64))
        except queue.Full:
            pass
        except Exception:
            pass

    def _event(self, event_id, tuner, params, ctx):
        if event_id == 1 and self.devT is not None:   # power overload change – must acknowledge
            self.overloads += 1
            try:
                _lib.sdrplay_api_Update(self.devT.dev, TUNER_A, UPD_OVERLOAD_ACK, 0)
            except Exception:
                pass

    # ----- lifecycle
    def open(self):
        lib = _load()
        _api_open()
        devs = (DeviceT * 16)()
        n = C.c_uint(0)
        lib.sdrplay_api_LockDeviceApi()
        try:
            _check(lib.sdrplay_api_GetDevices(devs, C.byref(n), 16), "GetDevices")
            sel = None
            for i in range(n.value):
                if devs[i].SerNo.decode(errors="ignore") == self.serial:
                    sel = devs[i]
            if sel is None:
                raise RuntimeError(f"RSP {self.serial} not found – is it plugged in / in use by another program?")
            if sel.hwVer == 3:            # RSPduo: use tuner A in single-tuner mode
                sel.tuner = TUNER_A
                sel.rspDuoMode = DUO_SINGLE
            self.devT = DeviceT.from_buffer_copy(sel)
            _check(lib.sdrplay_api_SelectDevice(C.byref(self.devT)), "SelectDevice")
        finally:
            lib.sdrplay_api_UnlockDeviceApi()
        p = C.POINTER(DeviceParamsT)()
        _check(lib.sdrplay_api_GetDeviceParams(self.devT.dev, C.byref(p)), "GetDeviceParams")
        self.params = p.contents
        dev = self.params.devParams
        rx = self.params.rxChannelA.contents
        if dev:
            dev.contents.fsFreq.fsHz = float(self.sample_rate)
            dev.contents.ppm = float(self.ppm)
        t = rx.tunerParams
        t.bwType = _bw_for(self.sample_rate)
        t.ifType = IF_ZERO
        t.rfFreq.rfHz = float(self.center)
        rx.ctrlParams.decimation.enable = 0
        rx.ctrlParams.agc.enable = AGC_50HZ if self.agc else AGC_DISABLE
        rx.ctrlParams.agc.setPoint_dBfs = -30
        self._apply_gain_values(rx)
        self._apply_antenna_values()
        saved = dict(self.flags)
        self._cbs = CallbackFnsT(StreamCb(self._stream), StreamCb(lambda *a: None), EventCb(self._event))
        self.pending = np.zeros(0, np.complex64)
        _check(lib.sdrplay_api_Init(self.devT.dev, C.byref(self._cbs), None), "Init")
        for k, v in saved.items():          # re-apply bias-T / notch switches after a restart
            if v:
                self.write_setting(k, "true")

    def close(self):
        lib = _load()
        if self.devT is not None and lib:
            try:
                lib.sdrplay_api_Uninit(self.devT.dev)
            except Exception:
                pass
            try:
                lib.sdrplay_api_ReleaseDevice(C.byref(self.devT))
            except Exception:
                pass
            _api_close()
        self.devT = None
        self.params = None

    def read(self, n):
        out = [self.pending] if len(self.pending) else []
        have = len(self.pending)
        while have < n:
            try:
                b = self.q.get(timeout=1.0)
            except queue.Empty:
                break
            out.append(b)
            have += len(b)
        if not out:
            return np.zeros(0, np.complex64)
        x = np.concatenate(out)
        self.pending = x[n:]
        return x[:n]

    # ----- controls
    def _update(self, reason, ext=0):
        if self.devT is None:
            return
        code = _lib.sdrplay_api_Update(self.devT.dev, TUNER_A, reason, ext)
        if code != 0:
            log.warning("SDRplay update 0x%x/0x%x: %s", reason, ext, _err(code))

    def set_center(self, f):
        self.center = f
        if self.params:
            self.params.rxChannelA.contents.tunerParams.rfFreq.rfHz = float(f)
            self._update(UPD_FRF)

    def set_sample_rate(self, sr):
        # sample rate changes need a full restart of the stream
        self.sample_rate = int(sr)
        if self.devT is not None:
            self.close()
            self.open()

    def gain_range(self):
        return (0.0, 100.0)

    def _apply_gain_values(self, rx):
        p = min(1.0, max(0.0, (self.gain or 0) / 100.0))
        lna_max = LNA_STATES.get(self.hw, 9) - 1
        # top half of the knob trims IF gain, bottom half adds LNA attenuation
        rx.tunerParams.gain.gRdB = int(round(20 + (1 - p) * 39))
        rx.tunerParams.gain.LNAstate = int(round((1 - p) ** 1.5 * lna_max))

    def set_gain(self, g):
        self.gain = float(g)
        if self.params and not self.agc:
            self._apply_gain_values(self.params.rxChannelA.contents)
            self._update(UPD_GR)

    def set_agc(self, on):
        self.agc = bool(on)
        if self.params:
            self.params.rxChannelA.contents.ctrlParams.agc.enable = AGC_50HZ if on else AGC_DISABLE
            self._update(UPD_AGC)
            if not on:
                self.set_gain(self.gain)

    def set_ppm(self, ppm):
        self.ppm = float(ppm)
        if self.params and self.params.devParams:
            self.params.devParams.contents.ppm = float(ppm)
            self._update(UPD_DEV_PPM)

    def antennas(self):
        if self.hw == 2:
            return ["Antenna A", "Antenna B", "Hi-Z"]
        if self.hw in (4, 7):
            return ["Antenna A", "Antenna B", "Antenna C"]
        if self.hw == 3:
            return ["Tuner 1 50 ohm", "Tuner 1 Hi-Z"]
        return ["Antenna"]

    def _apply_antenna_values(self):
        if not self.params or not self.antenna:
            return 0, 0
        rx = self.params.rxChannelA.contents
        a = self.antenna
        if self.hw == 2:
            rx.rsp2TunerParams.amPortSel = 1 if a == "Hi-Z" else 0
            rx.rsp2TunerParams.antennaSel = 6 if a == "Antenna B" else 5
            return UPD_RSP2_ANT | UPD_RSP2_AMPORT, 0
        if self.hw in (4, 7) and self.params.devParams:
            self.params.devParams.contents.rspDxParams.antennaSel = {"Antenna A": 0, "Antenna B": 1, "Antenna C": 2}.get(a, 0)
            return 0, EXT_DX_ANT
        if self.hw == 3:
            rx.rspDuoTunerParams.tuner1AmPortSel = 1 if "Hi-Z" in a else 0
            return 0x10000000, 0
        return 0, 0

    def set_antenna(self, name):
        self.antenna = name
        reason, ext = self._apply_antenna_values()
        if reason or ext:
            self._update(reason, ext)

    def sample_rates(self):
        return RATES

    def freq_range(self):
        return (1e3, 2e9)

    def extra_settings(self):
        s = [{"key": "bias_t", "name": "Bias-T (antenna power)", "type": 0,
              "value": str(self.flags["bias_t"]).lower(), "options": [], "description": "Powers an active antenna or LNA"}]
        if self.hw != 1:
            s.append({"key": "fm_notch", "name": "FM broadcast notch", "type": 0,
                      "value": str(self.flags["fm_notch"]).lower(), "options": [],
                      "description": "Blocks strong 88-108 MHz stations that can overload the receiver"})
        if self.hw in (255, 6, 3, 4, 7):
            s.append({"key": "dab_notch", "name": "DAB notch", "type": 0,
                      "value": str(self.flags["dab_notch"]).lower(), "options": [],
                      "description": "Blocks strong DAB (174-240 MHz) signals"})
        s.append({"key": "overloads", "name": f"ADC overload events: {self.overloads}", "type": 3,
                  "value": "", "options": [], "description": "If this climbs quickly, reduce RF GAIN"})
        return s

    def write_setting(self, key, value):
        on = str(value).lower() in ("1", "true", "on", "yes")
        if key not in self.flags or not self.params:
            return
        self.flags[key] = on
        rx = self.params.rxChannelA.contents
        dev = self.params.devParams.contents if self.params.devParams else None
        hw = self.hw
        if key == "bias_t":
            if hw in (255, 6):
                rx.rsp1aTunerParams.biasTEnable = on
                self._update(UPD_RSP1A_BIAST)
            elif hw == 2:
                rx.rsp2TunerParams.biasTEnable = on
                self._update(UPD_RSP2_BIAST)
            elif hw == 3:
                rx.rspDuoTunerParams.biasTEnable = on
                self._update(UPD_DUO_BIAST)
            elif hw in (4, 7) and dev:
                dev.rspDxParams.biasTEnable = on
                self._update(0, EXT_DX_BIAST)
        elif key == "fm_notch":
            if hw in (255, 6) and dev:
                dev.rsp1aParams.rfNotchEnable = on
                self._update(UPD_RSP1A_NOTCH)
            elif hw == 2:
                rx.rsp2TunerParams.rfNotchEnable = on
                self._update(UPD_RSP2_NOTCH)
            elif hw == 3:
                rx.rspDuoTunerParams.rfNotchEnable = on
                self._update(UPD_DUO_NOTCH)
            elif hw in (4, 7) and dev:
                dev.rspDxParams.rfNotchEnable = on
                self._update(0, EXT_DX_NOTCH)
        elif key == "dab_notch":
            if hw in (255, 6) and dev:
                dev.rsp1aParams.rfDabNotchEnable = on
                self._update(UPD_RSP1A_DAB)
            elif hw == 3:
                rx.rspDuoTunerParams.rfDabNotchEnable = on
                self._update(UPD_DUO_DAB)
            elif hw in (4, 7) and dev:
                dev.rspDxParams.rfDabNotchEnable = on
                self._update(0, EXT_DX_DAB)


def list_sdrplay():
    return [SdrplayDevice(d["serial"], d["hw"], d["name"]) for d in enumerate_rsp()]
