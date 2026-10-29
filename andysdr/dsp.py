"""Signal processing: channel selection, filtering, demodulation, squelch, spectrum."""
import numpy as np
from scipy import signal

IF_RATE = 240000      # first decimation stage output
AUDIO_RATE = 48000    # every demodulator delivers 48 kHz
BLOCK_SECONDS = 0.05  # processing block length

PREFERRED_RATES = [2400000, 1920000, 2880000, 6000000, 4800000, 3840000, 7200000, 960000]

# mode -> (channel half-bandwidth Hz, description)
MODES = {
    "NFM":  (6000, "Narrow FM (12.5 kHz) – PMR, amateur, business radio, marine"),
    "FM":   (8500, "FM (25 kHz) – older wide-channel FM"),
    "WFM":  (100000, "Wide FM broadcast (stations 87.5–108 MHz)"),
    "AM":   (4500, "AM – airband 118–137 MHz, HF broadcast"),
    "USB":  (1500, "Upper sideband – HF voice above 10 MHz, WEFAX, digital HF"),
    "LSB":  (1500, "Lower sideband – amateur HF voice below 10 MHz"),
    "CW":   (250, "Morse code (700 Hz tone)"),
    "DIG":  (6250, "Digital voice/data (12.5 kHz) – DMR, P25, NXDN, dPMR, M17, POCSAG"),
    "DIG25": (12500, "Wide digital (25 kHz) – TETRA, wide P25"),
}


def pick_sample_rate(available):
    for r in PREFERRED_RATES:
        if r in available:
            return r
    return 2400000


class FirDecimator:
    """Stateful polyphase FIR decimator (block length must be a multiple of D)."""

    def __init__(self, decim, cutoff_hz, fs, ntaps=None, complex_=True):
        self.D = int(decim)
        if ntaps is None:
            ntaps = max(31, 8 * self.D + 1)
        self.h = signal.firwin(ntaps, cutoff_hz, fs=fs).astype(np.float32)
        H = int(np.ceil((len(self.h) - 1) / self.D)) * self.D
        self.H = H
        self.hist = np.zeros(H, np.complex64 if complex_ else np.float32)

    def process(self, x):
        buf = np.concatenate((self.hist, x))
        y = signal.upfirdn(self.h, buf, 1, self.D)
        start = self.H // self.D
        out = y[start:start + len(x) // self.D]
        self.hist = buf[-self.H:]
        return out


class StatefulFIR:
    def __init__(self, taps):
        self.b = np.asarray(taps)
        self.zi = np.zeros(len(self.b) - 1, np.complex128)

    def process(self, x):
        y, self.zi = signal.lfilter(self.b, 1.0, x, zi=self.zi)
        return y


class StatefulIIR:
    def __init__(self, b, a):
        self.b, self.a = b, a
        self.zi = np.zeros(max(len(a), len(b)) - 1)

    def process(self, x):
        y, self.zi = signal.lfilter(self.b, self.a, x, zi=self.zi)
        return y


class Mixer:
    """Continuous-phase frequency shifter."""

    def __init__(self):
        self.phase = 0.0

    def process(self, x, freq, fs):
        if freq == 0:
            return x
        n = np.arange(len(x))
        w = -2 * np.pi * freq / fs
        osc = np.exp(1j * (self.phase + w * n)).astype(np.complex64)
        self.phase = (self.phase + w * len(x)) % (2 * np.pi)
        return x * osc


class Spectrum:
    """Averaged FFT for the spectrum / waterfall display."""

    def __init__(self, size=2048):
        self.size = size
        self.win = signal.windows.blackmanharris(size).astype(np.float32)
        self.wsum = float(np.sum(self.win) ** 2)
        self.avg = None

    def process(self, x, alpha=0.5):
        n = self.size
        frames = len(x) // n
        if frames == 0:
            return self.avg
        frames = min(frames, 16)
        seg = x[: frames * n].reshape(frames, n) * self.win
        p = np.mean(np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2, axis=0) / self.wsum
        db = 10 * np.log10(p + 1e-20)
        if self.avg is None or len(self.avg) != n:
            self.avg = db
        else:
            self.avg = alpha * db + (1 - alpha) * self.avg
        return self.avg


class Receiver:
    """One channel receiver: tuned offset -> 48 kHz audio + side outputs for decoders."""

    def __init__(self, fs):
        self.fs = int(fs)
        self.D1 = max(1, int(round(self.fs / IF_RATE)))
        self.mixer = Mixer()
        self.stage1 = FirDecimator(self.D1, 100000, self.fs)
        self.stage2 = FirDecimator(IF_RATE // AUDIO_RATE, 20000, IF_RATE, ntaps=61)
        self.mode = None
        self.set_mode("NFM")
        self.deemph_us = 50
        self.last_iq = np.complex64(1)
        self.last_if = np.complex64(1)
        self.squelch_db = -60.0
        self.sq_open = False
        self.level_db = -120.0
        self.audio_hpf = StatefulIIR(*signal.butter(2, 250, "highpass", fs=AUDIO_RATE))
        self.audio_lpf = StatefulIIR(*signal.butter(4, 3400, "lowpass", fs=AUDIO_RATE))
        self.agc_gain = 1.0
        self.cw_mix = Mixer()
        self.ssb_mix = Mixer()

    # ------------------------------------------------------------------
    def set_mode(self, mode):
        if mode not in MODES:
            mode = "NFM"
        if mode == self.mode:
            return
        self.mode = mode
        half = MODES[mode][0]
        if mode in ("USB", "LSB"):
            # complex band-pass 150..3150 Hz (or mirrored for LSB)
            lp = signal.firwin(129, 1500, fs=AUDIO_RATE)
            shift = 1650 if mode == "USB" else -1650
            n = np.arange(len(lp))
            taps = lp * np.exp(2j * np.pi * shift * n / AUDIO_RATE)
            self.chan = StatefulFIR(taps)
        elif mode == "WFM":
            self.chan = None
            self.wfm_dec = FirDecimator(IF_RATE // AUDIO_RATE, 15000, IF_RATE, ntaps=61, complex_=False)
            self._make_deemph()
        else:
            self.chan = StatefulFIR(signal.firwin(129 if half > 1000 else 255, half, fs=AUDIO_RATE))

    def _make_deemph(self):
        tau = getattr(self, "deemph_us", 50) * 1e-6
        # single pole de-emphasis at 48 kHz
        a = np.exp(-1.0 / (AUDIO_RATE * tau))
        self.deemph = StatefulIIR(np.array([1 - a]), np.array([1, -a]))

    # ------------------------------------------------------------------
    def _discriminator(self, x, last_attr):
        last = getattr(self, last_attr)
        prev = np.concatenate(([last], x[:-1]))
        setattr(self, last_attr, x[-1] if len(x) else last)
        return np.angle(x * np.conj(prev)).astype(np.float32)

    def process(self, iq, offset_hz):
        """Returns dict(audio, disc, iq48, level_db, squelch_open)."""
        x = self.mixer.process(iq, offset_hz, self.fs)
        x = self.stage1.process(x)                     # 240 kS/s
        out = {"iq240": x}
        if self.mode == "WFM":
            p = float(np.mean(np.abs(x) ** 2)) if len(x) else 1e-12
            d = self._discriminator(x, "last_if")       # radians/sample at 240k
            a = self.wfm_dec.process(d)                 # 48 kHz
            a = self.deemph.process(a) * 0.6
            out["disc"] = a.astype(np.float32)
            out["iq48"] = None
            audio = a
        else:
            y = self.stage2.process(x)                  # 48 kS/s complex
            out["iq48"] = y.astype(np.complex64)
            c = self.chan.process(y)
            p = float(np.mean(np.abs(c) ** 2)) if len(c) else 1e-12
            m = self.mode
            if m in ("NFM", "FM", "DIG", "DIG25"):
                d = self._discriminator(c.astype(np.complex64), "last_iq")
                out["disc"] = d
                scale = 1.0 / (2 * np.pi * (2500 if m != "FM" else 5000) / AUDIO_RATE) * 0.35
                audio = self.audio_lpf.process(self.audio_hpf.process(d * scale))
            elif m == "AM":
                env = np.abs(c)
                audio = self.audio_hpf.process(env)
                audio = self._agc(audio)
                out["disc"] = audio
            elif m in ("USB", "LSB"):
                audio = self._agc(np.real(c))
                out["disc"] = audio
            elif m == "CW":
                audio = self._agc(np.real(self.cw_mix.process(c, -700, AUDIO_RATE)))
                out["disc"] = audio
            else:
                audio = np.zeros(len(c), np.float32)
                out["disc"] = audio
        self.level_db = 10 * np.log10(p + 1e-20)
        # squelch with 3 dB hysteresis
        if self.squelch_db <= -119:
            self.sq_open = True
        elif self.sq_open:
            self.sq_open = self.level_db > self.squelch_db - 3
        else:
            self.sq_open = self.level_db > self.squelch_db
        out["audio_ungated"] = np.clip(audio, -1, 1).astype(np.float32)
        out["audio"] = out["audio_ungated"] if self.sq_open else np.zeros_like(out["audio_ungated"])
        out["level_db"] = self.level_db
        out["squelch_open"] = self.sq_open
        return out

    def _agc(self, a):
        peak = float(np.max(np.abs(a))) if len(a) else 0.0
        target = 0.3
        if peak > 1e-9:
            want = target / peak
            # fast attack, slow release
            if want < self.agc_gain:
                self.agc_gain = want
            else:
                self.agc_gain = self.agc_gain * 0.9 + want * 0.1
        self.agc_gain = min(self.agc_gain, 1e5)
        return a * self.agc_gain


def find_peaks_db(spec_db, fs, center, threshold_db, min_sep_hz=10000):
    """Detect live carriers in a spectrum (dBFS bins). Returns list of (freq_hz, level_db)."""
    if spec_db is None:
        return []
    n = len(spec_db)
    floor = float(np.median(spec_db))
    bin_hz = fs / n
    # ignore the outer 8% (filter roll-off) and DC spike
    lo, hi = int(n * 0.08), int(n * 0.92)
    s = spec_db.copy()
    s[:lo] = floor
    s[hi:] = floor
    dc = n // 2
    s[dc - 2: dc + 3] = floor
    dist = max(1, int(min_sep_hz / bin_hz))
    idx, _ = signal.find_peaks(s, height=floor + threshold_db, distance=dist)
    return [(center + (i - n / 2) * bin_hz, float(s[i])) for i in idx]
