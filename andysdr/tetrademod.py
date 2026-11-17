"""TETRA downlink demodulator: 48 kS/s complex baseband -> pi/4-DQPSK symbols for osmo-tetra's `tetra-rx -i`.

TETRA is 18 000 symbols/s, root-raised-cosine (alpha 0.35) pi/4-DQPSK, 25 kHz channels.  This module does the
radio part only (frequency tracking, matched filter, symbol timing, differential detection).  The output is
one float per symbol: the phase step in units of pi/4 (nominally +-1 or +-3), which is exactly what
`tetra-rx -i` expects; tetra-rx does burst sync, descrambling, de-interleaving, Viterbi and the MAC layers.
"""
import math

import numpy as np
from scipy import signal

SYMBOL_RATE = 18000.0


def rrc_taps(fs, rate=SYMBOL_RATE, alpha=0.35, span=10):
    sps = fs / rate
    n = int(round(span * sps)) | 1
    t = (np.arange(n) - (n - 1) / 2) / sps                      # in symbol periods
    h = np.zeros(n)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1.0 - alpha + 4 * alpha / math.pi
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-9:
            h[i] = (alpha / math.sqrt(2)) * ((1 + 2 / math.pi) * math.sin(math.pi / (4 * alpha)) +
                                            (1 - 2 / math.pi) * math.cos(math.pi / (4 * alpha)))
        else:
            h[i] = (math.sin(math.pi * x * (1 - alpha)) + 4 * alpha * x * math.cos(math.pi * x * (1 + alpha))) / \
                   (math.pi * x * (1 - (4 * alpha * x) ** 2))
    return (h / np.sqrt(np.sum(h ** 2))).astype(np.float64)


class TetraDemod:
    def __init__(self, fs=48000.0):
        self.fs = float(fs)
        self.sps = self.fs / SYMBOL_RATE
        self.taps = rrc_taps(self.fs)
        self.zi = np.zeros(len(self.taps) - 1, np.complex128)
        self.nco_phase = 0.0
        self.freq = 0.0                       # Hz correction currently applied
        self.invert = False                   # True = spectrum is mirrored (I/Q swapped): flip symbol polarity
        self.ybuf = np.zeros(0, np.complex128)
        self.base = 0                         # global index of ybuf[0]
        self.tn = None                        # global (fractional) index of the next symbol instant
        self.Xclk = 0j                        # smoothed clock-tone
        self.last = 1 + 0j                    # last symbol sample (for the differential step)
        self.locked_metric = 0.0              # eye quality 0..1 (higher = cleaner constellation)
        self.n_total = 0

    # ---------------------------------------------------------------- helpers
    def _interp(self, pos):
        """4-point cubic Lagrange interpolation of ybuf at global positions `pos`."""
        local = pos - self.base
        i = np.floor(local).astype(np.int64)
        f = local - i
        y = self.ybuf
        ym1, y0, y1, y2 = y[i - 1], y[i], y[i + 1], y[i + 2]
        c_m1 = -f * (f - 1) * (f - 2) / 6
        c_0 = (f + 1) * (f - 1) * (f - 2) / 2
        c_1 = -(f + 1) * f * (f - 2) / 2
        c_2 = (f + 1) * f * (f - 1) / 6
        return c_m1 * ym1 + c_0 * y0 + c_1 * y1 + c_2 * y2

    # ---------------------------------------------------------------- main entry
    def process(self, iq):
        """iq: complex samples at `fs`.  Returns float32 array of symbol phase steps (units of pi/4)."""
        x = np.asarray(iq, np.complex128)
        if len(x) == 0:
            return np.zeros(0, np.float32)
        n = np.arange(len(x))
        ph = self.nco_phase - 2 * np.pi * self.freq / self.fs * n
        x = x * np.exp(1j * ph)
        self.nco_phase = float((ph[-1] - 2 * np.pi * self.freq / self.fs) % (2 * np.pi))
        y, self.zi = signal.lfilter(self.taps, [1.0], x, zi=self.zi)

        # append to the history buffer
        self.ybuf = np.concatenate([self.ybuf, y])
        start_global = self.base
        # clock-tone (Oerder-Meyr style) over this block, kept continuous through the global index
        g = np.arange(len(y)) + (self.base + len(self.ybuf) - len(y))
        e = np.abs(y) ** 2
        self.Xclk = 0.8 * self.Xclk + np.sum(e * np.exp(-2j * np.pi * g / self.sps))
        tau = -np.angle(self.Xclk) * self.sps / (2 * np.pi)         # optimum instant mod sps
        if self.tn is None:
            self.tn = (self.base + 4) + ((tau - (self.base + 4)) % self.sps)
        else:
            err = (tau - self.tn + self.sps / 2) % self.sps - self.sps / 2
            self.tn += 0.15 * err
        avail_end = self.base + len(self.ybuf) - 3
        count = int(max(0, math.floor((avail_end - self.tn) / self.sps)))
        if count <= 0:
            self._trim()
            return np.zeros(0, np.float32)
        pos = self.tn + self.sps * np.arange(count)
        s = self._interp(pos)
        self.tn += self.sps * count
        prev = np.concatenate(([self.last], s[:-1]))
        d = s * np.conj(prev)
        self.last = s[-1]
        mag = np.abs(d) + 1e-12
        dn = d / mag
        # frequency error: d^4 of a pi/4-DQPSK step is -1 plus 4*delta
        delta = np.angle(-np.sum(dn ** 4)) / 4.0                   # rad / symbol
        self.freq += 0.4 * delta * SYMBOL_RATE / (2 * np.pi)
        self.freq = float(np.clip(self.freq, -6000, 6000))
        steps = np.angle(dn) / (np.pi / 4)
        # eye quality: how close the steps sit to the nominal +-1/+-3 values
        dev = np.abs(np.abs(steps) - np.where(np.abs(steps) > 2, 3.0, 1.0))
        self.locked_metric = float(np.clip(1.0 - np.mean(dev) / 0.5, 0, 1))
        self.n_total += count
        self._trim()
        if self.invert:
            steps = -steps
        return steps.astype(np.float32)

    def _trim(self):
        keep_from = int(self.tn - 8) if self.tn is not None else 0
        drop = keep_from - self.base
        if drop > 4096:
            self.ybuf = self.ybuf[drop:]
            self.base += drop
