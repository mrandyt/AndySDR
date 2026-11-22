"""VDL Mode 2 (aircraft data link on 136.xxx MHz) decoder, built in.

VDL2 is 25 kHz wide D8PSK at 10.5 kbaud (31.5 kbit/s).  A burst is:
  16 symbol preamble | 25 bit header (length + FEC) | data + Reed-Solomon parity, all scrambled
The data is HDLC/AVLC frames; the ones carrying ACARS are turned into the same messages the 131 MHz ACARS
decoder makes, so they show up in the AIRCRAFT TEXT tab (marked VDL2).

Written from the published protocol (ARINC 631 / ICAO Annex 10 as implemented by the open-source dumpvdl2 by
Tomasz Lemiech, GPL).  Input is the 48 kHz complex IQ of the tuned channel.
Not yet proven on live signals - the tests use a transmitter written from the same description.
"""
import time

import numpy as np
from scipy import signal

from .acars import crc16 as acars_crc16, parse_block

FS_IN = 48000
FS = 105000
SPS = 10
SYMRATE = 10500
NSYM_PRE = 16
RS_N, RS_K, NROOTS, FCR = 255, 249, 6, 120
LFSR_IV = 0x6959
SYNC_THRESHOLD = 4.0
GRAY = [0, 1, 3, 2, 6, 7, 5, 4]                 # symbol phase step (x pi/4) -> three bits
GRAY_INV = {g: i for i, g in enumerate(GRAY)}
PRE_PHASE = np.array([0, 3, -3, 1, 1, 2, 0, 4, -3, 4, -2, 3, 1, -2, -3, 0], float) * np.pi / 4
_X = np.arange(NSYM_PRE, dtype=float) - (NSYM_PRE - 1) / 2.0
_XD = float(np.sum(_X * _X))
MAX_BITS = 0x3FFF
WAIT_S = 3.0                                    # give up on a burst that never completes
KEEP = FS * 2

_H = [0b0000000011111111111110000, 0b0011111100001111111101000, 0b1100011100110000111100100,
      0b1101101101010011001100010, 0b0110100111100101010100001]
_SYND = [
    0b0000000000000000000000000, 0b0000000000000000000000001, 0b0000000000000000000000010, 0b0100000000000000000000100,
    0b0000000000000000000000100, 0b0100000000000000000000010, 0b1000000000000000000000000, 0b0100000000000000000000000,
    0b0000000000000000000001000, 0b0010000000000000000000000, 0b0001000000000000000000000, 0b0000100000000000000000000,
    0b0000010000000000000000000, 0b1000100000000000000000000, 0b0000001000000000000000000, 0b0000000100000000000000000,
    0b0000000000000000000010000, 0b0000000010000000000000000, 0b0100000000100000000000000, 0b0000000001000000000000000,
    0b0100000001000000000000000, 0b0000000000100000000000000, 0b0000000000010000000000000, 0b1000000010000000000000000,
    0b0000000000001000000000000, 0b0000000000000100000000000, 0b0000000000000010000000000, 0b0000000000000001000000000,
    0b0000000000000000100000000, 0b0000000000000000010000000, 0b0000000000000000001000000, 0b0000000000000000000100000]


# ---------------------------------------------------------------------------------------------- GF(256) / Reed-Solomon
_EXP = [0] * 512
_LOG = [0] * 256
_v = 1
for _i in range(255):
    _EXP[_i] = _v
    _LOG[_v] = _i
    _v <<= 1
    if _v & 0x100:
        _v ^= 0x187
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _div(a, b):
    return 0 if a == 0 else _EXP[(_LOG[a] - _LOG[b]) % 255]


def _poly_eval(p, x):
    """p[0] is the constant term."""
    r = 0
    for c in reversed(p):
        r = _mul(r, x) ^ c
    return r


def _poly_mul(a, b):
    out = [0] * (len(a) + len(b) - 1)
    for i, x in enumerate(a):
        if x:
            for j, y in enumerate(b):
                out[i + j] ^= _mul(x, y)
    return out


def rs_encode(data):
    """data = 249 bytes (zero padded if the block is short). Returns the 6 parity bytes."""
    gen = [1]
    for i in range(NROOTS):
        gen = _poly_mul(gen, [_EXP[FCR + i], 1])       # (x + alpha^(fcr+i)), constant term first
    gen = gen[::-1]                                     # highest power first, gen[0] == 1
    reg = [0] * NROOTS
    for d in data:
        fb = d ^ reg[0]
        reg = reg[1:] + [0]
        if fb:
            for k in range(NROOTS):
                reg[k] ^= _mul(fb, gen[k + 1])
    return reg


def rs_decode(cw, erasures=()):
    """Fix a 255 byte codeword in place. Returns the number of bytes corrected (erasures included) or -1."""
    n = RS_N
    s = []
    for i in range(NROOTS):
        r = 0
        a = _EXP[(FCR + i) % 255]
        for c in cw:
            r = _mul(r, a) ^ c
        s.append(r)
    if not any(s):
        return 0
    ne = len(erasures)
    if ne > NROOTS:
        return -1
    gamma = [1]                                         # erasure locator, constant term first
    for pos in erasures:
        gamma = _poly_mul(gamma, [1, _EXP[(n - 1 - pos) % 255]])
    t = _poly_mul(s, gamma)[:NROOTS]                    # modified syndromes
    # Berlekamp-Massey on t[ne:]
    tt = t[ne:]
    lam, b, L, m, bb = [1], [1], 0, 1, 1
    for i in range(len(tt)):
        d = tt[i]
        for j in range(1, L + 1):
            if j < len(lam):
                d ^= _mul(lam[j], tt[i - j])
        if d == 0:
            m += 1
        elif 2 * L <= i:
            old = lam[:]
            f = _div(d, bb)
            lam = lam + [0] * max(0, len(b) + m - len(lam))
            for j, c in enumerate(b):
                lam[j + m] ^= _mul(f, c)
            L, b, bb, m = i + 1 - L, old, d, 1
        else:
            f = _div(d, bb)
            lam = lam + [0] * max(0, len(b) + m - len(lam))
            for j, c in enumerate(b):
                lam[j + m] ^= _mul(f, c)
            m += 1
    while len(lam) > 1 and lam[-1] == 0:
        lam.pop()
    if 2 * L + ne > NROOTS:
        return -1
    psi = _poly_mul(lam, gamma)
    # Chien search: position p has locator X = alpha^(n-1-p); root of psi at X^-1
    locs = []
    for p in range(n):
        x_inv = _EXP[(-(n - 1 - p)) % 255]
        if _poly_eval(psi, x_inv) == 0:
            locs.append(p)
    if len(locs) != len(psi) - 1:
        return -1
    omega = _poly_mul(s, psi)[:NROOTS]
    dpsi = [psi[i] if i % 2 == 1 else 0 for i in range(1, len(psi))]     # formal derivative (char 2)
    for p in locs:
        x = _EXP[(n - 1 - p) % 255]
        x_inv = _EXP[(-(n - 1 - p)) % 255]
        den = _poly_eval(dpsi, x_inv)
        if den == 0:
            return -1
        e = _mul(_EXP[((1 - FCR) * (n - 1 - p)) % 255], _div(_poly_eval(omega, x_inv), den))
        cw[p] ^= e
    # verify
    for i in range(NROOTS):
        r = 0
        a = _EXP[(FCR + i) % 255]
        for c in cw:
            r = _mul(r, a) ^ c
        if r:
            return -1
    return len(locs)


# ---------------------------------------------------------------------------------------------- burst framing
def _fec_octets(last_len):
    if last_len < 3:
        return 0
    if last_len < 31:
        return 2
    if last_len < 68:
        return 4
    return 6


def _par(v):
    return bin(v).count("1") & 1


def header_fix(word):
    """25 bit header word -> (corrected word, syndrome)."""
    syn = 0
    for i, h in enumerate(_H):
        syn |= _par(word & h) << (4 - i)
    return word ^ _SYND[syn], syn


def _rev(v, n):
    return int(format(v, "0%db" % n)[::-1], 2)


_KEYSTREAM = None


def _keystream(n):
    global _KEYSTREAM
    if _KEYSTREAM is None:
        lf = LFSR_IV
        out = np.zeros(32767, np.uint8)
        for i in range(32767):
            bit = (lf ^ (lf >> 14)) & 1
            lf = (lf >> 1) | (bit << 14)
            out[i] = bit
        _KEYSTREAM = out
    reps = -(-n // 32767)
    return np.tile(_KEYSTREAM, reps)[:n]


def _interleave_map(nbytes, rows, fillwidth, offset):
    """Where each input byte lands in the rows x 255 table (the deinterleaver of the receiver, as indices)."""
    if rows == 0 or fillwidth == 0:
        return None
    last = nbytes % fillwidth or fillwidth
    if fillwidth + offset > RS_N or nbytes > rows * fillwidth:
        return None
    if rows > 1 and nbytes - last < (rows - 1) * fillwidth:
        return None
    pos = []
    row, col = 0, offset
    last += offset
    for _ in range(nbytes):
        if row == rows - 1 and col >= last:
            row = 0
            col += 1
        pos.append((row, col))
        row += 1
        if row == rows:
            row = 0
            col += 1
    return pos


def _bits_to_bytes_lsb(bits):
    n = len(bits) // 8
    b = np.asarray(bits[:n * 8], np.uint8).reshape(n, 8)
    return (b * (1 << np.arange(8, dtype=np.uint8))).sum(1).astype(np.uint8)


def _bytes_to_bits_lsb(data):
    a = np.frombuffer(bytes(data), np.uint8)
    return ((a[:, None] >> np.arange(8, dtype=np.uint8)) & 1).astype(np.uint8).reshape(-1)


def read_header(bits):
    """bits: first 25 descrambled bits. Returns (datalen_bits, syndrome) or None."""
    word = 0
    for b in bits[:25]:
        word = (word << 1) | int(b)
    word &= (1 << 22) - 1                      # the reserved symbol is always zero
    word, syn = header_fix(word)
    if word & ~((1 << 22) - 1):
        return None
    datalen = _rev((word >> 5) & 0x1FFFF, 17)
    if datalen > (MAX_BITS if syn == 0 else 0x1FFF):
        return None
    return datalen, syn


def layout(datalen):
    octets = (datalen + 7) // 8
    blocks = octets // RS_K
    last = octets % RS_K
    fec = blocks * NROOTS
    if last:
        blocks += 1
    fec += _fec_octets(last)
    if last == 0:
        last = RS_K
    return octets, blocks, last, fec


def decode_payload(bits, datalen):
    """bits: all descrambled bits after the header. Returns the list of AVLC frames (bytes) or None."""
    octets, blocks, last, fec = layout(datalen)
    if fec == 0 or len(bits) < 8 * (octets + fec):
        return None
    data = _bits_to_bytes_lsb(bits[:8 * octets])
    par = _bits_to_bytes_lsb(bits[8 * octets:8 * (octets + fec)])
    table = np.zeros((blocks, RS_N), np.uint8)
    m = _interleave_map(octets, blocks, RS_K, 0)
    if m is None:
        return None
    for v, (r, c) in zip(data, m):
        table[r, c] = v
    fec_rows = blocks - (1 if _fec_octets(last) == 0 else 0)
    m = _interleave_map(fec, fec_rows, NROOTS, RS_K)
    if m is None:
        return None
    for v, (r, c) in zip(par, m):
        table[r, c] = v
    out = bytearray()
    for r in range(blocks):
        nf = _fec_octets(last) if r == blocks - 1 else NROOTS
        row = [int(v) for v in table[r]]
        if nf == 0:
            pass
        else:
            er = list(range(RS_K + nf, RS_N)) if nf < NROOTS else []
            if rs_decode(row, er) < 0:
                return None
        out += bytes(row[:RS_K if r < blocks - 1 else last])
    stream = _bytes_to_bits_lsb(out)[:datalen]
    return _unstuff(stream)


def _unstuff(bits):
    """Split the bit stream into HDLC frames (flag 0x7E, bit stuffing). Returns a list of byte strings or None."""
    frames = []
    cur = []
    ones = 0
    for b in bits:
        if b == 0 and ones == 5:                    # stuffed zero
            ones = 0
            continue
        if b:
            ones += 1
            if ones > 6:
                return None
            cur.append(1)
            continue
        cur.append(0)
        if ones == 6:                               # flag
            ones = 0
            if len(cur) == 8:                       # opening flag
                cur = []
                continue
            if len(cur) < 8:
                return None
            body = cur[:-8]
            if len(body) % 8:
                return None
            frames.append(bytes(_bits_to_bytes_lsb(body)))
            cur = []
            continue
        ones = 0
    return frames if frames else None


def _stuff(data):
    bits = []
    ones = 0
    for b in _bytes_to_bits_lsb(data):
        bits.append(int(b))
        if b:
            ones += 1
            if ones == 5:
                bits.append(0)
                ones = 0
        else:
            ones = 0
    return bits


FLAG = [0, 1, 1, 1, 1, 1, 1, 0]


# ---------------------------------------------------------------------------------------------- AVLC
_CRC = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x8408 if _c & 1 else _c >> 1
    _CRC.append(_c)


def fcs16(data, crc=0xFFFF):
    for b in data:
        crc = (crc >> 8) ^ _CRC[(crc ^ b) & 0xFF]
    return crc


def parse_addr(b):
    v = (b[0] >> 1) | (b[1] << 6) | (b[2] << 13) | ((b[3] & 0xFE) << 20)
    v = _rev(v & 0xFFFFFFF, 28)
    return {"addr": v & 0xFFFFFF, "type": (v >> 24) & 7, "status": (v >> 27) & 1}


ADDR_TYPES = {1: "aircraft", 4: "ground", 5: "ground", 7: "all"}


def parse_avlc(frame):
    """Returns a dict or None. 'acars' is set when the frame carries an ACARS message."""
    if len(frame) < 11 or fcs16(frame) != 0xF0B8:
        return None
    body = frame[:-2]
    dst, src = parse_addr(body[0:4]), parse_addr(body[4:8])
    lcf = body[8]
    info = {"src": src, "dst": dst, "kind": "I" if lcf & 1 == 0 else ("S" if lcf & 3 == 1 else "U")}
    payload = body[9:]
    if info["kind"] == "I" and len(payload) > 3 and payload[0] == 0xFF and payload[1] == 0xFF and payload[2] == 0x01:
        a = payload[3:]
        if len(a) >= 16 and a[-1] == 0x7F and acars_crc16(a[:-1], 0) == 0:
            m = parse_block(a[:-3])
            if m:
                info["acars"] = m
    return info


def pack_addr(a, typ, status, last=False):
    """The 4 address bytes of an AVLC frame (7 bits per byte, bit 0 of the last byte marks the end)."""
    want = _rev((a & 0xFFFFFF) | (typ << 24) | (status << 27), 28)
    b = [((want >> (7 * i)) & 0x7F) << 1 for i in range(4)]
    if last:
        b[3] |= 1
    return bytes(b)


def build_avlc(src_icao, dst_gs, acars_bytes=None, air=True, lcf=0x00):
    """Test helper: a complete AVLC frame (with FCS) carrying an ACARS block (bytes from mode to ETX, no CRC)."""
    out = bytearray()
    out += pack_addr(dst_gs, 4, 0 if air else 1)
    out += pack_addr(src_icao, 1 if air else 4, 0, last=True)
    out.append(lcf)
    if acars_bytes is not None:
        crc = acars_crc16(acars_bytes, 0)
        out += b"\xff\xff\x01" + acars_bytes + bytes([crc & 0xFF, crc >> 8, 0x7F])
    fc = fcs16(out) ^ 0xFFFF
    out += bytes([fc & 0xFF, fc >> 8])
    return bytes(out)


# ---------------------------------------------------------------------------------------------- transmit side (tests)
def build_burst_bits(frames):
    """HDLC + RS + interleave + scramble: the bit stream after the preamble (header, data, parity)."""
    st = []
    for i, f in enumerate(frames):
        if i == 0:
            st += FLAG
        st += _stuff(f) + FLAG
    datalen = len(st)
    octets, blocks, last, fec = layout(datalen)
    pad = st + [0] * (octets * 8 - datalen)
    raw = bytes(_bits_to_bytes_lsb(pad))
    table = np.zeros((blocks, RS_N), np.uint8)
    for r in range(blocks):                         # the message is cut into 249 byte rows, each gets parity
        chunk = raw[r * RS_K:(r + 1) * RS_K]
        table[r, :len(chunk)] = np.frombuffer(chunk, np.uint8)
        table[r, RS_K:] = rs_encode([int(x) for x in table[r, :RS_K]])
    m = _interleave_map(octets, blocks, RS_K, 0)    # ... and is sent column by column
    data = bytes(int(table[r, c]) for (r, c) in m)
    fec_rows = blocks - (1 if _fec_octets(last) == 0 else 0)
    m2 = _interleave_map(fec, fec_rows, NROOTS, RS_K)
    parity = bytes(int(table[r, c]) for (r, c) in m2)
    body = list(_bytes_to_bits_lsb(data)) + list(_bytes_to_bits_lsb(bytes(parity)))
    tr = _rev(datalen, 17)
    word = tr << 5
    fec5 = 0
    for i, h in enumerate(_H):
        fec5 |= _par(word & h) << (4 - i)
    word |= fec5
    hdr = [(word >> (24 - i)) & 1 for i in range(25)]
    bits = np.array(hdr + body, np.uint8)
    bits ^= _keystream(len(bits))
    return bits


def burst_symbol_phases(bits):
    """Cumulative symbol phases (radians) for preamble + the given bit stream."""
    n = (len(bits) + 2) // 3
    b = np.concatenate((bits, np.zeros(n * 3 - len(bits), np.uint8))).reshape(n, 3)
    vals = b[:, 0] * 4 + b[:, 1] * 2 + b[:, 2]
    steps = np.array([GRAY_INV[int(v)] for v in vals], float) * np.pi / 4
    return np.concatenate((PRE_PHASE, PRE_PHASE[-1] + np.cumsum(steps)))


def _rrc(beta=0.6, span=8, sps=SPS):
    n = span * sps
    t = (np.arange(-n // 2, n // 2 + 1)) / sps
    h = np.zeros(len(t))
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1 - beta + 4 * beta / np.pi
        elif abs(abs(x) - 1 / (4 * beta)) < 1e-9:
            h[i] = beta / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * beta)) + (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta)))
        else:
            h[i] = (np.sin(np.pi * x * (1 - beta)) + 4 * beta * x * np.cos(np.pi * x * (1 + beta))) / (np.pi * x * (1 - (4 * beta * x) ** 2))
    return h / np.sqrt(np.sum(h ** 2))


def modulate(frames, fs=FS_IN, lead=0.05, tail=0.05, noise=0.0, cfo=0.0, amp=0.5, ppm=0.0, seed=3, phase0=0.7):
    """Test transmitter: returns complex IQ at 48 kHz for one burst."""
    ph = burst_symbol_phases(build_burst_bits(frames)) + phase0
    sym = np.exp(1j * ph)
    up = np.zeros(len(sym) * SPS, complex)
    up[::SPS] = sym
    sh = signal.lfilter(_rrc(), 1.0, np.concatenate((up, np.zeros(SPS * 8))))
    sh = np.concatenate((np.zeros(int(lead * FS)), sh, np.zeros(int(tail * FS))))
    y = signal.resample_poly(sh, 16, 35 * (1 + 0.0))
    if ppm:
        n = np.arange(len(y))
        y = np.interp(n * (1 + ppm * 1e-6), n, y.real) + 1j * np.interp(n * (1 + ppm * 1e-6), n, y.imag)
    n = np.arange(len(y))
    y = amp * y / np.max(np.abs(y)) * np.exp(2j * np.pi * cfo * n / fs)
    if noise:
        rng = np.random.default_rng(seed)
        y = y + noise * (rng.standard_normal(len(y)) + 1j * rng.standard_normal(len(y))) / np.sqrt(2)
    return y.astype(np.complex64)


# ---------------------------------------------------------------------------------------------- receiver
class Vdl2Decoder:
    def __init__(self, on_message=None, on_status=None):
        self.on_message, self.on_status = on_message, on_status
        self.lp = signal.firwin(81, 10000, fs=FS_IN)
        self.rrc = _rrc()                  # matched filter for the transmitter's pulse shape
        self.zi2 = np.zeros(len(self.rrc) - 1, complex)
        self.zi = np.zeros(len(self.lp) - 1, complex)
        self.pend = np.zeros(32, complex)
        self.x = np.zeros(0, np.complex64)
        self.ph = np.zeros(0, np.float32)
        self.scan_pos = 0
        self.bursts = 0                    # headers that checked out
        self.frames = 0                    # AVLC frames with a good checksum
        self.acars = 0
        self.bad = 0
        self.level = 0.0
        self.last_status = 0.0

    # ---- input
    def process(self, iq):
        x = np.asarray(iq, np.complex64)
        if not len(x):
            return
        self.level = 0.9 * self.level + 0.1 * float(np.mean(np.abs(x)))
        y, self.zi = signal.lfilter(self.lp, 1.0, x, zi=self.zi)
        self.pend = np.concatenate((self.pend, y))
        m = 48
        while len(self.pend) >= 16 * (m + 4):
            seg = self.pend[:16 * (m + 4)]
            r = signal.resample_poly(seg, 35, 16)[70:(m + 2) * 35]
            r, self.zi2 = signal.lfilter(self.rrc, 1.0, r, zi=self.zi2)
            self.pend = self.pend[16 * m:]
            self.x = np.concatenate((self.x, r.astype(np.complex64)))
            self.ph = np.concatenate((self.ph, np.angle(r).astype(np.float32)))
        self._scan()
        if len(self.x) > KEEP and self.scan_pos > KEEP // 2:
            cut = self.scan_pos - 2000
            self.x, self.ph = self.x[cut:], self.ph[cut:]
            self.scan_pos -= cut
        now = time.time()
        if self.on_status and now - self.last_status > 1.0:
            self.last_status = now
            self.on_status({"bursts": self.bursts, "frames": self.frames, "acars": self.acars, "bad": self.bad,
                            "level": round(float(self.level), 4)})

    # ---- preamble search
    def _metric(self, n):
        n = np.asarray(n)
        idx = n[:, None] + SPS * np.arange(NSYM_PRE)[None, :]
        err = np.unwrap(self.ph[idx] - PRE_PHASE[None, :], axis=1)
        err = err - err.mean(axis=1, keepdims=True)
        slope = (err * _X[None, :]).sum(1) / _XD
        res = err - slope[:, None] * _X[None, :]
        return (res * res).sum(1), slope

    def _scan(self):
        end = len(self.ph) - SPS * NSYM_PRE - 8
        start = self.scan_pos
        if end - start < 16:
            return
        a = max(0, start - 6)
        pe, _ = self._metric(np.arange(a, end))
        pos = start
        for c in np.nonzero(pe < SYNC_THRESHOLD)[0]:
            n = a + int(c)
            if n < pos:
                continue
            if n >= end - 6:
                break                               # not enough neighbours yet - look again with more samples
            if pe[c] > pe[max(0, c - 6):c + 7].min():
                continue
            r = self._try(n)
            if r == "wait":
                self.scan_pos = n
                if len(self.ph) - n > WAIT_S * FS:  # never completes - drop it
                    self.scan_pos = n + 80
                return
            pos = n + (r or 80)
        self.scan_pos = max(pos, end - 6)

    # ---- one burst
    def _symbols(self, n, slope, count):
        pos = n + SPS * (np.arange(NSYM_PRE - 1, NSYM_PRE - 1 + count + 1))
        p = self.ph[pos].astype(float)
        d = np.diff(p) - slope
        d = np.mod(d, 2 * np.pi)
        idx = np.rint(d / (np.pi / 4)).astype(int) % 8
        return idx

    def _try(self, n0):
        """Try to decode a burst whose preamble starts near sample n0. Returns 'wait', 0/None, or the symbols used."""
        need_hdr = SPS * (NSYM_PRE + 10) + 4
        if n0 + need_hdr + 4 >= len(self.ph):
            return "wait"
        for off in (0, -1, 1, -2, 2):
            n = n0 + off
            if n < 1:
                continue
            _, slope = self._metric(np.array([n]))
            slope = float(slope[0])
            idx = self._symbols(n, slope, 9)
            bits = np.array([[(GRAY[i] >> 2) & 1, (GRAY[i] >> 1) & 1, GRAY[i] & 1] for i in idx], np.uint8).reshape(-1)
            ks = _keystream(len(bits))
            h = read_header((bits ^ ks)[:25])
            if h is None:
                continue
            datalen, syn = h
            octets, blocks, last, fec = layout(datalen)
            if fec == 0:
                continue
            total_bits = 25 + 8 * (octets + fec)
            nsym = -(-total_bits // 3)
            if n + SPS * (NSYM_PRE + nsym) + 2 >= len(self.ph):
                return "wait"
            idx = self._symbols(n, slope, nsym)
            bits = np.array([[(GRAY[i] >> 2) & 1, (GRAY[i] >> 1) & 1, GRAY[i] & 1] for i in idx], np.uint8).reshape(-1)
            bits = bits ^ _keystream(len(bits))
            frames = decode_payload(bits[25:], datalen)
            if frames is None:
                continue
            self.bursts += 1
            for f in frames:
                self._frame(f)
            return SPS * (NSYM_PRE + nsym)
        self.bad += 1
        return 0

    def _frame(self, f):
        a = parse_avlc(f)
        if a is None:
            return
        self.frames += 1
        m = a.get("acars")
        if m is None:
            return
        self.acars += 1
        up = a["src"]["type"] in (4, 5)
        m = dict(m, via="VDL2", icao="%06X" % (a["dst"]["addr"] if up else a["src"]["addr"]),
                 gs="%06X" % (a["src"]["addr"] if up else a["dst"]["addr"]))
        if self.on_message:
            self.on_message(m)
