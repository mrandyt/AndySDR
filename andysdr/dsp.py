"""Signal processing: channel selection, filtering, demodulation, squelch, spectrum."""
import numpy as np
from scipy import signal

IF_RATE = 240000      # first decimation stage output
AUDIO_RATE = 48000    # every demodulator delivers 48 kHz
BLOCK_SECONDS = 0.05  # processing block length
SQ_HANG_BLOCKS = 5     # squelch hang time = 5 blocks = 0.25 s
NB_FACTOR = 6.0        # noise blanker: samples this many times above the median level are removed

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

# Selectable channel-filter widths (total Hz) per mode: (Hz, label).  The first entry marked default=True
# is what the mode uses when nothing is chosen (twice the half-bandwidth in MODES above).
BANDWIDTHS = {
    "NFM":   [(6000, "6 kHz – 6.25 kHz channels"), (8000, "8 kHz"), (10000, "10 kHz"), (12000, "12 kHz – standard narrow (default)"),
              (15000, "15 kHz"), (20000, "20 kHz")],
    "FM":    [(12500, "12.5 kHz"), (15000, "15 kHz"), (17000, "17 kHz – 25 kHz channels (default)"), (20000, "20 kHz"),
              (25000, "25 kHz"), (30000, "30 kHz")],
    "AM":    [(4000, "4 kHz – narrow"), (6000, "6 kHz"), (8000, "8 kHz – 8.33 kHz airband"), (9000, "9 kHz – broadcast (default)"),
              (12000, "12 kHz"), (15000, "15 kHz – 25 kHz airband"), (20000, "20 kHz")],
    "USB":   [(1800, "1.8 kHz – narrow"), (2400, "2.4 kHz – communications"), (2700, "2.7 kHz – standard SSB"),
              (3000, "3 kHz (default)"), (3600, "3.6 kHz – wide"), (4500, "4.5 kHz")],
    "LSB":   [(1800, "1.8 kHz – narrow"), (2400, "2.4 kHz – communications"), (2700, "2.7 kHz – standard SSB"),
              (3000, "3 kHz (default)"), (3600, "3.6 kHz – wide"), (4500, "4.5 kHz")],
    "CW":    [(100, "100 Hz – very narrow"), (200, "200 Hz"), (300, "300 Hz"), (500, "500 Hz (default)"), (800, "800 Hz"), (1200, "1.2 kHz")],
    "DIG":   [(8000, "8 kHz"), (9000, "9 kHz – M17"), (10000, "10 kHz"), (12500, "12.5 kHz (default)"), (15000, "15 kHz")],
    "DIG25": [(20000, "20 kHz"), (25000, "25 kHz (default)"), (30000, "30 kHz"), (36000, "36 kHz")],
    "WFM":   [(200000, "200 kHz – broadcast FM (fixed)")],
}
MAX_CHAN_BW = 44000          # the channel filter works at the 48 kHz audio rate


def default_bandwidth(mode):
    return int(MODES.get(mode, MODES["NFM"])[0] * 2)


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


class RationalDecimator(FirDecimator):
    """Polyphase resampler by up/down (e.g. 1 MS/s -> 240 kS/s = 6/25) with filter history carried between blocks."""

    def __init__(self, up, down, cutoff_hz, fs, ntaps=401):
        self.U, self.D = int(up), int(down)
        self.h = (signal.firwin(ntaps, cutoff_hz, fs=fs * self.U) * self.U).astype(np.float32)
        need = int(np.ceil((len(self.h) - 1) / self.U))
        self.H = int(np.ceil(need / self.D)) * self.D          # history, a multiple of `down` so the phase lines up
        self.hist = np.zeros(self.H, np.complex64)

    def process(self, x):
        buf = np.concatenate((self.hist, x))
        y = signal.upfirdn(self.h, buf, self.U, self.D)
        start = self.H * self.U // self.D
        out = y[start:start + len(x) * self.U // self.D]
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
        w = -2 * np.pi * freq / fs
        key = (len(x), w)
        if getattr(self, "_key", None) != key:            # the ramp only depends on tune offset and block size: reuse it
            self._ramp = np.exp(1j * (w * np.arange(len(x)))).astype(np.complex64)
            self._key = key
        osc = self._ramp * np.complex64(np.exp(1j * self.phase))
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


class _Chain:
    def __init__(self, *stages):
        self.stages = [s for s in stages if s is not None]

    def process(self, x):
        for s in self.stages:
            x = s.process(x)
        return x


class Receiver:
    """One channel receiver: tuned offset -> 48 kHz audio + side outputs for decoders."""

    def __init__(self, fs):
        self.fs = int(fs)
        self.mixer = Mixer()
        if self.fs % IF_RATE == 0:
            self.D1 = self.fs // IF_RATE
            self.stage1 = FirDecimator(self.D1, 100000, self.fs)
        else:
            # rates such as 10 MS/s are not a whole multiple of 240 kS/s: decimate to 1 MS/s, then resample 6/25
            from math import gcd
            pre = next(d for d in (10, 8, 5, 4, 2, 1) if self.fs % d == 0 and (self.fs // d) % 1000 == 0)
            mid = self.fs // pre
            g = gcd(IF_RATE, mid)
            self.D1 = pre
            first = FirDecimator(pre, 350000, self.fs, ntaps=41) if pre > 1 else None
            second = RationalDecimator(IF_RATE // g, mid // g, 100000, mid)
            self.stage1 = _Chain(first, second)
        self.stage2 = FirDecimator(IF_RATE // AUDIO_RATE, 20000, IF_RATE, ntaps=61)
        self.mode = None
        self.bw = 0                      # chosen channel width in Hz (0 = the mode's default)
        self.bw_lookup = lambda mode: 0  # engine hook: remembered width for a mode when it is selected
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
        self.am_car = None               # AM carrier level (tracks fades slowly) - makes AM loudness independent of signal strength
        self.nb_on = True                # impulse noise blanker (ignition, switching noise) for AM / SSB / CW
        self.nb_hits = 0
        self.hang = 0                    # squelch hang: keeps the audio open briefly so word endings are not chopped
        self.cw_mix = Mixer()
        self.ssb_mix = Mixer()

    # ------------------------------------------------------------------
    def set_mode(self, mode):
        if mode not in MODES:
            mode = "NFM"
        if mode == self.mode:
            return
        self.mode = mode
        try:
            self.bw = int(self.bw_lookup(mode) or 0)
        except Exception:
            self.bw = 0
        if mode == "WFM":
            self.chan = None
            self.wfm_dec = FirDecimator(IF_RATE // AUDIO_RATE, 15000, IF_RATE, ntaps=61, complex_=False)
            self._make_deemph()
        else:
            self._build_channel()

    def bandwidth(self):
        """Effective channel-filter width in Hz."""
        return int(self.bw) if self.bw else default_bandwidth(self.mode)

    def set_bandwidth(self, hz):
        """Choose the channel-filter width (total Hz) for the current mode; 0 = the mode's default."""
        hz = int(hz or 0)
        if self.mode == "WFM":
            return
        if hz:
            hz = max(50, min(MAX_CHAN_BW, hz))
        if hz == self.bw:
            return
        self.bw = hz
        self._build_channel()

    def _build_channel(self):
        mode = self.mode
        width = self.bandwidth()
        half = width / 2.0
        if mode in ("USB", "LSB"):
            # complex band-pass from 150 Hz up to 150 Hz + width (mirrored for LSB)
            n_taps = 129 if width >= 2000 else 257
            lp = signal.firwin(n_taps, half, fs=AUDIO_RATE)
            shift = (150 + half) * (1 if mode == "USB" else -1)
            n = np.arange(len(lp))
            taps = lp * np.exp(2j * np.pi * shift * n / AUDIO_RATE)
            self.chan = StatefulFIR(taps)
        else:
            n_taps = 129 if half > 1000 else (255 if half >= 200 else 1023)
            self.chan = StatefulFIR(signal.firwin(n_taps, half, fs=AUDIO_RATE))

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
        if self.nb_on and self.mode in ("AM", "USB", "LSB", "CW"):
            x = self._blank_impulses(x)
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
                audio = self._am_demod(c)
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
        if self.sq_open or self.squelch_db <= -119:
            self.hang = SQ_HANG_BLOCKS
        elif self.hang > 0:
            self.hang -= 1
        audio_open = self.sq_open or self.hang > 0
        out["audio"] = out["audio_ungated"] if audio_open else np.zeros_like(out["audio_ungated"])
        out["level_db"] = self.level_db
        out["squelch_open"] = self.sq_open
        return out

    def _am_demod(self, c):
        """Envelope AM detector referenced to the carrier: audio = envelope / carrier - 1, so a strong and a weak
        station sound equally loud, and fades are followed smoothly (no block-by-block pumping)."""
        env = np.abs(c).astype(np.float32)
        if not len(env):
            return env
        m = float(np.mean(env)) + 1e-9
        old = self.am_car if self.am_car else m
        alpha = 0.6 if m > old else 0.12                   # attack fast, release slowly
        new = old + (m - old) * alpha
        self.am_car = new
        car = np.linspace(old, new, len(env), dtype=np.float32)
        a = (env / car - 1.0) * 0.5
        return self.audio_hpf.process(np.clip(a, -1.5, 1.5))

    def _blank_impulses(self, x):
        """Impulse noise blanker: zero the few samples that stand far above the typical level."""
        if len(x) < 64:
            return x
        mag = np.abs(x)
        ref = float(np.median(mag)) + 1e-12
        hit = mag > ref * NB_FACTOR
        n = int(hit.sum())
        if n == 0 or n > len(x) * 0.15:                    # nothing to remove - or the signal itself is bursty: leave it alone
            return x
        hit = np.convolve(hit.astype(np.uint8), np.ones(5, np.uint8), "same") > 0      # widen a little around each spike
        self.nb_hits += n
        y = x.copy()
        y[hit] = 0
        return y

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
        new = min(self.agc_gain, 1e5)
        old = getattr(self, "_agc_prev", new)
        self._agc_prev = new
        self.agc_gain = new
        if len(a) and abs(new - old) > 1e-9 * max(new, old):
            return a * np.linspace(old, new, len(a), dtype=np.float32)        # ramp, so block-to-block gain steps do not click or pump
        return a * new


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
