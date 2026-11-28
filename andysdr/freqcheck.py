"""Is the receiver really on the signal, and if not, why not?

A satellite pass is the one time a receiver is handed a signal whose true frequency is known in advance. The orbit
says where the carrier will be, second by second, so any difference between that and where the carrier actually
turns up is error - and measuring it through a whole pass says *which* error it is, which a single reading never
can:

* **A constant offset, the same at the start, the middle and the end, is the receiver's own oscillator.** Its
  crystal is a little fast or a little slow, by the same proportion at every frequency, and the cure is a ppm
  correction. At 137.9 MHz one part per million is 138 Hz, so a 1.4 kHz offset is about ten parts per million -
  which is also 11 kHz at 1090 MHz, and that is why an oscillator error shows up first on the bands where it costs
  the most.

* **An offset that grows and shrinks with the satellite's speed towards you is not the oscillator at all.** The
  Doppler shift is largest at the start and end of a pass and passes through zero overhead, so an error that
  follows that shape means the orbit prediction is wrong - usually stale orbit data - and no ppm correction will
  help. Correcting that one with ppm would make every other band worse.

* **An offset that creeps in one direction through the pass** is warm-up drift: the crystal settling as the
  receiver heats up.

Telling the three apart needs readings spread across a pass, which is what this collects. The measurement itself
does not look for a peak - a Meteor LRPT carrier is about 120 kHz wide and almost flat on top, so its peak wanders
with the noise. It slides a block the width of the signal across the spectrum, takes the position holding the most
power, and then finds the middle of the power inside that block, which gives an answer finer than one bin.
"""
import logging
import math

log = logging.getLogger("andysdr.freqcheck")

SEARCH_HZ = 30000.0        # how far either side of where it should be to look: more than any plausible error
MIN_SNR_DB = 6.0           # a reading weaker than this is not a measurement of anything
MIN_READINGS = 6           # ...and this many are needed before anything is concluded
PPM_WORTH_SAYING = 2.0     # below this the correction is not worth making
SPREAD_LIMIT_HZ = 900.0    # readings scattered more widely than this are not describing one steady offset


def carrier_offset(spec_db, center, fs, want_hz, half_bw, search_hz=SEARCH_HZ):
    """How far the signal actually is from `want_hz`, in Hz. None when there is nothing to measure.

    `spec_db` is one spectrum frame in dB, covering `center` +- fs/2. `half_bw` is half the width of the wanted
    signal. Positive means the signal is higher in frequency than the receiver is tuned.
    """
    import numpy as np

    n = len(spec_db)
    if n < 256 or fs <= 0 or half_bw <= 0:
        return None
    hz = fs / n
    k_want = (want_hz - (center - fs / 2.0)) / hz
    w = int(round(half_bw / hz))                       # half the signal's width, in bins
    span = int(round(search_hz / hz))
    if w < 3 or k_want - w - span < 0 or k_want + w + span >= n:
        return None                                    # the search would run off the end of the spectrum

    lin = np.power(10.0, np.asarray(spec_db, dtype=np.float64) / 10.0)
    floor = float(np.median(lin))
    p = np.maximum(lin - floor, 0.0)                   # power above the noise, so the noise cannot drag the answer

    lo = int(round(k_want - span))
    hi = int(round(k_want + span))
    cum = np.concatenate(([0.0], np.cumsum(p)))
    best_k, best_sum = None, 0.0
    for k in range(lo, hi + 1):                        # slide a block the width of the signal and take the fullest
        a, b = k - w, k + w + 1
        s = float(cum[b] - cum[a])
        if s > best_sum:
            best_sum, best_k = s, k
    if best_k is None or best_sum <= 0:
        return None

    seg = p[best_k - w:best_k + w + 1]
    tot = float(seg.sum())
    if tot <= 0:
        return None
    # The middle of the power inside that block: finer than one bin, and steadier than the highest point, because
    # the top of an LRPT carrier is flat and its peak moves about with the noise.
    mid = float((seg * np.arange(best_k - w, best_k + w + 1)).sum() / tot)

    noise = floor * len(seg)
    if noise > 0 and 10.0 * math.log10(max(tot, 1e-30) / noise) < MIN_SNR_DB:
        return None                                    # too weak to be measuring the signal rather than the noise
    return float((mid - k_want) * hz)


class Tracker:
    """Collects the offset through one pass on one receiver, and says what it means at the end."""

    def __init__(self, freq_hz, rx=""):
        self.freq = float(freq_hz or 0.0)
        self.rx = rx
        self.rows = []                                 # (seconds into the pass, offset Hz, elevation, doppler Hz)

    def add(self, t, offset, el=None, doppler=None):
        if offset is None or not math.isfinite(offset):
            return
        self.rows.append((float(t), float(offset),
                          None if el is None else float(el),
                          None if doppler is None else float(doppler)))

    # ------------------------------------------------------------------ what it all means
    def result(self):
        """{"n","median","spread","ppm","verdict","why"} or None when there is not enough to say anything."""
        if len(self.rows) < MIN_READINGS:
            return None
        offs = sorted(r[1] for r in self.rows)
        n = len(offs)
        med = offs[n // 2] if n % 2 else 0.5 * (offs[n // 2 - 1] + offs[n // 2])
        q1, q3 = offs[n // 4], offs[(3 * n) // 4]
        spread = q3 - q1
        ppm = (med / self.freq) * 1e6 if self.freq > 0 else 0.0
        out = {"n": n, "median": round(med, 1), "spread": round(spread, 1), "ppm": round(ppm, 2),
               "freq": self.freq, "rx": self.rx}
        out.update(self._verdict(med, spread, ppm))
        return out

    def _verdict(self, med, spread, ppm):
        doppler_like = self._follows_doppler()
        if doppler_like is not None and doppler_like > 0.6 and abs(med) < abs(spread):
            return {"verdict": "orbit",
                    "why": ("The error follows the satellite's own Doppler shift rather than staying put, so this is "
                            "the orbit prediction rather than the receiver - usually orbit data that needs "
                            "refreshing. A ppm correction would not fix it and would put every other band out.")}
        if spread > SPREAD_LIMIT_HZ and abs(med) < spread:
            return {"verdict": "unsteady",
                    "why": ("The readings are too scattered to describe one steady offset (%.0f Hz apart), so there "
                            "is nothing here worth correcting. A weak pass does this." % spread)}
        if abs(ppm) < PPM_WORTH_SAYING:
            return {"verdict": "good",
                    "why": ("The receiver is on frequency: %+.0f Hz at %.3f MHz, which is %+.1f ppm and not worth "
                            "correcting." % (med, self.freq / 1e6, ppm))}
        at1090 = ppm * 1090.0                           # the same proportional error, in Hz, up at 1090 MHz
        return {"verdict": "oscillator",
                "why": ("The error stays put across the pass at %+.0f Hz, which is %+.1f ppm - the receiver's own "
                        "crystal, not the orbit. The same proportion is %+.0f Hz at 1090 MHz, which is where it "
                        "costs the most. Setting this receiver's frequency correction to %+.1f ppm would take it "
                        "out." % (med, ppm, at1090, -ppm))}

    def _follows_doppler(self):
        """How closely the error tracks the Doppler shift: 1 means exactly, 0 means not at all. None if unknown."""
        pairs = [(r[3], r[1]) for r in self.rows if r[3] is not None]
        if len(pairs) < MIN_READINGS:
            return None
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        if sxx < 1e-9 or syy < 1e-9:
            return None
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        return abs(sxy / math.sqrt(sxx * syy))


def sentence(res):
    """One line for the pass report."""
    if not res:
        return ""
    who = {"rx1": "Main receiver", "rx2": "Second receiver"}.get(res.get("rx"), "Receiver")
    return f"{who} frequency check ({res['n']} readings): {res['median']:+.0f} Hz, {res['ppm']:+.2f} ppm - {res['why']}"
