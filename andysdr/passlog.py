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
TRACK_EVERY = 3            # one point on the antenna map for every third reading (about every 3 s)
TRACK_MAX = 400            # most points kept per pass
TRACE_EVERY_S = 3          # the signal trace keeps a reading every few seconds...
TRACE_MAX = 400            # ...and no more than this many
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
    sig_lin = float(np.mean(10.0 ** (p / 10.0)))
    edge = int(n * 0.08)                                   # the filter roll-off at both ends is not noise
    out = None
    for guard in (3 * w, 2 * w, w + 2):                    # a wide channel in a narrow span leaves no room for a wide guard
        lo_s = np.asarray(spec_db[edge:max(edge, k - guard)], dtype=np.float64)
        hi_s = np.asarray(spec_db[min(n - edge, k + guard):n - edge], dtype=np.float64)
        cand = np.concatenate((lo_s, hi_s))
        if len(cand) >= 16:
            out = cand
            break
    if out is None:
        return None
    noise_lin = float(np.median(10.0 ** (out / 10.0)))
    # The channel holds signal AND noise, so the noise has to come off before the two are compared: without that a
    # channel with nothing in it still reads +2 to +3 dB, and every weak signal is flattered by a few dB.
    return float(10.0 * math.log10(max(sig_lin - noise_lin, noise_lin * 0.02) / (noise_lin + 1e-30)))


class PassMeter:
    """Collects one sample a second during a pass."""

    def __init__(self, el_fn=None):
        self.el_fn = el_fn              # ts -> (elevation deg, range km[, azimuth deg]) or None
        self.s = []                     # (snr, norm_snr, el, gain)
        self.track = []                 # (azimuth, elevation, distance-corrected snr), a point every few seconds
        self.t0 = time.time()
        self._last = 0.0

    def add(self, now, snr, gain):
        if snr is None or not math.isfinite(snr) or now - self._last < 1.0:      # a dropped buffer can read as -inf / NaN: not a measurement
            return
        self._last = now
        el, rng, az = None, None, None
        if self.el_fn:
            try:
                r = self.el_fn(now)
            except Exception:
                r = None
            if r:
                el, rng = r[0], r[1]
                az = r[2] if len(r) > 2 else None
        norm = snr + 20.0 * math.log10(max(rng, 200.0) / REF_RANGE_KM) if rng else None
        self.s.append((snr, norm, el, gain, now - self.t0))
        if az is not None and el is not None and norm is not None and len(self.s) % TRACK_EVERY == 0:
            self.track.append((int(round(az)) % 360, int(round(el)), round(norm, 1)))

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
        if self.track:
            out["track"] = [list(p) for p in self.track[-TRACK_MAX:]]     # [azimuth, elevation, strength] for the antenna map
        tr = self.trace()
        if tr:
            out["snr_trace"] = tr
        return out

    def trace(self, every=TRACE_EVERY_S):
        """[[seconds from the start of the recording, SNR dB]], thinned.

        Kept only until the decoder has finished, so the signal can be laid against the frames that arrived and the
        level that actually produced them worked out; `PassLog.set_frames` throws it away afterwards.
        """
        out, nxt = [], 0.0
        for x in self.s:
            if len(x) < 5 or x[4] is None:
                continue
            if x[4] >= nxt:
                out.append([int(round(x[4])), round(x[0], 1)])
                nxt = x[4] + every
        return out[:TRACE_MAX]


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

    def set_images(self, key, shown, total, note="", fill=None):
        with self.lock:
            r = self.find(key)
            if r is not None:
                r["images"] = int(shown)
                r["decoded"] = True
                if fill and fill.get("pct") is not None:
                    r["fill"] = fill["pct"]              # % of scan lines with data (between first and last line)
                    if fill.get("n"):
                        r["fill_good"], r["fill_n"] = int(fill.get("good") or 0), int(fill["n"])
                    if fill.get("poor"):
                        r["poor"] = True                  # the pictures were set aside, not shown in the gallery
                if note:
                    r["note"] = note
                elif total > shown:
                    r["note"] = f"{total - shown} blank discarded"
                self._save()

    def set_frames(self, key, m):
        """File what the frames say about this pass, and drop the signal trace they were measured against.

        `m` is what `framesnr.measure` returned: how many of the frames the satellite sent actually arrived, and -
        when the window could be placed against the trace - the signal level that was producing them.
        """
        with self.lock:
            r = self.find(key)
            if r is None:
                return
            if m:
                keep = {k: m[k] for k in ("frames", "sent", "pct", "secs", "snr_lo", "snr_med", "snr_hi") if k in m}
                if keep:
                    r["rx_frames"] = keep
            r.pop("snr_trace", None)                      # served its purpose: it is big and nothing else reads it
            self._save()

    def set_merged(self, key, rep, images):
        """File what came out of putting both receivers' frames together on this pass."""
        with self.lock:
            r = self.find(key)
            if r is not None and rep:
                r["merged"] = {"frames": rep.get("frames"), "gained": rep.get("gained"),
                               "gained_pct": rep.get("gained_pct"), "images": int(images or 0),
                               "fill": (rep.get("fill") or {}).get("pct")}
                self._save()

    def set_user_note(self, key, text):
        """A note the user wrote on a pass (for example which way the antenna was pointing). Empty text removes it."""
        text = " ".join(str(text or "").split())[:200]
        with self.lock:
            r = self.find(key)
            if r is None:
                return False
            if text:
                r["user_note"] = text
            else:
                r.pop("user_note", None)
            self._save()
            return True

    def result(self, r, now=None):
        now = now or time.time()
        if r.get("poor"):
            return f"poor ({r.get('fill')}% filled) – set aside"
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
                        "result": self.result(r, now), "images": r.get("images", 0), "stopped": r.get("stopped"),
                        "fill": r.get("fill"), "fill_good": r.get("fill_good"), "fill_n": r.get("fill_n"),
                        "merged_fill": (r.get("merged") or {}).get("fill"),
                        "rx_frames": r.get("rx_frames"), "rx": r.get("rx"),
                        "user_note": r.get("user_note")})
        return out

    def antenna_map(self, n=12, min_el=5):
        """Where in the sky the signal was strong and weak over the last n passes that have a track.

        Strength is distance-corrected (see PassMeter), so a low pass is not blamed for being far away.
        Returns {"passes": n, "points": [[az, el, strength], ...], "sectors": {...}, "text": "..."}.
        """
        rows = [r for r in self.rows if r.get("track")][-max(1, int(n)):]
        pts = [p for r in rows for p in r["track"] if p[1] >= min_el]
        sec = {}
        for az, el, v in pts:
            sec.setdefault(int(((az + 22.5) % 360) // 45), []).append(v)
        names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        avg = {names[k]: round(statistics.mean(v), 1) for k, v in sec.items() if len(v) >= 5}
        text = ""
        if len(avg) >= 3:
            best, worst = max(avg, key=avg.get), min(avg, key=avg.get)
            gap = round(avg[best] - avg[worst], 1)
            text = (f"Strongest toward {best} ({avg[best]} dB), weakest toward {worst} ({avg[worst]} dB) – {gap} dB apart." if gap >= 1.5
                    else "Pretty even in every direction – the antenna is not favouring any side.")
        return {"passes": len(rows), "points": pts[-1500:], "sectors": avg, "text": text}

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
