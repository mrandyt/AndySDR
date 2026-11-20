"""Morse (CW) decoder.

Takes the 48 kHz receiver audio, finds the strongest steady tone between 250 Hz and 1.5 kHz, turns it into a
key-down / key-up envelope at 1 ms resolution, measures the dot length as it goes (so it follows any speed from
about 5 to 45 WPM) and turns the dots and dashes into text.
"""
import numpy as np
from scipy import signal

FS = 48000
ENV_DEC = 48            # envelope at 1 kHz -> 1 ms steps
MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H", "..": "I",
    ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5", "-....": "6", "--...": "7",
    "---..": "8", "----.": "9", ".-.-.-": ".", "--..--": ",", "..--..": "?", ".----.": "'", "-.-.--": "!",
    "-..-.": "/", "-.--.": "(", "-.--.-": ")", ".-...": "&", "---...": ":", "-.-.-.": ";", "-...-": "=",
    ".-.-.": "+", "-....-": "-", "..--.-": "_", ".-..-.": '"', "...-..-": "$", ".--.-.": "@",
    "...-.-": "<SK>", "-.-.-": "<KA>", ".-.-": "<AA>", "...-.": "<SN>", "-...-.-": "<BK>", "........": "<ERR>",
}


class MorseDecoder:
    def __init__(self, on_text=None, on_status=None):
        self.on_text, self.on_status = on_text, on_status
        self.buf = np.zeros(0, np.float32)
        self.tone = 700.0
        self.phase = 0.0
        self.unit = 60.0            # dot length in ms (20 WPM to start with)
        self.marks = 0
        self.recent = []
        self.key = False
        self.run = 0                # ms spent in the current key state
        self.sym_ms = []
        self.space_sent = True
        self.noise = 1e-4
        self.t = 0
        self.peak = 1e-3
        self.since_tone = 0
        self.lp = signal.firwin(255, 90, fs=FS).astype(np.float32)
        self.zi = np.zeros(len(self.lp) - 1, np.complex64)
        self.tone_hist = []
        self.last_status = 0
        self.text_total = 0
        self.snr_db = 0.0

    # ------------------------------------------------------------------ tone finding
    def _find_tone(self, x):
        n = len(x)
        w = np.hanning(n)
        sp = np.abs(np.fft.rfft(x * w))
        f = np.fft.rfftfreq(n, 1 / FS)
        sel = (f > 250) & (f < 1500)
        s, ff = sp[sel], f[sel]
        k = int(np.argmax(s))
        med = float(np.median(s)) + 1e-12
        return float(ff[k]), float(s[k] / med)

    def process(self, audio):
        """Feed one block of audio (any length); returns nothing – text and status go to the callbacks."""
        x = np.asarray(audio, np.float32)
        self.buf = np.concatenate((self.buf, x))[-FS:]
        self.since_tone += len(x)
        if self.since_tone >= FS // 4 and len(self.buf) >= FS // 2:       # re-check the pitch four times a second
            self.since_tone = 0
            f, q = self._find_tone(self.buf[-FS // 2:])
            if q > 12:                                                      # a clear tone, not just noise
                self.tone_hist = (self.tone_hist + [f])[-4:]
                self.tone = float(np.median(self.tone_hist))
        # mix the tone to zero Hz, low-pass, take the envelope
        n = np.arange(len(x))
        w = -2 * np.pi * self.tone / FS
        osc = np.exp(1j * (self.phase + w * n)).astype(np.complex64)
        self.phase = (self.phase + w * len(x)) % (2 * np.pi)
        y, self.zi = signal.lfilter(self.lp, 1.0, x * osc, zi=self.zi)
        env = np.abs(y)
        k = len(env) // ENV_DEC
        if k == 0:
            return
        env = env[:k * ENV_DEC].reshape(k, ENV_DEC).mean(axis=1)           # 1 ms steps
        for v in env:
            self._step(float(v))
        self._status()

    # ------------------------------------------------------------------ keying
    def _step(self, v):
        # noise floor follows the quiet parts, peak follows the strong parts
        self.t += 1
        if not self.key:                                     # the noise floor is the average of the quiet parts
            rate = 0.02 if self.t < 600 else 0.001
            self.noise += (v - self.noise) * rate
        self.noise = max(self.noise, 1e-7)
        self.peak = v if v > self.peak else self.peak * 0.9996 + v * 0.0004
        self.peak = max(self.peak, self.noise * 1.5)
        span = self.peak - self.noise
        hi = self.noise + 0.55 * span if self.key else self.noise + 0.65 * span
        lo = self.noise + 0.40 * span
        down = (v > hi) if not self.key else (v > lo)
        self.snr_db = 20 * np.log10((self.peak + 1e-12) / (self.noise + 1e-12))
        valid = self.snr_db > 12 and self.t > 600
        if not valid:
            down = False
        if down == self.key:
            self.run += 1
            if not self.key:
                self._gap_check()
            return
        # a state change: ignore blips shorter than 8 ms
        if self.run < 8:
            self.run += 1
            return
        if self.key:
            self._mark(self.run)
        else:
            self._space(self.run)
        self.key = down
        self.run = 1

    def _split(self):
        """Duration that separates a dot from a dash, from the marks heard recently."""
        lo, hi = min(self.recent), max(self.recent)
        if len(self.recent) >= 3 and hi > 2.0 * lo:           # both dots and dashes seen: split between them
            return (lo * hi) ** 0.5
        return 2.0 * self.unit

    def _mark(self, ms):
        self.recent = (self.recent + [ms])[-14:]
        self.sym_ms.append(ms)
        split = self._split()
        if len(self.recent) >= 3 and max(self.recent) > 2.0 * min(self.recent):
            dots = [m for m in self.recent if m < split]
            self.unit = min(max(0.5 * self.unit + 0.5 * (sum(dots) / len(dots)), 22.0), 240.0)   # 5 .. 55 WPM
        self.marks += 1
        self.space_sent = False
        if len(self.sym_ms) > 8:                              # not Morse: reset
            self.sym_ms = []

    def _space(self, ms):
        pass

    def _gap_check(self):
        u = self.unit
        if len(self.recent) < 3 and self.recent:             # speed not known yet: assume the shortest mark may be a dash
            u = max(u, min(self.recent) / 3.0)
        if self.sym_ms and self.run >= 2.2 * u:
            self._emit_char()
        if not self.space_sent and not self.sym_ms and self.run >= 5.5 * u:
            self.space_sent = True
            self._out(" ")

    def _emit_char(self):
        split = self._split()                                # judged once the whole letter is in, with the latest speed
        sym = "".join("-" if m >= split else "." for m in self.sym_ms)
        self.sym_ms = []
        self._out(MORSE.get(sym, "¿"))

    def _out(self, s):
        self.text_total += len(s)
        if self.on_text:
            self.on_text(s)

    def _status(self):
        self.last_status += 1
        if self.last_status >= 5 and self.on_status:           # about twice a second
            self.last_status = 0
            self.on_status({"wpm": round(1200.0 / self.unit, 1), "tone": round(self.tone), "key": bool(self.key),
                            "snr": round(float(self.snr_db), 1), "ok": bool(self.snr_db > 12)})
