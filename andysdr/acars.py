"""ACARS decoder (aircraft text messages on 131.xxx MHz, AM, 2400 bit/s MSK).

Input is the 48 kHz AM-demodulated audio of one channel. The audio is brought down to 12 kHz, shifted so the two
tones (1200 / 2400 Hz) sit at -600 / +600 Hz, and the tone of every bit slot is found from the phase step.
A 2400 Hz slot means "the bit stays the same", a 1200 Hz slot means "the bit flips", so adding the flips up
gives the bit stream. Blocks are found by their SYN SYN SOH start, checked with the CRC and the odd parity
of every character (one or two damaged characters are repaired).

Five bit-clock phases (5 samples per bit) are tried at once, which copes with any timing and a slow clock drift.
"""
import collections
import time

import numpy as np
from scipy import signal

FS_IN = 48000
FS = 12000
SPB = 5                      # samples per bit at 12 kHz
SYN, SOH, STX, ETX, ETB, DEL = 0x16, 0x01, 0x02, 0x83, 0x97, 0x7F
KEEP_S = 3.0                 # how much audio is kept for block search
SCAN_EVERY_S = 0.3

_CRC_TAB = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x8408 if _c & 1 else _c >> 1
    _CRC_TAB.append(_c)


def crc16(data, crc=0):
    for b in data:
        crc = (crc >> 8) ^ _CRC_TAB[(crc ^ b) & 0xFF]
    return crc


def _par_ok(b):
    return bin(b).count("1") & 1 == 1          # odd parity (bit 7 is the parity bit)


def _bits_of(v):
    return [(v >> i) & 1 for i in range(8)]    # least significant bit first


_PAT = np.array(_bits_of(SYN) * 2 + _bits_of(SOH), np.int8)
_LABELS = {
    "H1": "Message to/from terminal", "5Z": "Airline designated", "80": "Terminal weather",
    "Q0": "Link test", "_d": "Demand mode", "SA": "Media advisory", "10": "Departure/delay",
    "12": "Position report", "13": "Position/OOOI", "14": "Position report", "15": "Position report",
    "16": "Position report", "20": "Delayed/Position", "21": "Position report", "22": "Position report",
    "23": "Position report", "24": "Position report", "30": "Position report", "33": "Position report",
    "44": "Position/Weather", "4A": "Position/ATIS", "4M": "Position", "5U": "Weather request",
    "5Y": "Takeoff/OOOI", "B6": "ATC request", "A9": "ATC request", "AA": "ATC response", "A0": "ATC",
    "C1": "Cabin message", "B9": "ATC", "RA": "Clearance", "SQ": "Squitter (ground station)",
    "QA": "OOOI Out", "QB": "OOOI Off", "QC": "OOOI On", "QD": "OOOI In", "QE": "OOOI", "QF": "OOOI",
    "QG": "OOOI", "QH": "OOOI Out", "QK": "OOOI", "QM": "OOOI", "QN": "OOOI", "QP": "OOOI", "QQ": "OOOI",
    "QR": "OOOI", "QS": "OOOI", "QT": "OOOI", "H2": "Wind report", "H3": "Wind report",
    "MA": "Media", "MB": "Media", "5V": "Airline", "57": "Fuel", "58": "Fuel", "83": "Weather",
}


def label_name(label):
    return _LABELS.get(label, "")


def parse_block(raw):
    """raw = bytes from after SOH up to and including ETX/ETB (parity still on). Returns a dict."""
    b = bytes(x & 0x7F for x in raw)
    if len(b) < 13:
        return None
    mode = chr(b[0])
    reg = b[1:8].decode("latin1").lstrip(".").strip()
    ack = b[8]
    ack_s = "NAK" if ack == 0x15 else chr(ack)
    label = chr(b[9]) + ("d" if b[10] == 0x7F else chr(b[10]))
    bid = chr(b[11])
    end = b[-1]
    body = b[13:-1] if b[12] == STX else b[12:-1]
    flight, msgno = "", ""
    down = bid.isdigit()
    if down and len(body) >= 10:
        msgno = body[:4].decode("latin1")
        flight = body[4:10].decode("latin1").strip()
        body = body[10:]
    text = "".join(chr(c) if 32 <= c < 127 or c in (10, 13) else " " for c in body)
    return {"mode": mode, "reg": reg, "ack": ack_s, "label": label, "bid": bid, "down": down,
            "msgno": msgno, "flight": flight, "text": text.replace("\r\n", "\n").replace("\r", "\n").strip("\n"),
            "more": end == ETB}


class AcarsDecoder:
    def __init__(self, on_message=None, on_status=None):
        self.on_message, self.on_status = on_message, on_status
        self.lp_in = signal.firwin(97, 4600, fs=FS_IN).astype(np.float32)
        self.zi_in = np.zeros(len(self.lp_in) - 1, np.float32)
        self.pre = np.zeros(0, np.float32)
        self.lp = signal.firwin(41, 1300, fs=FS).astype(np.float32)
        self.zi = np.zeros(len(self.lp) - 1, np.complex64)
        self.n_mix = 0
        self.z = np.zeros(0, np.complex64)        # filtered baseband, newest at the end
        self.base = 0                              # absolute sample index of z[0]
        self.new = 0
        self.seen = collections.deque(maxlen=64)   # (abs start sample, crc) of blocks already reported
        self.count = 0
        self.bad = 0
        self.level = 0.0
        self.last_status = 0.0

    # ------------------------------------------------------------------ audio in
    def process(self, audio):
        x = np.asarray(audio, np.float32)
        if not len(x):
            return
        y, self.zi_in = signal.lfilter(self.lp_in, 1.0, x, zi=self.zi_in)
        y = np.concatenate((self.pre, y.astype(np.float32)))
        k = (len(y) // 4) * 4
        self.pre = y[k:]
        y = y[:k:4]                                # 48 kHz -> 12 kHz
        if not len(y):
            return
        y = y - float(np.mean(y))                  # take the AM carrier (DC) out
        n = np.arange(self.n_mix, self.n_mix + len(y))
        self.n_mix += len(y)
        mix = np.exp(-2j * np.pi * 1800.0 * n / FS).astype(np.complex64)
        z, self.zi = signal.lfilter(self.lp, 1.0, y * mix, zi=self.zi)
        self.level = 0.9 * self.level + 0.1 * float(np.mean(np.abs(z)))
        self.z = np.concatenate((self.z, z.astype(np.complex64)))
        keep = int(KEEP_S * FS)
        if len(self.z) > keep:
            cut = len(self.z) - keep
            self.z = self.z[cut:]
            self.base += cut
        self.new += len(y)
        if self.new >= SCAN_EVERY_S * FS:
            self.new = 0
            self._scan()
        now = time.time()
        if self.on_status and now - self.last_status > 1.0:
            self.last_status = now
            self.on_status({"count": self.count, "bad": self.bad, "level": round(float(self.level), 4)})

    # ------------------------------------------------------------------ block search
    def _scan(self):
        z = self.z
        if len(z) < 2 * SPB * 40 or self.level < 1e-4:
            return
        u = z[1:] * np.conj(z[:-1])
        for o in range(SPB):
            uu = u[o:]
            m = (len(uu) // SPB) * SPB
            if m < SPB * 60:
                continue
            s = uu[:m].reshape(-1, SPB).sum(axis=1).imag > 0     # True = 2400 Hz slot (bit stays)
            flips = (~s).astype(np.int8)
            bits = np.concatenate(([0], np.cumsum(flips) & 1)).astype(np.int8)[:-1]
            for pol in (0, 1):
                b = bits ^ pol
                if len(b) < 24:
                    continue
                win = np.lib.stride_tricks.sliding_window_view(b, 24)
                for h in np.where((win == _PAT).all(axis=1))[0]:
                    self._try_block(b, int(h) + 24, self.base + o + int(h) * SPB)

    def _try_block(self, b, p, start_abs):
        raw = []
        n = len(b)
        while p + 8 <= n and len(raw) < 260:
            v = 0
            for i in range(8):
                v |= int(b[p + i]) << i
            raw.append(v)
            p += 8
            if v in (ETX, ETB):
                break
        else:
            return
        if raw[-1] not in (ETX, ETB) or p + 16 > n:
            return
        crc_bytes = [sum(int(b[p + i + 8 * j]) << i for i in range(8)) for j in range(2)]
        data = self._repair(raw, crc_bytes)
        if data is None:
            self.bad += 1
            return
        key = crc16(data)
        for st, k in self.seen:
            if k == key and abs(st - start_abs) < 2 * FS:
                return
        self.seen.append((start_abs, key))
        msg = parse_block(data)
        if msg is None:
            return
        msg["t"] = time.time()
        self.count += 1
        if self.on_message:
            self.on_message(msg)

    @staticmethod
    def _repair(raw, crc_bytes):
        """Return the characters (parity removed later) when the block is good or fixable, else None."""
        data = list(raw)
        if crc16(data + crc_bytes) == 0 and all(_par_ok(c) for c in data):
            return data
        bad = [i for i, c in enumerate(data) if not _par_ok(c)]
        if len(bad) > 2:
            return None
        if len(bad) == 1:
            for i in range(8):
                d2 = list(data)
                d2[bad[0]] ^= 1 << i
                if crc16(d2 + crc_bytes) == 0 and all(_par_ok(c) for c in d2):
                    return d2
        elif len(bad) == 2:
            for i in range(8):
                for j in range(8):
                    d2 = list(data)
                    d2[bad[0]] ^= 1 << i
                    d2[bad[1]] ^= 1 << j
                    if crc16(d2 + crc_bytes) == 0 and all(_par_ok(c) for c in d2):
                        return d2
        return None


# ---------------------------------------------------------------------- modulator (used by the tests)
def build_block(mode="2", reg="G-ABCD", label="H1", bid="3", text="HELLO", ack="!", flight="BA0123", msgno="M01A"):
    """Make the characters of an ACARS block (after SOH) with parity, ETX and the CRC bytes."""
    def pc(c):
        c &= 0x7F
        return c if _par_ok(c) else c | 0x80
    body = list(mode.encode()) + list(reg.rjust(7, ".").encode()) + [0x15 if ack == "!" else ord(ack)]
    body += list(label.encode()) + [ord(bid), STX]
    if bid.isdigit():
        body += list((msgno + flight.ljust(6)).encode())
    body += list(text.encode())
    chars = [pc(c) for c in body] + [ETX]
    crc = crc16(chars)
    return chars, [crc & 0xFF, crc >> 8]


def modulate(chars, crc, fs=FS_IN, lead=0.3, noise=0.0, amp=0.4, drift=0.0, seed=1):
    """AM audio of one ACARS block: carrier level + MSK tones. A bit that flips = 1200 Hz, same = 2400 Hz."""
    bits = [1] * 120                                       # pre-key (steady 2400 Hz)
    bits += [0] * 1                                         # a flip into bit sync
    for v in (0x2B | 0x80, 0x2A, SYN, SYN, SOH):
        bits += _bits_of(v)
    for v in chars + crc + [DEL]:
        bits += _bits_of(v)
    bits += [1] * 8
    spb = fs / 2400.0 * (1 + drift)
    phase, out, prev = 0.0, [], bits[0]
    for b in bits[1:]:
        f = 2400.0 if b == prev else 1200.0
        prev = b
        n = int(round(spb))
        t = (np.arange(n)) / fs
        out.append(np.sin(phase + 2 * np.pi * f * t))
        phase += 2 * np.pi * f * n / fs
    sig = np.concatenate(out)
    audio = np.concatenate((np.zeros(int(lead * fs)), 0.5 + amp * sig, np.zeros(int(0.4 * fs))))
    if noise:
        audio += np.random.default_rng(seed).normal(0, noise, len(audio))
    return (audio - 0.5).astype(np.float32)
