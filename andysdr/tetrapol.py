"""TETRAPOL demodulator: 48 kS/s complex baseband -> hard bits for the Tetrapol-kit frame decoder (tetrapol_dump).

TETRAPOL (the French-designed PMR system used by the police and gendarmerie of France, and by other forces in
Spain, Switzerland, the Czech Republic, Slovakia, Hungary, Romania and more - not in the UK) is GMSK, 8000 bit/s
in 12.5 kHz channels, in 160 bit (20 ms) frames.  The radio part is done here: channel filter, FM discriminator,
frequency-offset removal, symbol timing (Gardner) and the bit decision.  `tetrapol_dump -i -` does everything
after that: frame sync, differential decoding, de-interleaving, error correction and the message layers.

The first 8 bits of every frame are a fixed synchronisation pattern.  This module looks for it (in both
polarities), which gives us three things: the correct signal polarity (a mirrored spectrum is common), a
"locked" indication, and a quick way for the automatic identifier to recognise a Tetrapol channel.
"""
import math

import numpy as np
from scipy import signal

SYMBOL_RATE = 8000.0
FRAME_BITS = 160
DSYNC = np.array([1, 0, 1, 0, 0, 1, 1], np.uint8)      # bits 1..7 of every frame, before differential decoding


# ------------------------------------------------------------------------------------------------ frame sync
def sync_errors(bits):
    """errors[p] = how many of the 7 sync bits at p+1..p+7 differ from the pattern (p = candidate frame start)."""
    n = len(bits) - 8 + 1
    if n <= 0:
        return np.zeros(0, np.int32)
    e = np.zeros(n, np.int32)
    for i in range(7):
        e += bits[1 + i:1 + i + n] != DSYNC[i]
    return e


def find_sync(bits, min_frames=5, max_err=1):
    """Look for the frame sync pattern repeating every 160 bits.  Returns (offset, frames_found, frames_possible)
    or None.  The pattern must be found, with at most `max_err` wrong bits, in most of the frames."""
    bits = np.asarray(bits, np.uint8)
    e = sync_errors(bits)
    nfr = (len(e) - 1) // FRAME_BITS
    if nfr < min_frames:
        return None
    use = e[:nfr * FRAME_BITS].reshape(nfr, FRAME_BITS)         # [frame, offset]
    good = (use <= max_err).sum(0)                              # per offset: frames showing the pattern
    o = int(np.argmax(good))
    if good[o] >= max(min_frames, int(0.6 * nfr)):
        return o, int(good[o]), nfr
    return None


# ------------------------------------------------------------------------------------------------ demodulator
class TetrapolDemod:
    def __init__(self, fs=48000.0):
        self.fs = float(fs)
        self.sps = self.fs / SYMBOL_RATE
        self.lp = signal.firwin(129, 6500, fs=self.fs)
        self.zi = np.zeros(len(self.lp) - 1, np.complex128)
        self.sm = np.array([0.25, 0.5, 0.25])
        self.zs = np.zeros(2)
        self.prev = 0j
        self.dc = 0.0
        self.dc_init = False
        self.buf = np.zeros(0)
        self.base = 0
        self.tn = None
        self.omega_adj = 0.0
        self.locked_once = False
        self.hist = np.zeros(0, np.uint8)       # raw bits, polarity as demodulated
        self.since = 0
        self.invert = False                     # True = spectrum mirrored: bits are flipped before output
        self.synced = False
        self.sync_frames = 0
        self.n_bits = 0
        self.freq = 0.0                         # frequency error being removed (Hz)

    # ---- symbol timing: each block, find where the eye is most open and steer the sampling grid there
    def _bits(self, d):
        self.buf = np.concatenate((self.buf, d))
        buf = self.buf
        sps = self.sps + self.omega_adj
        tn = self.tn if self.tn is not None else self.base + 2 * self.sps
        avail = (len(buf) - 2) - (tn - self.base)
        n = int(avail // sps)
        if n < 8:
            self.tn = tn
            return np.zeros(0, np.uint8)
        idx = np.arange(len(buf), dtype=float)
        grid = tn - self.base + sps * np.arange(n)
        # eye opening at 12 trial offsets across one symbol: the right instant has the largest mean |level|
        trial = np.arange(-6, 6) * (self.sps / 12.0)
        m = np.array([np.mean(np.abs(np.interp(np.clip(grid + t, 0, len(buf) - 1), idx, buf))) for t in trial])
        k = int(np.argmax(m))
        dl = trial[k]
        if 0 < k < len(m) - 1:                                 # parabolic refinement
            den = m[k - 1] - 2 * m[k] + m[k + 1]
            if den < 0:
                dl += 0.5 * (m[k - 1] - m[k + 1]) / den * (self.sps / 12.0)
        first = not self.locked_once
        step = 1.0 if first else 0.5
        y = np.interp(np.clip(grid + step * dl, 0, len(buf) - 1), idx, buf)
        self.locked_once = True
        self.omega_adj = float(np.clip(self.omega_adj + 0.01 * dl / max(n, 1), -0.05 * self.sps, 0.05 * self.sps))
        self.tn = tn + n * sps + 0.4 * dl
        cut = int(self.tn - self.base - 3 * self.sps)
        if cut > 0:
            self.buf = buf[cut:]
            self.base += cut
        return (y > 0).astype(np.uint8)

    def process(self, iq):
        """iq: complex 48 kS/s.  Returns the bits (uint8 0/1) found in this block, polarity corrected."""
        x = np.asarray(iq, np.complex128)
        if not len(x):
            return np.zeros(0, np.uint8)
        y, self.zi = signal.lfilter(self.lp, 1.0, x, zi=self.zi)
        yy = np.concatenate(([self.prev], y))
        self.prev = y[-1]
        d = np.angle(yy[1:] * np.conj(yy[:-1]))                    # rad per sample
        m = float(np.mean(d))
        self.dc = m if not self.dc_init else self.dc + 0.1 * (m - self.dc)
        self.dc_init = True
        self.freq = self.dc * self.fs / (2 * math.pi)
        d, self.zs = signal.lfilter(self.sm, 1.0, d - self.dc, zi=self.zs)
        raw = self._bits(d)
        if len(raw):
            self.hist = np.concatenate((self.hist, raw))[-3200:]
            self.since += len(raw)
            if self.since >= 400 and len(self.hist) >= 1280:
                self.since = 0
                self._check_sync()
        self.n_bits += len(raw)
        return (raw ^ 1) if self.invert else raw

    def _check_sync(self):
        a = find_sync(self.hist)
        b = find_sync(self.hist ^ 1)
        if a and (not b or a[1] >= b[1]):
            self.invert, self.synced, self.sync_frames = False, True, a[1]
        elif b:
            self.invert, self.synced, self.sync_frames = True, True, b[1]
        else:
            self.synced = False


def probe(iq, fs=48000.0):
    """Is there a Tetrapol signal in this stretch of IQ (about a second)?  Returns a dict or None."""
    d = TetrapolDemod(fs)
    bits = []
    iq = np.asarray(iq, np.complex64)
    for i in range(0, len(iq), 2400):
        bits.append(d.process(iq[i:i + 2400]))
    if d.synced and d.sync_frames >= 6:
        return {"frames": d.sync_frames, "invert": d.invert, "freq": d.freq}
    return None


# ------------------------------------------------------------------------------------------------ test transmitter
def make_frames(nframes, seed=1):
    """Random 160 bit frames whose first byte carries the sync pattern."""
    rng = np.random.default_rng(seed)
    fr = rng.integers(0, 2, (nframes, FRAME_BITS)).astype(np.uint8)
    fr[:, 1:8] = DSYNC
    return fr.reshape(-1)


def modulate(bits, fs=48000.0, bt=0.25, cfo=0.0, noise=0.0, ppm=0.0, amp=0.5, seed=2, lead=0.02):
    """GMSK (h = 0.5, Gaussian BT) at 8000 bit/s; a 1 bit is a positive frequency step."""
    sps = int(round(fs / SYMBOL_RATE))
    nrz = np.repeat(2.0 * np.asarray(bits, float) - 1.0, sps)
    sigma = math.sqrt(math.log(2)) / (2 * math.pi * bt) * sps
    n = int(4 * sps) | 1
    t = np.arange(n) - (n - 1) / 2
    g = np.exp(-t * t / (2 * sigma * sigma))
    g /= g.sum()
    f = np.convolve(nrz, g, mode="same")
    ph = np.cumsum(f) * (math.pi / 2) / sps
    iq = np.exp(1j * ph)
    iq = np.concatenate((np.full(int(lead * fs), iq[0]), iq, np.full(int(lead * fs), iq[-1])))
    if ppm:
        k = np.arange(len(iq))
        kk = k * (1 + ppm * 1e-6)
        iq = np.interp(kk, k, iq.real) + 1j * np.interp(kk, k, iq.imag)
    k = np.arange(len(iq))
    iq = amp * iq * np.exp(2j * np.pi * cfo * k / fs)
    if noise:
        rng = np.random.default_rng(seed)
        iq = iq + noise * (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq))) / math.sqrt(2)
    return iq.astype(np.complex64)
