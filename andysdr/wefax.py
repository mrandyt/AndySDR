"""Built-in HF weather fax (WEFAX) decoder.

HF fax is sent in USB: 1500 Hz = black, 2300 Hz = white, 120 lines per minute, IOC 576.
Start tone = 300 Hz black/white alternation, stop tone = 450 Hz, followed by phasing lines
(white pulse at line start) used to line the picture up.

Tune 1.9 kHz BELOW the published (carrier) frequency in USB, e.g. Northwood 8040.0 kHz -> 8038.1 kHz,
or simply tune the published frequency – the decoder automatically searches 1.5-2.3 kHz.
"""
import os
import struct
import time
import zlib

import numpy as np
from scipy import signal

FS = 48000


def write_png(path, img):
    """Grey-scale uint8 2-D array -> PNG (no external libraries needed)."""
    h, w = img.shape
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png)
    os.replace(tmp, path)


class WefaxDecoder:
    name = "WEFAX"

    def __init__(self, out_dir, on_event=None, lpm=120, ioc=576):
        self.out_dir = out_dir
        self.on_event = on_event or (lambda *a, **k: None)
        self.lpm = lpm
        self.width = int(round(np.pi * ioc))          # 1810 px for IOC 576
        self.slant_ppm = 0.0
        self.offset = 0
        self.reset()
        b, a = signal.butter(4, 700, fs=FS)
        self.lp = (b, a)
        self.zi_r = signal.lfilter_zi(b, a) * 0
        self.zi_i = signal.lfilter_zi(b, a) * 0
        self.phase = 0.0
        self.prev = 1 + 0j

    @property
    def line_samples(self):
        return FS * 60.0 / self.lpm * (1 + self.slant_ppm * 1e-6)

    def reset(self):
        self.pix = np.zeros(0, np.float32)
        self.lines = []
        self.state = "free"           # free | phasing | image
        self.phasing_lines = []
        self.started = time.strftime("%Y%m%d_%H%M%S")
        self.path = None
        self._last_save = 0

    def process(self, audio):
        n = np.arange(len(audio))
        w = 2 * np.pi * 1900 / FS
        osc = np.exp(-1j * (self.phase + w * n))
        self.phase = (self.phase + w * len(audio)) % (2 * np.pi)
        z = audio * osc
        zr, self.zi_r = signal.lfilter(*self.lp, z.real, zi=self.zi_r)
        zi, self.zi_i = signal.lfilter(*self.lp, z.imag, zi=self.zi_i)
        zc = zr + 1j * zi
        prev = np.concatenate(([self.prev], zc[:-1]))
        self.prev = zc[-1] if len(zc) else self.prev
        f = np.angle(zc * np.conj(prev)) * FS / (2 * np.pi)      # -400 (black) .. +400 (white)
        px = np.clip((f + 400) / 800.0, 0, 1).astype(np.float32)
        self.pix = np.concatenate((self.pix, px))
        L = self.line_samples
        while len(self.pix) >= L:
            k = int(L)
            line = self.pix[:k]
            self.pix = self.pix[k:]
            self._line(line)

    def _tone(self, line):
        """Detect 300 Hz start / 450 Hz stop modulation in a line of pixel values."""
        spec = np.abs(np.fft.rfft(line - line.mean()))
        freqs = np.fft.rfftfreq(len(line), 1 / FS)
        total = spec.sum() + 1e-9
        for tone, name in ((300, "start"), (450, "stop")):
            band = (freqs > tone - 15) & (freqs < tone + 15)
            if spec[band].sum() / total > 0.25:
                return name
        return None

    def _line(self, line):
        tone = self._tone(line)
        if tone == "start":
            if self.state != "phasing":
                self.save()
                self.reset()
                self.state = "phasing"
                self.on_event("wefax", "Start tone – new chart")
            return
        if tone == "stop":
            if self.lines:
                self.save()
                self.on_event("wefax", f"Stop tone – chart saved ({len(self.lines)} lines)")
            self.reset()
            return
        # resample to image width
        idx = (np.arange(self.width + 1) * len(line) / self.width).astype(int)
        cs = np.concatenate(([0], np.cumsum(line)))
        row = (cs[idx[1:]] - cs[idx[:-1]]) / np.maximum(1, idx[1:] - idx[:-1])
        if self.state == "phasing":
            self.phasing_lines.append(row)
            # phasing lines are mostly black with a short white pulse
            if row.mean() < 0.35 and len(self.phasing_lines) < 40:
                return
            self._align()
            self.state = "image"
        row = np.roll(row, -self.offset)
        self.lines.append(row)
        if len(self.lines) - self._last_save >= 20:
            self.save()

    def _align(self):
        if not self.phasing_lines:
            return
        m = np.mean(np.array(self.phasing_lines), axis=0)
        k = max(3, self.width // 40)
        sm = np.convolve(m, np.ones(k) / k, mode="same")
        self.offset = int((np.argmax(sm) - k // 2) % self.width)
        self.on_event("wefax", f"Aligned picture (offset {self.offset}px)")

    def save(self):
        if len(self.lines) < 5:
            return None
        img = (np.clip(np.array(self.lines), 0, 1) * 255).astype(np.uint8)
        if not self.path:
            self.path = os.path.join(self.out_dir, f"wefax_{self.started}.png")
        write_png(self.path, img)
        self._last_save = len(self.lines)
        self.on_event("image", os.path.basename(self.path))
        return self.path
