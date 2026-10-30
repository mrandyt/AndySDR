"""Built-in SSTV (slow-scan TV) picture decoder – and a test-pattern encoder.

Supported modes (picked automatically from the VIS header sent before every picture):
  Martin M1 / M2, Scottie S1 / S2 / DX, Robot 36 / 72, PD 50 / 90 / 120 / 160 / 180 / 240.

Where to listen in the UK:
  * ISS (ARISS SSTV events)  145.800 MHz NFM  – usually PD 120 / PD 180
  * 2 m SSTV                 144.500 MHz NFM
  * 20 m SSTV                14.230 MHz USB   (also 14.233)
  * 40 m SSTV                7.165 MHz LSB    * 80 m  3.735 MHz LSB

Audio: 1200 Hz = sync, 1500 Hz = black, 2300 Hz = white. The decoder follows the sync pulses,
so small clock errors (slant) are corrected automatically.
"""
import os
import struct
import time
import zlib

import numpy as np
from scipy import signal

FS = 48000          # input audio rate
DEC = 4
FSD = FS // DEC     # internal rate: 12 kHz frequency samples

# segment kinds: sync, porch(freq), sep(freq), Y/R/G/B/RY/BY/Y0/Y1/C (picture data)
MODES = {
    "Martin M1": dict(vis=44, w=320, h=256, seq=[("sync", 4.862), ("porch", 0.572), ("G", 146.432), ("porch", 0.572),
                                                 ("B", 146.432), ("porch", 0.572), ("R", 146.432), ("porch", 0.572)]),
    "Martin M2": dict(vis=40, w=320, h=256, seq=[("sync", 4.862), ("porch", 0.572), ("G", 73.216), ("porch", 0.572),
                                                 ("B", 73.216), ("porch", 0.572), ("R", 73.216), ("porch", 0.572)]),
    "Scottie S1": dict(vis=60, w=320, h=256, pre_sync=9.0,
                       seq=[("porch", 1.5), ("G", 138.24), ("porch", 1.5), ("B", 138.24), ("sync", 9.0),
                            ("porch", 1.5), ("R", 138.24)]),
    "Scottie S2": dict(vis=56, w=320, h=256, pre_sync=9.0,
                       seq=[("porch", 1.5), ("G", 88.064), ("porch", 1.5), ("B", 88.064), ("sync", 9.0),
                            ("porch", 1.5), ("R", 88.064)]),
    "Scottie DX": dict(vis=76, w=320, h=256, pre_sync=9.0,
                       seq=[("porch", 1.5), ("G", 345.6), ("porch", 1.5), ("B", 345.6), ("sync", 9.0),
                            ("porch", 1.5), ("R", 345.6)]),
    "Robot 36": dict(vis=8, w=320, h=240, seq=[("sync", 9.0), ("porch", 3.0), ("Y", 88.0), ("sep", 4.5),
                                               ("porch2", 1.5), ("C", 44.0)]),
    "Robot 72": dict(vis=12, w=320, h=240, seq=[("sync", 9.0), ("porch", 3.0), ("Y", 138.0), ("sepR", 4.5),
                                                ("porch2", 1.5), ("RY", 69.0), ("sepB", 4.5), ("porch2", 1.5),
                                                ("BY", 69.0)]),
}
for _name, _vis, _t, _w, _h in (("PD 50", 93, 91.52, 320, 256), ("PD 90", 99, 170.24, 320, 256),
                                ("PD 120", 95, 121.6, 640, 496), ("PD 160", 98, 195.584, 512, 400),
                                ("PD 180", 96, 183.04, 640, 496), ("PD 240", 97, 244.48, 640, 496)):
    MODES[_name] = dict(vis=_vis, w=_w, h=_h, pd=True,
                        seq=[("sync", 20.0), ("porch", 2.08), ("Y0", _t), ("RY", _t), ("BY", _t), ("Y1", _t)])
BY_VIS = {m["vis"]: n for n, m in MODES.items()}
PIX_KINDS = {"Y", "R", "G", "B", "RY", "BY", "Y0", "Y1", "C"}


def _seg_freq(kind, line):
    if kind == "sync":
        return 1200.0
    if kind == "sep":
        return 1500.0 if line % 2 == 0 else 2300.0
    if kind == "sepR":
        return 1500.0
    if kind == "sepB":
        return 2300.0
    if kind == "porch2":
        return 1900.0
    return 1500.0


def line_ms(mode):
    return sum(d for _, d in MODES[mode]["seq"])


def scan_lines(mode):
    m = MODES[mode]
    return m["h"] // 2 if m.get("pd") else m["h"]


def ycc_to_rgb(y, cr, cb):
    y, cr, cb = (np.asarray(a, np.float32) for a in (y, cr, cb))
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    return np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)


def rgb_to_ycc(img):
    img = img.astype(np.float32)
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cr = 128 + 0.713 * (r - y)
    cb = 128 + 0.564 * (b - y)
    return y, np.clip(cr, 0, 255), np.clip(cb, 0, 255)


def write_png_rgb(path, img):
    h, w, _ = img.shape
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png)
    os.replace(tmp, path)


# ======================================================================== encoder (tests / simulator)
def test_pattern(w, h):
    x = np.linspace(0, 1, w)[None, :].repeat(h, 0)
    y = np.linspace(0, 1, h)[:, None].repeat(w, 1)
    img = np.zeros((h, w, 3), np.uint8)
    bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    for i, c in enumerate(bars):
        img[: h // 2, int(i * w / 8): int((i + 1) * w / 8)] = c
    img[h // 2:, :, 0] = (x[h // 2:] * 255).astype(np.uint8)
    img[h // 2:, :, 1] = (y[h // 2:] * 255).astype(np.uint8)
    img[h // 2:, :, 2] = 128
    img[h // 2 - 3: h // 2 + 3] = 40
    return img


def encode(mode, img=None, fs=FS, return_freq=False):
    """Picture -> SSTV audio (float, 1.0 peak). Used by the simulator and the self-test."""
    m = MODES[mode]
    w, h = m["w"], m["h"]
    if img is None:
        img = test_pattern(w, h)
    Y, CR, CB = rgb_to_ycc(img)
    parts = []   # (freq or array of freqs, duration ms)

    def tone(f, ms):
        parts.append((f, ms))

    tone(1900, 300); tone(1200, 10); tone(1900, 300); tone(1200, 30)
    code = m["vis"]
    bits = [(code >> i) & 1 for i in range(7)]
    bits.append(sum(bits) % 2)
    for b in bits:
        tone(1100 if b else 1300, 30)
    tone(1200, 30)
    if m.get("pre_sync"):
        tone(1200, m["pre_sync"])
    for ln in range(scan_lines(mode)):
        for kind, ms in m["seq"]:
            if kind not in PIX_KINDS:
                tone(_seg_freq(kind, ln), ms)
                continue
            if m.get("pd"):
                r0, r1 = 2 * ln, 2 * ln + 1
                row = {"Y0": Y[r0], "Y1": Y[r1], "RY": (CR[r0] + CR[r1]) / 2, "BY": (CB[r0] + CB[r1]) / 2}[kind]
            elif kind == "C":
                row = CR[ln] if ln % 2 == 0 else CB[ln]
            else:
                row = {"Y": Y[ln], "RY": CR[ln], "BY": CB[ln], "R": img[ln, :, 0], "G": img[ln, :, 1],
                       "B": img[ln, :, 2]}[kind]
            parts.append((1500.0 + np.asarray(row, np.float32) / 255.0 * 800.0, ms))
    # synthesise with exact (fractional) timing
    total = sum(ms for _, ms in parts) / 1000.0
    n = int(round(total * fs)) + 2
    f = np.full(n, 1500.0, np.float32)
    t = 0.0
    for val, ms in parts:
        a, b = int(round(t * fs)), int(round((t + ms / 1000.0) * fs))
        t += ms / 1000.0
        if b <= a:
            continue
        if np.isscalar(val):
            f[a:b] = val
        else:
            idx = np.minimum((np.arange(b - a) * len(val) / (b - a)).astype(int), len(val) - 1)
            f[a:b] = val[idx]
    if return_freq:
        return f
    ph = np.mod(2 * np.pi * np.cumsum(f.astype(np.float64)) / fs, 2 * np.pi)
    return np.sin(ph).astype(np.float32)


# ======================================================================== decoder
class SstvDecoder:
    name = "SSTV"

    def __init__(self, out_dir, on_event=None, watch_only=False):
        self.out_dir = out_dir
        self.on_event = on_event or (lambda *a: None)
        self.watch_only = watch_only          # True = only look for the VIS header (auto-detect)
        b, a = signal.butter(4, 1150, fs=FS)
        self.lp = (b, a)
        self.zr = np.zeros(4)
        self.zi = np.zeros(4)
        self.phase = 0.0
        self.prev = 1 + 0j
        self.carry = np.zeros(0, np.float32)
        self.f = np.zeros(0, np.float32)      # instantaneous frequency at 12 kHz
        self.base = 0                         # absolute index of self.f[0]
        self.state = "idle"
        self.mode = None
        self.vis_found = None

    # ---------------------------------------------------------------- front end
    def _demod(self, audio):
        audio = np.asarray(audio, np.float64)
        n = np.arange(len(audio))
        w = 2 * np.pi * 1700 / FS
        z = audio * np.exp(-1j * (self.phase + w * n))
        self.phase = (self.phase + w * len(audio)) % (2 * np.pi)
        zr, self.zr = signal.lfilter(*self.lp, z.real, zi=self.zr)
        zi, self.zi = signal.lfilter(*self.lp, z.imag, zi=self.zi)
        zc = zr + 1j * zi
        prev = np.concatenate(([self.prev], zc[:-1]))
        self.prev = zc[-1] if len(zc) else self.prev
        f = 1700 + np.angle(zc * np.conj(prev)) * FS / (2 * np.pi)
        f = np.concatenate((self.carry, f.astype(np.float32)))
        k = len(f) // DEC * DEC
        self.carry = f[k:]
        return f[:k].reshape(-1, DEC).mean(1)

    def process(self, audio):
        fd = self._demod(audio)
        self.f = np.concatenate((self.f, fd))
        guard = 0
        while guard < 64:
            guard += 1
            if self.state == "idle":
                if not self._find_vis():
                    break
            elif self.state == "image":
                if not self._next_line():
                    break
            else:
                break
        if self.state == "idle":
            keep = int(1.2 * FSD)
            if len(self.f) > keep:
                drop = len(self.f) - keep
                self.f = self.f[drop:]
                self.base += drop

    # ---------------------------------------------------------------- VIS header
    def _find_vis(self):
        ms = FSD // 1000                                   # 12 samples per ms
        nms = len(self.f) // ms
        if nms < 420:
            return False
        fm = self.f[: nms * ms].reshape(nms, ms).mean(1)
        near12 = (np.abs(fm - 1200) < 70).astype(np.float32)
        near19 = (np.abs(fm - 1900) < 90).astype(np.float32)
        c12 = np.concatenate(([0], np.cumsum(near12)))
        c19 = np.concatenate(([0], np.cumsum(near19)))
        # start bit (30 ms of 1200) preceded by >= 150 ms of 1900 leader, full header must fit
        last = nms - 310        # header plus stop bit must be in the buffer
        for i in range(160, last):
            if c12[i + 30] - c12[i] < 24 or c19[i] - c19[i - 150] < 120:
                continue
            # refine to the exact start edge of the start bit
            j = i
            while j < i + 8 and fm[j] > 1550:
                j += 1
            while j > i - 6 and fm[j - 1] < 1550:
                j -= 1
            bits = []
            for k in range(8):
                a = j + 30 + 30 * k
                bits.append(1 if np.mean(fm[a + 6: a + 24]) < 1200 else 0)
            stop = np.mean(fm[j + 270: j + 290])
            code = sum(b << k for k, b in enumerate(bits[:7]))
            if sum(bits) % 2 != 0 or abs(stop - 1200) > 120 or code not in BY_VIS:
                continue
            mode = BY_VIS[code]
            self.vis_found = mode
            start = (j + 300) * ms + int(MODES[mode].get("pre_sync", 0) * FSD / 1000)
            self.f = self.f[start:]
            self.base += start
            if self.watch_only:
                # keep the picture data – the auto-detector promotes this object to a full decoder
                self.state = "found"
                return False
            self._start_image(mode)
            return True
        drop = max(0, (last - 160)) * ms
        if drop > 0 and nms > 900:
            self.f = self.f[drop:]
            self.base += drop
        return False

    def promote(self):
        """Called when the auto-detector hands over a VIS it found while watching."""
        self.watch_only = False
        if self.state == "found":
            self._start_image(self.vis_found)
        else:
            self.state = "idle"

    # ---------------------------------------------------------------- picture
    def _start_image(self, mode):
        m = MODES[mode]
        self._img_base = self.base        # picture start as an absolute index (buffer gets trimmed)
        self.mode = mode
        self.state = "image"
        self.line = 0
        self.lines_total = scan_lines(mode)
        self.period = line_ms(mode) * FSD / 1000.0
        self.seq = []
        t = 0.0
        for kind, ms in m["seq"]:
            self.seq.append((kind, t * FSD / 1000.0, ms * FSD / 1000.0))
            t += ms
        self.sync_off, self.sync_len = next((o, d) for k, o, d in self.seq if k == "sync")
        self.img = np.zeros((m["h"], m["w"], 3), np.uint8)
        self.rows_done = 0
        self.fit = []                 # (line, measured line start) pairs for slant correction
        self.next_start = 0.0         # expected start of the current line (relative to self.f)
        self.missed = 0
        self.cr = np.full(m["w"], 128.0)
        self.cb = np.full(m["w"], 128.0)
        self.started = time.strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(self.out_dir, f"sstv_{self.started}_{mode.replace(' ', '')}.png")
        self._last_save = 0
        self.on_event("log", f"SSTV picture starting – {mode} ({m['w']}x{m['h']})")

    def _predict(self, line):
        if len(self.fit) >= 6:
            ks = np.array([k for k, _ in self.fit[-60:]], float)
            ps = np.array([p for _, p in self.fit[-60:]], float)
            a, b = np.polyfit(ks, ps, 1)
            if abs(a - self.period) < self.period * 0.01:
                return a * line + b
        if self.fit:
            k, p = self.fit[-1]
            return p + (line - k) * self.period
        return line * self.period

    def _next_line(self):
        margin = max(self.sync_len, 0.004 * FSD)
        start = self._predict(self.line) - self.base_off()
        end = start + self.period + margin
        if len(self.f) < end + 2:
            return False
        # look for the sync pulse near where we expect it
        es = start + self.sync_off
        a, b = int(max(0, es - margin)), int(min(len(self.f) - self.sync_len, es + margin))
        meas = None
        if b > a:
            ind = (self.f < 1350).astype(np.float32)
            cs = np.concatenate(([0], np.cumsum(ind)))
            L = int(self.sync_len)
            win = cs[a + L: b + L] - cs[a: b]
            k = int(np.argmax(win))
            if win[k] >= 0.6 * L:
                meas = a + k - self.sync_off
        if meas is not None:
            self.fit.append((self.line, meas + self.base_off()))
            self.missed = 0
            use = meas if len(self.fit) < 6 else start   # trust the fitted line once established
            if abs(meas - start) > 3 * margin:
                use = meas
        else:
            self.missed += 1
            use = start
        self._decode_line(use)
        self.line += 1
        if self.line >= self.lines_total or self.missed > 12:
            self._finish()
            return True
        # discard samples before this line
        drop = int(max(0, use - 2 * margin))
        if drop > 0:
            self.f = self.f[drop:]
            self.base += drop
        return True

    def base_off(self):
        """self.f index 0 relative to the picture start (absolute index bookkeeping)."""
        return self.base - self._img_base

    def _seg(self, start, off, dur, w):
        a = start + off
        edges = a + dur * np.arange(w + 1) / w
        idx = np.clip(np.round(edges).astype(int), 0, len(self.f) - 1)
        cs = np.concatenate(([0.0], np.cumsum(self.f, dtype=np.float64)))
        cnt = np.maximum(1, idx[1:] - idx[:-1])
        f = (cs[idx[:-1] + cnt] - cs[idx[:-1]]) / cnt
        return np.clip((f - 1500.0) / 800.0 * 255.0, 0, 255)

    def _decode_line(self, start):
        m = MODES[self.mode]
        w = m["w"]
        vals = {kind: self._seg(start, off, dur, w) for kind, off, dur in self.seq if kind in PIX_KINDS}
        ln = self.line
        if m.get("pd"):
            for r, yk in ((2 * ln, "Y0"), (2 * ln + 1, "Y1")):
                if r < m["h"]:
                    self.img[r] = ycc_to_rgb(vals[yk], vals["RY"], vals["BY"])
            self.rows_done = min(m["h"], 2 * ln + 2)
        elif "C" in vals:            # Robot 36: chroma alternates R-Y / B-Y
            if ln % 2 == 0:
                self.cr = vals["C"]
            else:
                self.cb = vals["C"]
            self.img[ln] = ycc_to_rgb(vals["Y"], self.cr, self.cb)
            if ln % 2 == 1 and ln > 0:
                self.img[ln - 1] = ycc_to_rgb(self._prevY, self.cr, self.cb)
            self._prevY = vals["Y"]
            self.rows_done = ln + 1
        elif "RY" in vals:           # Robot 72
            self.img[ln] = ycc_to_rgb(vals["Y"], vals["RY"], vals["BY"])
            self.rows_done = ln + 1
        else:                        # Martin / Scottie: straight RGB
            self.img[ln] = np.stack([vals["R"], vals["G"], vals["B"]], -1).astype(np.uint8)
            self.rows_done = ln + 1
        if self.rows_done - self._last_save >= 16:
            self.save()

    def save(self):
        if self.rows_done < 4:
            return None
        write_png_rgb(self.path, self.img)
        self._last_save = self.rows_done
        self.on_event("image", os.path.basename(self.path))
        return self.path

    def _finish(self):
        self.save()
        self.on_event("log", f"SSTV picture finished – {self.mode}, {self.rows_done} lines")
        self.state = "idle"
        self.mode = None

