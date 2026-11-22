"""Radiosonde (weather balloon) decoder: Vaisala RS41, the sonde the UK Met Office launches (about 400 - 406 MHz).

Input is the 48 kHz FM-discriminator signal of the sonde channel (GFSK, 4800 bit/s, so 10 samples per bit).
The 64-bit frame header is found at ten sample phases at once, then a small timing loop follows the bit clock
through the 320-byte frame. The bytes are un-scrambled with the 64-byte mask, every block is checked with its
CRC-16, and the position (ECEF), speed, GPS time, serial number and frame number are read out.
"""
import math
import time

import numpy as np

SPB = 10                                   # samples per bit (48 kHz / 4800)
HEADER = bytes([0x10, 0xB6, 0xCA, 0x11, 0x22, 0x96, 0x12, 0xF8])
FRAME_LEN = 320
MASK = bytes([
    0x96, 0x83, 0x3E, 0x51, 0xB1, 0x49, 0x08, 0x98, 0x32, 0x05, 0x59, 0x0E, 0xF9, 0x44, 0xC6, 0x26,
    0x21, 0x60, 0xC2, 0xEA, 0x79, 0x5D, 0x6D, 0xA1, 0x54, 0x69, 0x47, 0x0C, 0xDC, 0xE8, 0x5C, 0xF1,
    0xF7, 0x76, 0x82, 0x7F, 0x07, 0x99, 0xA2, 0x2C, 0x93, 0x7C, 0x30, 0x63, 0xF5, 0x10, 0x2E, 0x61,
    0xD0, 0xBC, 0xB4, 0xB6, 0x06, 0xAA, 0xF4, 0x23, 0x78, 0x6E, 0x3B, 0xAE, 0xBF, 0x7B, 0x4C, 0xC1])
KEEP_S = 2.4
SCAN_EVERY_S = 0.5


def _bits_of(v):
    return [(v >> i) & 1 for i in range(8)]       # least significant bit first


_HDR_BITS = np.array(sum((_bits_of(b) for b in HEADER), []), np.int8)

_CRC = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) & 0xFFFF if _c & 0x8000 else (_c << 1) & 0xFFFF
    _CRC.append(_c)


def crc16(data):
    c = 0xFFFF
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _CRC[((c >> 8) ^ b) & 0xFF]
    return c


def ecef_to_geodetic(x, y, z):
    a, b = 6378137.0, 6356752.31424518
    e2 = (a * a - b * b) / (a * a)
    ee2 = (a * a - b * b) / (b * b)
    lam = math.atan2(y, x)
    p = math.hypot(x, y)
    t = math.atan2(z * a, p * b)
    phi = math.atan2(z + ee2 * b * math.sin(t) ** 3, p - e2 * a * math.cos(t) ** 3)
    n = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    return math.degrees(phi), math.degrees(lam), p / math.cos(phi) - n


def parse_frame(frame):
    """frame = 320 bytes incl. header (still scrambled). Returns a dict of what was read, or None."""
    f = bytes(frame[i] ^ MASK[i % 64] for i in range(len(frame)))
    out = {}
    pos = 0x39
    while pos + 4 <= len(f):
        bid, ln = f[pos], f[pos + 1]
        end = pos + 2 + ln
        if end + 2 > len(f) or bid not in (0x79, 0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x76, 0x78, 0x7F):
            break
        if crc16(f[pos + 2:end]) == f[end] | (f[end + 1] << 8):
            out[bid] = f[pos + 2:end]
        pos = end + 2
    st, gps, nav = out.get(0x79), out.get(0x7C), out.get(0x7B)
    if st is None or nav is None or len(nav) < 21 or len(st) < 10:
        return None
    sid = st[2:10].decode("latin1")
    if not sid.isprintable():
        return None
    r = {"id": sid, "frame": st[0] | (st[1] << 8)}
    x, y, z = (int.from_bytes(nav[4 * i:4 * i + 4], "little", signed=True) / 100.0 for i in range(3))
    lat, lon, alt = ecef_to_geodetic(x, y, z)
    if not -1000 < alt < 80000:
        return None
    vx, vy, vz = (int.from_bytes(nav[12 + 2 * i:14 + 2 * i], "little", signed=True) / 100.0 for i in range(3))
    phi, lam = math.radians(lat), math.radians(lon)
    vn = -vx * math.sin(phi) * math.cos(lam) - vy * math.sin(phi) * math.sin(lam) + vz * math.cos(phi)
    ve = -vx * math.sin(lam) + vy * math.cos(lam)
    vu = vx * math.cos(phi) * math.cos(lam) + vy * math.cos(phi) * math.sin(lam) + vz * math.sin(phi)
    r.update(lat=lat, lon=lon, alt=alt, vh=math.hypot(vn, ve), vu=vu,
             heading=(math.degrees(math.atan2(ve, vn)) + 360) % 360, sats=nav[18])
    if gps is not None and len(gps) >= 6:
        week = gps[0] | (gps[1] << 8)
        tow = int.from_bytes(gps[2:6], "little") / 1000.0
        r["gps_week"], r["gps_tow"] = week, tow
        r["time"] = _gps_time(week, tow)
    r["type"] = "RS41"
    return r


def _gps_time(week, tow):
    """UTC 'YYYY-MM-DD HH:MM:SS' from a GPS week and seconds of week (18 s leap seconds, valid since 2017)."""
    t = 315964800 + week * 604800 + tow - 18
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))


def bearing_distance(lat1, lon1, lat2, lon2):
    """Distance (km) and bearing (degrees) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * 6371.0 * math.asin(min(1, math.sqrt(a)))
    brg = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                  math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return d, (brg + 360) % 360


class RadiosondeDecoder:
    def __init__(self, on_sonde=None, on_status=None):
        self.on_sonde, self.on_status = on_sonde, on_status
        self.x = np.zeros(0, np.float32)
        self.base = 0
        self.new = 0
        self.done = []                      # absolute sample positions of frames already read
        self.frames = 0
        self.bad = 0
        self.level = 0.0
        self.last_status = 0.0

    def process(self, disc):
        x = np.asarray(disc, np.float32)
        if not len(x):
            return
        self.x = np.concatenate((self.x, x))
        keep = int(KEEP_S * 48000)
        if len(self.x) > keep:
            cut = len(self.x) - keep
            self.x = self.x[cut:]
            self.base += cut
        self.new += len(x)
        if self.new >= SCAN_EVERY_S * 48000:
            self.new = 0
            self._scan()
        now = time.time()
        if self.on_status and now - self.last_status > 1.0:
            self.last_status = now
            self.on_status({"frames": self.frames, "bad": self.bad})

    # ------------------------------------------------------------------
    def _scan(self):
        x = self.x - float(np.median(self.x))               # the tuning error is just a DC offset
        need = (64 + FRAME_LEN * 8 - 64) * SPB + 20
        if len(x) < need:
            return
        self.done = [d for d in self.done if d > self.base - 48000]
        for o in range(SPB):
            m = ((len(x) - o) // SPB) * SPB
            s = (x[o:o + m].reshape(-1, SPB).sum(axis=1) > 0).astype(np.int8)
            if len(s) < 70:
                continue
            win = np.lib.stride_tricks.sliding_window_view(s, 64)
            for pol in (0, 1):
                target = _HDR_BITS ^ pol
                for h in np.where((win == target).all(axis=1))[0]:
                    start = o + int(h) * SPB
                    absn = self.base + start
                    if any(abs(absn - d) < 4800 for d in self.done):
                        continue
                    if start + FRAME_LEN * 8 * SPB + 20 > len(x):
                        continue                              # not all of it has arrived yet
                    rec = self._read_frame(x, start, pol)
                    if rec is None:
                        self.bad += 1
                        continue
                    self.done.append(absn)
                    self.frames += 1
                    rec["t"] = time.time()
                    if self.on_sonde:
                        self.on_sonde(rec)

    @staticmethod
    def _read_frame(x, start, pol):
        n = len(x)
        T = float(SPB)
        pos = start + 64 * SPB + T / 2                         # centre of the first bit after the header
        bits = []
        prev = None
        amp = float(np.mean(np.abs(x[start:start + 64 * SPB]))) + 1e-9

        def at(p):
            i = int(p)
            if i < 0 or i + 1 >= n:
                return 0.0
            fr = p - i
            return x[i] * (1 - fr) + x[i + 1] * fr
        for _ in range((FRAME_LEN - 8) * 8):
            v = at(pos) / amp
            bits.append(1 if v > 0 else 0)
            if prev is not None:                               # Gardner timing error: mid-point sample at the bit edge
                err = (v - prev) * at(pos - T / 2) / amp
                pos -= 0.06 * max(-1.0, min(1.0, err)) * T * 0.5
            prev = v
            pos += T
            if pos + 2 >= n:
                return None
        b = np.array(bits, np.int8) ^ pol
        body = np.packbits(b.reshape(-1, 8)[:, ::-1], axis=1).ravel()      # LSB first
        return parse_frame(HEADER + bytes(body.tolist()))


# ---------------------------------------------------------------------- frame builder (used by the tests)
def build_frame(sid="N1234567", frame_no=1234, lat=51.5, lon=-0.12, alt=1500.0, week=2400, tow=345600.0, sats=9,
                vn=1.0, ve=2.0, vu=5.0):
    a, b = 6378137.0, 6356752.31424518
    e2 = (a * a - b * b) / (a * a)
    phi, lam = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    x = (n + alt) * math.cos(phi) * math.cos(lam)
    y = (n + alt) * math.cos(phi) * math.sin(lam)
    z = (n * (1 - e2) + alt) * math.sin(phi)
    vx = -vn * math.sin(phi) * math.cos(lam) - ve * math.sin(lam) + vu * math.cos(phi) * math.cos(lam)
    vy = -vn * math.sin(phi) * math.sin(lam) + ve * math.cos(lam) + vu * math.cos(phi) * math.sin(lam)
    vz = vn * math.cos(phi) + vu * math.sin(phi)
    f = bytearray(FRAME_LEN)
    f[:8] = HEADER

    def block(pos, bid, data):
        f[pos] = bid
        f[pos + 1] = len(data)
        f[pos + 2:pos + 2 + len(data)] = data
        c = crc16(bytes(data))
        f[pos + 2 + len(data)] = c & 0xFF
        f[pos + 3 + len(data)] = c >> 8
    st = bytearray(0x28)
    st[0:2] = frame_no.to_bytes(2, "little")
    st[2:10] = sid.encode()
    block(0x39, 0x79, st)
    block(0x65, 0x7A, bytearray(0x2A))
    gps = bytearray(0x1E)
    gps[0:2] = week.to_bytes(2, "little")
    gps[2:6] = int(tow * 1000).to_bytes(4, "little")
    block(0x93, 0x7C, gps)
    block(0xB5, 0x7D, bytearray(0x59))
    nav = bytearray(0x15)
    for i, v in enumerate((x, y, z)):
        nav[4 * i:4 * i + 4] = int(round(v * 100)).to_bytes(4, "little", signed=True)
    for i, v in enumerate((vx, vy, vz)):
        nav[12 + 2 * i:14 + 2 * i] = int(round(v * 100)).to_bytes(2, "little", signed=True)
    nav[18] = sats
    block(0x112, 0x7B, nav)
    return bytes(f[i] ^ MASK[i % 64] if i >= 8 else f[i] for i in range(FRAME_LEN))


def modulate_frame(frame, fs=48000, lead=0.2, dev=1.0, noise=0.0, offset=0.0, drift=0.0, seed=2):
    """The discriminator signal (+-1 = +-2.4 kHz) for one frame, with a small clock error and DC offset if wanted."""
    bits = []
    for v in frame:
        bits += _bits_of(v)
    t = fs / 4800.0 * (1 + drift)
    idx = (np.arange(int(len(bits) * t)) / t).astype(int)
    sig = np.array(bits, np.float32)[idx] * 2 - 1
    # gentle gaussian-ish smoothing, like a real FSK channel
    k = np.hanning(7)
    sig = np.convolve(sig, k / k.sum(), mode="same")
    out = np.concatenate((np.zeros(int(lead * fs)), sig * dev, np.zeros(int(0.3 * fs)))) + offset
    if noise:
        out = out + np.random.default_rng(seed).normal(0, noise, len(out))
    return out.astype(np.float32)
