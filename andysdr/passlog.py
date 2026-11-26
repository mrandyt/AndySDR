"""Satellite pass report and gain learning.

Every automatic satellite pass leaves one line in `passes.jsonl`: which satellite, how high it went, the RF gain used, how far
the signal stood above the noise, and what came out (pictures or none).  From those lines the optional "learn the best gain"
mode picks the gain for the next pass: it tries a little lower and a little higher than the best gain so far, keeps whichever
gave the cleaner signal, and now and then checks its neighbours again in case things have changed.
"""
import json
import logging
import math
import os
import statistics
import threading
import time

import numpy as np

log = logging.getLogger("andysdr.passlog")

WINDOW = 12                # learning looks at the last dozen scored passes, so it follows slow changes
KEEP = 300                 # lines kept in the file
MIN_SAMPLES = 25           # about 25 s of signal needed before a pass counts for learning
REF_RANGE_KM = 1000.0      # SNR is scaled to this distance so a low pass and a high pass can be compared
BAND_HALF_HZ = {"lrpt": 60e3, "sstv": 8e3, "record": 8e3, "wefax": 8e3, "goes": 300e3, "metop": 300e3}


# --------------------------------------------------------------------------- measuring
def band_snr(spec_db, center, fs, f_sig, half_bw):
    """How far the channel stands above the noise floor, in dB, from one spectrum frame.

    Signal = average power in the channel; noise = the median of the bins well outside it (robust against other signals)."""
    n = len(spec_db)
    if n < 64 or fs <= 0:
        return None
    hz = fs / n
    k = int(round((f_sig - (center - fs / 2.0)) / hz))
    w = max(2, int(half_bw / hz))
    lo, hi = k - w, k + w + 1
    if hi <= 0 or lo >= n:
        return None
    lo, hi = max(0, lo), min(n, hi)
    p = np.asarray(spec_db[lo:hi], dtype=np.float64)
    if len(p) < 2:
        return None
    sig = 10.0 * math.log10(float(np.mean(10.0 ** (p / 10.0))) + 1e-30)
    edge = int(n * 0.08)                                   # the filter roll-off at both ends is not noise
    guard = 3 * w
    out = np.concatenate((np.asarray(spec_db[edge:max(edge, k - guard)], dtype=np.float64),
                          np.asarray(spec_db[min(n - edge, k + guard):n - edge], dtype=np.float64)))
    if len(out) < 16:
        return None
    return float(sig - np.median(out))


class PassMeter:
    """Collects one sample a second during a pass."""

    def __init__(self, el_fn=None):
        self.el_fn = el_fn              # ts -> (elevation deg, range km) or None
        self.s = []                     # (snr, norm_snr, el, gain)
        self.t0 = time.time()
        self._last = 0.0

    def add(self, now, snr, gain):
        if snr is None or now - self._last < 1.0:
            return
        self._last = now
        el, rng = None, None
        if self.el_fn:
            try:
                r = self.el_fn(now)
            except Exception:
                r = None
            if r:
                el, rng = r
        norm = snr + 20.0 * math.log10(max(rng, 200.0) / REF_RANGE_KM) if rng else None
        self.s.append((snr, norm, el, gain))

    def summary(self):
        if not self.s:
            return {"samples": 0}
        snr = [x[0] for x in self.s]
        high = [x[1] for x in self.s if x[1] is not None and x[2] is not None and x[2] >= 20.0]
        gains = [x[3] for x in self.s if x[3] is not None]
        out = {"samples": len(self.s), "secs": int(time.time() - self.t0),
               "snr_peak": round(max(snr), 1), "snr_med": round(statistics.median(snr), 1)}
        if gains:
            out["gain"] = round(statistics.median(gains), 1)
        if len(high) >= MIN_SAMPLES:
            out["score"] = round(statistics.median(high), 1)       # distance-corrected, comparable between passes
        return out


# --------------------------------------------------------------------------- the file
class PassLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.rows = []
        self.open_key = None            # the pass being recorded right now
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    try:
                        self.rows.append(json.loads(ln))
                    except ValueError:
                        pass
        except OSError:
            pass
        self.rows = self.rows[-KEEP:]

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in self.rows:
                    f.write(json.dumps(r) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            log.exception("pass log not saved")

    def add(self, rec):
        with self.lock:
            self.rows.append(rec)
            self.rows = self.rows[-KEEP:]
            self._save()

    def find(self, key):
        for r in reversed(self.rows):
            if r.get("key") == key:
                return r
        return None

    def add_image(self, key, n=1):
        with self.lock:
            r = self.find(key)
            if r is not None:
                r["images"] = r.get("images", 0) + n
                self._save()

    def set_images(self, key, shown, total, note=""):
        with self.lock:
            r = self.find(key)
            if r is not None:
                r["images"] = int(shown)
                r["decoded"] = True
                if note:
                    r["note"] = note
                elif total > shown:
                    r["note"] = f"{total - shown} blank discarded"
                self._save()

    def set_merged(self, key, rep, images):
        """File what came out of putting both receivers' frames together on this pass."""
        with self.lock:
            r = self.find(key)
            if r is not None and rep:
                r["merged"] = {"frames": rep.get("frames"), "gained": rep.get("gained"),
                               "gained_pct": rep.get("gained_pct"), "images": int(images or 0)}
                self._save()

    def result(self, r, now=None):
        now = now or time.time()
        if r.get("images", 0) > 0:
            return f"{r['images']} picture{'s' if r['images'] != 1 else ''}"
        if r.get("note") and r.get("decoded"):
            return "no picture – " + r["note"] if "SatDump" in r["note"] else "no picture"
        if r.get("action") == "lrpt" and not r.get("decoded") and now - r.get("end", 0) < 1800:
            return "processing…"
        return "no picture"

    def view(self, n=20, now=None):
        out = []
        for r in reversed(self.rows[-n:]):
            out.append({"key": r.get("key"), "sat": r.get("sat"), "name": r.get("name"), "start": r.get("start"),
                        "max_el": r.get("max_el"), "gain": r.get("gain"), "gain_mode": r.get("gain_mode"),
                        "snr": r.get("snr_peak"), "score": r.get("score"), "samples": r.get("samples", 0),
                        "result": self.result(r, now), "images": r.get("images", 0), "stopped": r.get("stopped")})
        return out

    def for_sat(self, sid):
        return [r for r in self.rows if r.get("sat") == sid]

    def text(self):
        lines = []
        for r in self.rows:
            lines.append(json.dumps(r))
        return "\n".join(lines)


# --------------------------------------------------------------------------- gain learning
def best_gain(rows, step):
    """(gain, score, passes) of the best gain tried so far, from the scored passes in `rows`; None without any."""
    by, gains = {}, {}
    for r in [x for x in rows if x.get("score") is not None and x.get("gain") is not None][-WINDOW:]:
        k = round(float(r["gain"]) / step)
        by.setdefault(k, []).append(float(r["score"]))
        gains.setdefault(k, []).append(float(r["gain"]))
    if not by:
        return None
    k = max(by, key=lambda x: statistics.mean(by[x]))
    return round(statistics.mean(gains[k]), 1), round(statistics.mean(by[k]), 1), len(by[k])


def suggest_gain(rows, knob, lo, hi, down=0.40, up=0.25):
    """The gain to use for the next pass of a satellite whose earlier passes are in `rows` (oldest first).  It never strays
    further than `down` / `up` (fractions of the gain range) below / above the knob."""
    span = max(1.0, float(hi) - float(lo))
    step = max(1.0, round(0.08 * span))
    low_b, high_b = max(float(lo), knob - down * span), min(float(hi), knob + up * span)
    allscored = [r for r in rows if r.get("score") is not None and r.get("gain") is not None]
    if not allscored:
        return float(knob)
    scored = allscored[-WINDOW:]
    by, gains = {}, {}
    for r in scored:
        k = round(float(r["gain"]) / step)
        by.setdefault(k, []).append(float(r["score"]))
        gains.setdefault(k, []).append(float(r["gain"]))
    mean = {k: statistics.mean(v) for k, v in by.items()}
    best = max(mean, key=mean.get)

    def ok(k):
        g = k * step
        return low_b - 1e-6 <= g <= high_b + 1e-6

    for k in (best - 1, best + 1):                          # try a step lower, then a step higher, once each
        if k not in mean and ok(k):
            return float(k * step)
    if len(allscored) % 4 == 0:                              # things drift: look at the neighbour not seen for longest
        cands = [k for k in (best - 1, best + 1) if ok(k)]
        if cands:
            last = {k: max((i for i, r in enumerate(scored) if round(float(r["gain"]) / step) == k), default=-1) for k in cands}
            return float(min(cands, key=lambda k: last[k]) * step)
    return round(min(high_b, max(low_b, statistics.mean(gains[best]))), 1)


def iq_summary(path, fs=240000):
    """A quick look at a finished pass recording (16-bit stereo WAV, 240 kS/s): how strong the noise is, and where the
    signal sits in the channel (centre in kHz from the tuned frequency, and how many dB it stands above the noise).
    The middle of the pass is compared with its quiet end.  Returns None when the file cannot be read."""
    try:
        raw = np.memmap(path, dtype="<i2", mode="r", offset=44)
        n = len(raw) // 2
        if n < fs * 60:
            return None

        def get(t0, t1):
            a = np.asarray(raw[2 * int(t0 * fs):2 * int(t1 * fs)], np.float32)
            return a[0::2] + 1j * a[1::2]

        N = 2048
        win = np.hanning(N)

        def psd(x):
            k = len(x) // N
            X = np.abs(np.fft.fftshift(np.fft.fft(x[:k * N].reshape(k, N) * win, axis=1), axes=1)) ** 2
            return X.mean(0)

        secs = n / fs
        mid = psd(get(secs * 0.4, secs * 0.4 + 30))
        ref = psd(get(secs - 25, secs - 5))              # the start is not used: the tuner is still settling there
        f = np.fft.fftshift(np.fft.fftfreq(N, 1 / fs)) / 1e3
        k = np.ones(9) / 9
        ex = np.convolve(mid / ref, k, mode="same")          # how far the middle stands above the ends, per bin
        band = np.abs(f) <= 70
        e = np.clip(ex[band] - 1.0, 0, None)
        centre = float((f[band] * e).sum() / e.sum()) if e.sum() > 0 else None
        a = np.asarray(raw[:2 * fs * 10], np.float32)
        return {"rms": round(float(np.sqrt(np.mean(a * a) / 2)), 1),
                "excess_db": round(float(10 * np.log10(mid[band].sum() / ref[band].sum())), 1),
                "centre_khz": None if centre is None else round(centre, 1)}
    except Exception:                                    # noqa: BLE001
        log.debug("iq_summary", exc_info=True)
        return None
