"""Automatic signal identification.

Looks at about one second of the received channel and works out what kind of transmission it is,
so AndySDR can switch to the right demodulator and start the right decoder by itself:

  4FSK 4800 Bd + 30 ms TDMA bursts  -> DMR            (DSD-FME, DMR)
  4FSK 4800 Bd continuous            -> P25 / DMR repeater / YSF  (DSD-FME, auto)
  4FSK 2400 Bd                       -> NXDN 48 or dPMR (dPMR inside PMR446)
  2FSK 512/1200/2400 Bd, wide dev.   -> POCSAG / FLEX pager (multimon-ng)
  AFSK 1200/2200 Hz tones            -> APRS / packet (multimon-ng)
  DTMF tone pairs                    -> DTMF (multimon-ng)
  SSTV (1200 Hz sync + 1500-2300)    -> SSTV pictures (built in)
  1500-2300 Hz fax tones, no sync    -> WEFAX charts (built in, SSB only)
  wide 25 kHz, non-constant envelope -> TETRA (shown only – UK Airwave is encrypted)
  otherwise                          -> analogue voice (FM with CTCSS tone reported, AM, SSB) – no decoder
"""
import numpy as np
from scipy import signal

FS = 48000
WIN = 48000            # analyse one second at a time
CTCSS = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9,
         114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9,
         186.2, 192.8, 203.5, 210.7, 218.1, 225.7, 233.6, 241.8, 250.3]
DTMF_LO, DTMF_HI = [697, 770, 852, 941], [1209, 1336, 1477, 1633]

_LP_CH = signal.firwin(129, 8500, fs=FS)          # 12.5/25 kHz channel for FSK analysis
_LP_DISC = signal.firwin(63, 6000, fs=FS)


def _band_power(freqs, p, f0, half):
    m = (freqs > f0 - half) & (freqs < f0 + half)
    return float(p[m].sum())


def _line_strength(x, rates, fs=FS):
    """Strength of spectral lines at the candidate symbol rates in x (e.g. squared derivative)."""
    x = x - x.mean()
    X = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / fs)
    out = {}
    for r in rates:
        pk = (f > r * 0.985) & (f < r * 1.015)
        ref = ((f > r * 0.8) & (f < r * 0.95)) | ((f > r * 1.05) & (f < r * 1.2))
        out[r] = float(X[pk].max() / (np.median(X[ref]) + 1e-20)) if pk.any() and ref.any() else 0.0
    return out


def _levels(d):
    """Number of clusters in the FM discriminator output (2 = 2FSK, 4 = 4FSK, 0 = continuous)."""
    lim = np.percentile(np.abs(d), 99) * 1.15 + 1e-9
    h, _ = np.histogram(np.clip(d, -lim, lim), bins=60, range=(-lim, lim))
    h = np.convolve(h, np.ones(3) / 3, mode="same")
    pk, _ = signal.find_peaks(h, prominence=h.max() * 0.15, distance=4)
    return len(pk)


def _kurtosis(d):
    d = d - d.mean()
    v = np.mean(d ** 2) + 1e-20
    return float(np.mean(d ** 4) / v ** 2)


class SignalClassifier:
    def __init__(self):
        self.reset()

    def reset(self):
        self.iq = []
        self.audio = []
        self.n = 0
        self.history = []       # recent kinds, for a stable decision
        self.last = None

    # ------------------------------------------------------------------
    def collect(self, out):
        """Add one 50 ms block. Returns (iq, audio) once a full second has been gathered."""
        iq = out.get("iq48")
        if iq is None:                      # WFM broadcast – nothing to identify
            return None
        self.iq.append(np.asarray(iq, np.complex64))
        self.audio.append(np.asarray(out["audio_ungated"], np.float32))
        self.n += len(iq)
        if self.n < WIN:
            return None
        iq = np.concatenate(self.iq)[-WIN:]
        audio = np.concatenate(self.audio)[-WIN:]
        self.iq, self.audio, self.n = [], [], 0
        return iq, audio

    def feed(self, out, mode, freq):
        """Feed one 50 ms block and classify in-line (used by the tests)."""
        got = self.collect(out)
        if got is None:
            return None
        return self.decide(self.classify(got[0], got[1], mode, freq))

    def decide(self, res):
        """Smooth successive results so one odd second does not flip the decoder."""
        if res is None:
            self.history.clear()
            return None
        self.history = (self.history + [res["kind"]])[-3:]
        # report a kind once it has been seen twice running (one second is enough for strong data)
        stable = self.history.count(res["kind"]) >= 2 or res.get("conf", 0) > 0.9
        res["stable"] = stable
        self.last = res
        return res

    # ------------------------------------------------------------------
    def classify(self, iq, audio, mode, freq=0):
        # ---- is there anything there?  occupied bandwidth & SNR from the 48 kHz spectrum
        f, P = signal.welch(iq, fs=FS, nperseg=1024, return_onesided=False)
        f, P = np.fft.fftshift(f), np.fft.fftshift(P)
        Pdb = 10 * np.log10(P + 1e-20)
        # noise floor: the quietest part of the flat region (the receiver filter rolls off above 16 kHz)
        floor = float(np.percentile(Pdb[np.abs(f) < 16000], 12))
        inband = np.abs(f) < 6500
        snr = float(np.max(Pdb[inband]) - floor)
        if snr < 9:
            return None
        # occupied bandwidth = span holding 99 % of the power above the noise floor
        sel = np.abs(f) < 16000
        pw = np.where(Pdb[sel] > floor + 6, P[sel] - 10 ** (floor / 10), 0.0)
        cpw = np.cumsum(pw) / (pw.sum() + 1e-30)
        fs_ = f[sel]
        bw = float(fs_[np.searchsorted(cpw, 0.995)] - fs_[np.searchsorted(cpw, 0.005)]) if pw.sum() > 0 else 0.0

        # ---- envelope and FM discriminator of a 12.5/25 kHz channel
        wide = bw > 15000
        x = signal.lfilter(signal.firwin(129, 12500 if wide else 8500, fs=FS), 1, iq)[200:]
        env = np.abs(x)
        env_full = float(np.std(env) / (np.mean(env) + 1e-12))
        # TDMA radios (DMR handsets) are off half the time – only analyse while the carrier is up
        onmask = signal.medfilt((env > 0.35 * np.percentile(env, 90)).astype(float), 49) > 0.5
        act = np.flatnonzero(onmask)
        # only judge the stretch where the transmitter was keyed (it may start or stop mid-window)
        a0, a1 = (int(act[0]), int(act[-1]) + 1) if len(act) else (0, len(onmask))
        duty = float(onmask[a0:a1].mean()) if a1 - a0 > FS * 0.3 else float(onmask.mean())
        on = env[onmask]
        env_cv = float(np.std(on) / (np.mean(on) + 1e-12)) if len(on) > 100 else 1.0
        d = np.angle(x[1:] * np.conj(x[:-1])) * FS / (2 * np.pi)
        d = signal.lfilter(_LP_DISC, 1, d)
        m = onmask[1:]
        # shrink the mask a little so burst edges do not count
        m = signal.medfilt(m.astype(float), 1) > 0.5
        m &= np.roll(m, 120) & np.roll(m, -120)
        dfull = np.where(m, d, 0.0)[100:]
        d = d[m][100:] if m.sum() > 4000 else d[100:]

        info = {"snr": round(snr, 1), "bw": round(bw / 1000, 1)}

        def R(kind, label, decoder="", sub="auto", dmode=None, conf=0.7, **extra):
            r = {"kind": kind, "label": label, "decoder": decoder, "sub": sub, "mode": dmode, "conf": conf}
            r.update(info)
            r.update(extra)
            return r

        # ---- TETRA: about 25 kHz wide, pi/4-DQPSK (envelope not constant)
        if wide and snr > 15 and env_full > 0.18 and duty > 0.75 and mode not in ("AM", "USB", "LSB", "CW"):
            return R("TETRA", "TETRA (π/4-DQPSK, 25 kHz) – Airwave is encrypted", dmode="DIG25", conf=0.8)

        # ---- audio-tone signals (work in FM and SSB): SSTV, WEFAX, AFSK/APRS, DTMF
        tone = self._tones(audio, mode)
        if tone:
            tone.update(info)
            return tone

        if mode in ("AM", "USB", "LSB", "CW"):
            kind = {"AM": "AM voice", "CW": "Morse (CW)"}.get(mode, "SSB voice")
            return R(kind, kind, conf=0.5)

        # ---- FSK digital: symbol rate lines + discriminator levels
        if env_cv < 0.35:
            dd = np.diff(dfull) ** 2
            lines = _line_strength(dd, [512, 1200, 1600, 2400, 3200, 4800])
            best = max(lines, key=lines.get)
            for r in sorted(lines):          # a 1200 Bd signal also shows a 2400 Hz harmonic
                if r < best and best % r == 0 and lines[r] > 12 and lines[r] > 0.1 * lines[best]:
                    best = r
                    break
            strength = lines[best]
            dev = float(np.percentile(np.abs(d), 95))
            lev = _levels(d)
            k = _kurtosis(d)
            info.update(baud=best, line=round(strength, 1), dev=round(dev), levels=lev, kurt=round(k, 2))
            if strength > 12 and dev > 900:
                if best == 4800:
                    burst = 0.3 < duty < 0.7 and a1 - a0 > FS * 0.3 and self._tdma(env[a0:a1])
                    if burst:
                        return R("DMR", "DMR (4FSK 4800 Bd, TDMA bursts)", "dsd", "dmr", "DIG", 0.9)
                    if 144e6 <= freq <= 146e6 or 430e6 <= freq <= 440e6:
                        return R("Digital 4FSK", "Digital 4FSK 4800 Bd – DMR repeater / P25 / Fusion / M17",
                                 "dsd", "auto", "DIG", 0.75)
                    return R("P25 / DMR", "P25 Phase 1 or DMR repeater (4FSK 4800 Bd)", "dsd", "auto", "DIG", 0.8)
                if best == 2400 and lev != 2 and k > 1.15:
                    if 446.0e6 <= freq <= 446.2e6:
                        return R("dPMR", "dPMR446 (4FSK 2400 Bd)", "dsd", "dpmr", "DIG", 0.85)
                    return R("NXDN / dPMR", "NXDN 4800 or dPMR (4FSK 2400 Bd)", "dsd", "nxdn48", "DIG", 0.75)
                if best in (512, 1200, 2400, 1600, 3200) and (lev == 2 or k < 1.4):
                    name = "FLEX" if best in (1600, 3200) else f"POCSAG {best}"
                    return R("Pager", f"Pager – {name} (2FSK {best} Bd)", "pager", "auto", None, 0.85)
        # ---- analogue FM voice (report a CTCSS sub-tone if there is one)
        ct = self._ctcss(d)
        return R("FM voice", "Analogue FM voice" + (f" · CTCSS {ct} Hz" if ct else ""), conf=0.6, ctcss=ct)

    # ------------------------------------------------------------------
    def _tdma(self, env):
        """DMR handsets transmit 27.5 ms bursts every 60 ms – look for that on/off pattern."""
        p = env.astype(np.float64) ** 2
        k = FS // 1000
        pm = p[: len(p) // k * k].reshape(-1, k).mean(1)      # 1 ms power
        hi = np.percentile(pm, 90) + 1e-20
        on = (pm > hi * 0.25).astype(float)
        duty = on.mean()
        if not 0.3 < duty < 0.7:
            return False
        o = on - on.mean()
        ac = np.correlate(o, o, "full")[len(o) - 1:]
        ac /= ac[0] + 1e-12
        return ac[60] > 0.5 and ac[30] < 0.0

    def _ctcss(self, d):
        dd = signal.decimate(signal.decimate(d, 8), 6)          # 1 kHz
        if len(dd) < 600:
            return None
        X = np.abs(np.fft.rfft(dd * np.hanning(len(dd)), 8192)) ** 2
        f = np.fft.rfftfreq(8192, 1 / 1000)
        band = (f > 60) & (f < 260)
        i = int(np.argmax(np.where(band, X, 0)))
        if X[i] < 25 * np.median(X[band]):
            return None
        best = min(CTCSS, key=lambda c: abs(c - f[i]))
        return best if abs(best - f[i]) < 1.5 else None

    def _tones(self, audio, mode):
        a = audio - audio.mean()
        if np.std(a) < 1e-4:
            return None
        f, P = signal.welch(a, fs=FS, nperseg=4096)
        tot = float(P[(f > 300) & (f < 3400)].sum()) + 1e-20
        # AFSK1200 (APRS / packet): energy at 1200 and 2200 Hz
        p12, p22 = _band_power(f, P, 1200, 200), _band_power(f, P, 2200, 200)
        if (p12 + p22) / tot > 0.5 and p12 / tot > 0.12 and p22 / tot > 0.1 and 0.25 < p12 / p22 < 4:
            return {"kind": "APRS", "label": "AFSK 1200 – APRS / packet data", "decoder": "data", "sub": "auto",
                    "mode": None, "conf": 0.85}
        # DTMF: one low-group and one high-group tone
        lo = max(_band_power(f, P, t, 20) for t in DTMF_LO)
        hi = max(_band_power(f, P, t, 20) for t in DTMF_HI)
        if (lo + hi) / tot > 0.7 and min(lo, hi) / tot > 0.15:
            return {"kind": "DTMF", "label": "DTMF tones", "decoder": "data", "sub": "auto", "mode": None,
                    "conf": 0.8}
        # SSTV / WEFAX: a single swept tone between 1100 and 2300 Hz
        z = signal.hilbert(signal.sosfilt(signal.butter(4, (1000, 2500), "bandpass", fs=FS, output="sos"), a))
        inst = np.diff(np.unwrap(np.angle(z))) * FS / (2 * np.pi)
        inst = signal.medfilt(inst[200:], 9)
        amp = np.abs(z[201:])
        steady = float(np.std(amp) / (np.mean(amp) + 1e-12))
        in_band = float(np.mean((inst > 1100) & (inst < 2350)))
        if in_band < 0.85 or steady > 0.45:
            return None
        sync = float(np.mean(np.abs(inst - 1200) < 60))
        pix = float(np.mean((inst > 1450) & (inst < 2350)))
        spread = float(np.std(inst[(inst > 1450) & (inst < 2350)])) if pix > 0.1 else 0.0
        if 0.008 < sync < 0.25 and pix > 0.55:
            return {"kind": "SSTV", "label": "SSTV picture", "decoder": "sstv", "sub": "auto", "mode": None,
                    "conf": 0.85}
        if sync < 0.01 and pix > 0.85 and spread > 120 and mode in ("USB", "LSB"):
            return {"kind": "WEFAX", "label": "Weather fax (WEFAX)", "decoder": "wefax", "sub": "auto",
                    "mode": None, "conf": 0.8}
        return None
