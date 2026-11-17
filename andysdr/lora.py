"""LoRa physical layer in numpy: modulator (used for self-tests) and a receive-only demodulator.

Implements the open, reverse-engineered LoRa PHY (explicit header, CRC, coding rates 4/5 .. 4/8, optional low-data-rate
optimisation).  Conventions follow gr-lora_sdr (Tapparel et al.): whitening LFSR, Hamming(4+cr, 4), diagonal
interleaver, Gray mapping with the "-1" symbol offset, CRC-16 CCITT with the last two payload bytes folded in.

Receiver chain (`LoraDemod`): input is complex baseband already resampled to exactly one sample per chip
(fs = BW).  It finds a run of preamble up-chirps, locks timing and carrier offset from the preamble and the
2.25 down-chirp start-of-frame delimiter, de-chirps every symbol with an FFT, then decodes header and payload.
"""
import math

import numpy as np

# --------------------------------------------------------------------------- constants
def _whitening():
    st, out = 0xFF, []
    for _ in range(255):
        out.append(st)
        fb = ((st >> 7) ^ (st >> 5) ^ (st >> 4) ^ (st >> 3)) & 1
        st = ((st << 1) & 0xFF) | fb
    return out


WHITEN = _whitening()
SYNC_DEFAULT = 0x12


def crc16(data, crc=0):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _par(*bits):
    r = 0
    for b in bits:
        r ^= b
    return r


def _cw_encode(nib, cr, rev=True):
    """Nibble -> codeword with (4+cr) bits.  Layout (MSB first): d3 d2 d1 d0 p0 p1 p2 p3, truncated for cr<4;
    d0 is the nibble's MSB, so the nibble's LSB leads the codeword (gr-lora_sdr).  rev=False gives the plain order."""
    b = [(nib >> 3) & 1, (nib >> 2) & 1, (nib >> 1) & 1, nib & 1]       # MSB first
    d0, d1, d2, d3 = b if rev else b[::-1]
    if cr == 1:
        bits = [d3, d2, d1, d0, _par(d0, d1, d2, d3)]
    else:
        p0, p1, p2, p3 = _par(d3, d2, d1), _par(d2, d1, d0), _par(d3, d2, d0), _par(d3, d1, d0)
        bits = ([d3, d2, d1, d0, p0, p1, p2, p3])[:4 + cr]
    v = 0
    for x in bits:
        v = (v << 1) | x
    return v


_CW_TABLES = {}


def _cw_table(cr, rev):
    k = (cr, rev)
    if k not in _CW_TABLES:
        _CW_TABLES[k] = [_cw_encode(n, cr, rev) for n in range(16)]
    return _CW_TABLES[k]


def _cw_decode(cw, cr, rev):
    """Nearest valid codeword -> nibble (corrects one bit error for 4/7 and 4/8)."""
    tab = _cw_table(cr, rev)
    best, bd = 0, 99
    for n, c in enumerate(tab):
        d = bin(c ^ cw).count("1")
        if d < bd:
            best, bd = n, d
            if d == 0:
                break
    return best, bd


def _hdr_checksum(n0, n1, n2):
    b = lambda n, i: (n >> i) & 1
    c4 = b(n0, 3) ^ b(n0, 2) ^ b(n0, 1) ^ b(n0, 0)
    c3 = b(n0, 3) ^ b(n1, 3) ^ b(n1, 2) ^ b(n1, 1) ^ b(n2, 0)
    c2 = b(n0, 2) ^ b(n1, 3) ^ b(n1, 0) ^ b(n2, 3) ^ b(n2, 1)
    c1 = b(n0, 1) ^ b(n1, 2) ^ b(n1, 0) ^ b(n2, 2) ^ b(n2, 1) ^ b(n2, 0)
    c0 = b(n0, 0) ^ b(n1, 1) ^ b(n2, 3) ^ b(n2, 2) ^ b(n2, 1) ^ b(n2, 0)
    return c4, (c3 << 3) | (c2 << 2) | (c1 << 1) | c0


def ldro_needed(sf, bw):
    return (2 ** sf) / bw > 0.016


def n_symbols(plen, sf, cr, has_crc, ldro):
    """Total symbols in the frame body (header block + payload blocks), explicit header."""
    nn = 2 * (plen + (2 if has_crc else 0))
    first = sf - 7                                  # payload nibbles that fit in the header block
    rest = max(0, nn - first)
    per = sf - 2 * (1 if ldro else 0)
    return 8 + (4 + cr) * int(math.ceil(rest / per)) if rest else 8


# --------------------------------------------------------------------------- transmit side (tests, simulator)
def encode_frame(payload, sf, cr=1, has_crc=True, ldro=False, rev=True):
    """Payload bytes -> list of raw chirp symbol values (0 .. 2^sf-1), explicit header."""
    n = 1 << sf
    p = list(payload)
    data = list(p)
    if has_crc and len(p) >= 2:
        c = crc16(p[:-2]) ^ (p[-1] | (p[-2] << 8))
        data += [c & 0xFF, c >> 8]
    data = [b ^ WHITEN[i] for i, b in enumerate(data)]
    nibs = []
    for b in data:
        nibs += [b & 0xF, b >> 4]
    c4, c30 = _hdr_checksum(len(p) >> 4, len(p) & 0xF, (cr << 1) | int(has_crc))
    header = [len(p) >> 4, len(p) & 0xF, (cr << 1) | int(has_crc), c4, c30]
    allnib = header + nibs
    syms = []
    pos = 0
    first = True
    while pos < len(allnib):
        if first:
            sf_app, cw_len, red = sf - 2, 8, True
        else:
            red = ldro
            sf_app, cw_len = (sf - 2 if ldro else sf), 4 + cr
        blk = allnib[pos:pos + sf_app]
        blk += [0] * (sf_app - len(blk))
        pos += sf_app
        cws = [_cw_encode(x, 4 if first else cr, rev) for x in blk]
        for i in range(cw_len):
            g = 0
            for j in range(sf_app):
                bit = (cws[(i - j - 1) % sf_app] >> (cw_len - 1 - i)) & 1
                g = (g << 1) | bit
            v = g
            sh = 1
            while (g >> sh) > 0:
                v ^= g >> sh
                sh += 1                                   # inverse Gray (prefix XOR)
            if red:
                v <<= 2
            syms.append((v + 1) % n)
        first = False
    return syms


def _chirp(n, s, sf_n, tau=0.0, scale=1.0):
    """Up-chirp symbol s sampled at integer times (n) minus tau (continuous-phase)."""
    u = (np.asarray(n, dtype=np.float64) - tau) * scale
    N = sf_n
    ph = np.pi * u * u / N + 2 * np.pi * (s / N - 0.5) * u
    wrap = u >= (N - s)
    ph = np.where(wrap, ph - 2 * np.pi * (u - (N - s)), ph)
    return np.exp(1j * ph)


def modulate(symbols, sf, preamble=8, sync=SYNC_DEFAULT, tau=0.0, cfo_bins=0.0, drift=0.0, snr_db=None,
             lead=3, tail=3, rng=None):
    """Frame -> complex baseband at 1 sample/chip.  tau = fractional start delay, cfo_bins = carrier offset in bins,
    drift = sample-clock error (fraction).  Noise is relative to the signal power in the channel."""
    N = 1 << sf
    seq = [("u", 0)] * preamble + [("u", (sync >> 4) << 3), ("u", (sync & 0xF) << 3), ("d", 0), ("d", 0), ("q", 0)]
    seq += [("u", s) for s in symbols]
    total = lead * N + sum(N if k != "q" else N // 4 for k, _ in seq) + tail * N
    n = np.arange(total)
    t = n * (1.0 - drift) - tau                       # TX time in samples (drift: RX clock vs TX clock)
    x = np.zeros(total, dtype=np.complex128)
    pos = lead * N
    for kind, s in seq:
        ln = N // 4 if kind == "q" else N
        m = (t >= pos) & (t < pos + ln)
        u = t[m] - pos
        if kind == "u":
            x[m] = _chirp(u, s, N)
        else:
            x[m] = np.conj(_chirp(u, 0, N))
        pos += ln
    x *= np.exp(2j * np.pi * cfo_bins * n / N)
    if snr_db is not None:
        rng = rng or np.random.default_rng(1)
        sig_pow = 1.0
        npw = sig_pow / (10 ** (snr_db / 10))
        x += (rng.standard_normal(total) + 1j * rng.standard_normal(total)) * math.sqrt(npw / 2)
    return x.astype(np.complex64)


# --------------------------------------------------------------------------- receive side
class Frame:
    def __init__(self, payload, crc_ok, cr, has_crc, snr_db, cfo_hz, start, end, sf):
        self.payload, self.crc_ok, self.cr, self.has_crc = bytes(payload), crc_ok, cr, has_crc
        self.snr_db, self.cfo_hz, self.start, self.end, self.sf = snr_db, cfo_hz, start, end, sf


def _gray(v):
    return v ^ (v >> 1)


def _igray(g):
    v = g
    sh = 1
    while (g >> sh) > 0:
        v ^= g >> sh
        sh += 1
    return v


class LoraDemod:
    def __init__(self, sf=8, bw=62500.0, ldro=None, min_preamble=6):
        self.sf, self.bw = sf, float(bw)
        self.N = 1 << sf
        self.ldro = ldro_needed(sf, bw) if ldro is None else ldro
        self.min_pre = min_preamble
        n = np.arange(self.N)
        self.up = np.exp(1j * np.pi * (n * n / self.N - n)).astype(np.complex64)
        self.down = np.conj(self.up)
        self.ofac = 4                                   # FFT zero-padding for fractional peak positions

    # ---- low-level
    def _spec(self, x, ref):
        return np.abs(np.fft.fft(x * ref))

    def _peak(self, x, ref):
        """(bin, magnitude, mean-power) of the dechirped window; bin is fractional (zero-padded FFT)."""
        N, of = self.N, self.ofac
        sp = np.fft.fft(x * ref, N * of)
        mag = np.abs(sp)
        # circular folding: peak position in zero-padded bins / of
        k = int(np.argmax(mag))
        a, b, c = mag[(k - 1) % (N * of)], mag[k], mag[(k + 1) % (N * of)]
        den = a - 2 * b + c
        d = 0.5 * (a - c) / den if den != 0 else 0.0
        return ((k + d) / of) % N, float(b), float(np.mean(mag ** 2))

    def _sym(self, x, start, ref, off):
        w = x[start:start + self.N]
        if len(w) < self.N:
            return None
        return self._spec(w, ref)

    # ---- detection
    def _scan(self, x):
        """Return (grid_phase, run_start_window, n_windows, b_up) candidates for preamble runs, strongest first."""
        N = self.N
        step = N // 4
        nwin = (len(x) - N) // step
        if nwin < self.min_pre * 4:
            return []
        peaks = np.zeros(nwin)
        bins = np.zeros(nwin)
        for w in range(nwin):
            seg = x[w * step:w * step + N]
            sp = np.abs(np.fft.fft(seg * self.down))
            k = int(np.argmax(sp))
            bins[w] = k
            peaks[w] = sp[k] / (np.sqrt(np.mean(sp ** 2)) + 1e-12)
        thr = 4.6
        out = []
        ph = 0
        w = 0
        while w < nwin:
            if peaks[w] < thr:
                w += 1
                continue
            # follow windows spaced one symbol apart with a constant bin (the bin does not move for a pure preamble)
            run = [w]
            nxt = w + 4
            while nxt < nwin and peaks[nxt] >= thr and abs(((bins[nxt] - bins[run[-1]] + N / 2) % N) - N / 2) <= 1.5:
                run.append(nxt)
                nxt += 4
            if len(run) >= self.min_pre:
                out.append((w, len(run), float(np.mean(peaks[run]))))
                w = run[-1] + 4
            else:
                w += 1
        return out

    def _find_sfd(self, x, t0):
        """From the first window of a preamble run, slide forward to locate the plateau of down-chirp windows.
        Returns (t_plateau_start, t_plateau_len) in samples or None."""
        N = self.N
        step = N // 8
        # search window: preamble can be long (up to ~40 symbols) plus 2 sync symbols
        tmax = min(len(x) - N, t0 + 60 * N)
        ts = np.arange(t0, tmax, step)
        up_s = np.zeros(len(ts))
        dn_s = np.zeros(len(ts))
        for i, t in enumerate(ts):
            seg = x[t:t + N]
            sd = np.abs(np.fft.fft(seg * self.up))               # strong for down-chirps
            su = np.abs(np.fft.fft(seg * self.down))
            m = np.sqrt(np.mean(sd ** 2)) + 1e-12
            dn_s[i] = sd.max() / m
            up_s[i] = su.max() / (np.sqrt(np.mean(su ** 2)) + 1e-12)
        if len(ts) < 14:
            return None
        # box-sum over the span of windows that fit entirely inside the 2.25 down-chirps (1.25 N) – robust at low SNR
        width = int(round(1.25 * N / step))
        score = np.convolve(dn_s - 0.5 * up_s, np.ones(width), mode="valid")
        i = int(np.argmax(score))
        pk = score[i] / width
        if pk < 3.2:
            return None
        mid = int(round(ts[i] + (width - 1) * step / 2.0))
        return mid - int(0.625 * N), mid                # (coarse SFD start, a window start fully inside the down-chirps)

    # ---- frame decode
    def decode_at(self, x, t_pre, n_pre):
        """Try to decode one frame whose preamble run begins near sample t_pre.  Returns Frame or None."""
        N = self.N
        plat = self._find_sfd(x, t_pre)
        if plat is None:
            return None
        t_dn0, tdw = plat
        # fractional preamble bin (average of a few windows inside the run, same grid phase as t_pre)
        bu = []
        for k in range(1, min(n_pre - 1, 5)):
            p = self._peak(x[t_pre + k * N:t_pre + (k + 1) * N], self.down)
            bu.append(p[0])
        if not bu:
            return None
        # circular mean of bins
        ang = np.angle(np.mean(np.exp(2j * np.pi * np.array(bu) / N)))
        b_u = (ang / (2 * np.pi) * N) % N
        # down-chirp window strictly inside the plateau (any window there has the same peak bin)
        if tdw + N > len(x) or tdw < 0:
            return None
        b_d, _, _ = self._peak(x[tdw:tdw + N], self.up)
        u = t_pre % N
        d = tdw % N
        # solve f (carrier offset, bins) and P0 (symbol-boundary phase) modulo the N/2 ambiguity
        diff = ((b_u - b_d) + N / 2) % N - N / 2                    # b_u - b_d wrapped to (-N/2, N/2]
        s = (u + d)
        p0_cands = [((s - diff) / 2 + k * N / 2) % N for k in range(2)]
        # sfd boundary near the plateau start: choose P0 whose boundary lies closest (modulo N) to t_dn0
        best = None
        for p0 in p0_cands:
            e = ((t_dn0 - p0) + N / 2) % N - N / 2
            if best is None or abs(e) < abs(best[1]):
                best = (p0, e)
        p0 = best[0]
        f = ((b_u - (((u - p0) + N / 2) % N - N / 2)) + N / 2) % N - N / 2
        # absolute start of the SFD: plateau start snapped to the P0 grid
        k0 = round((t_dn0 - p0) / N)
        sfd = p0 + k0 * N
        if sfd < t_dn0 - N // 4:
            sfd += N
        if sfd > t_dn0 + N // 4 + 1:
            sfd -= N
        return self._decode_aligned(x, t_pre, sfd, f, n_pre)

    def _shift_cfo(self, seg, e, f):
        """Advance seg by a fractional delay e (samples, via FFT) and remove a carrier offset of f bins."""
        M = len(seg)
        X = np.fft.fft(seg)
        k = np.fft.fftfreq(M)                                  # cycles/sample, signed
        y = np.fft.ifft(X * np.exp(2j * np.pi * k * e))
        n = np.arange(M)
        return (y * np.exp(-2j * np.pi * f * n / self.N)).astype(np.complex64)

    def _decode_aligned(self, x, t_pre, sfd, f, n_pre=8):
        N, sf = self.N, self.sf
        a = max(0, t_pre - N)
        nmax = n_symbols(255, sf, 4, True, self.ldro)
        bseg = min(len(x), int(sfd + 2.25 * N + (nmax + 2) * N))
        seg = x[a:bseg]
        sfd_s = sfd - a                                        # SFD boundary in segment coordinates (float)
        for _ in range(3):
            fl = math.floor(sfd_s)
            z = self._shift_cfo(seg, sfd_s - fl, f)
            # on the corrected signal the preamble and the first down-chirp must both peak at bin 0
            ups = [self._peak(z[fl - j * N:fl - j * N + N], self.down)[0] for j in range(3, max(4, min(n_pre - 2, 12)) + 1) if fl - j * N >= 0]
            if fl + N > len(z) or not ups:
                return None
            bu = float(np.angle(np.mean(np.exp(2j * np.pi * np.array(ups) / N))) / (2 * np.pi) * N)
            bd = self._peak(z[fl:fl + N], self.up)[0]
            bd = (bd + N / 2) % N - N / 2
            df, et = (bu + bd) / 2.0, (bd - bu) / 2.0
            f += df
            sfd_s += et
            if abs(df) < 0.02 and abs(et) < 0.02:
                break
        fl = math.floor(sfd_s)
        z = self._shift_cfo(seg, sfd_s - fl, f)
        data0 = fl + 2.25 * N
        cfo_hz = f * self.bw / N
        return self._decode_body(z, data0, 0.0, cfo_hz, 0.0, a + fl, a)

    def _symbols(self, x, data0, f, eps, count, k0=0):
        """FFT-demodulate `count` symbols starting at symbol index k0.
        Returns (raw symbol values, fractional bins with the timing residual removed, per-symbol SNR)."""
        N = self.N
        out, fr, snr = [], [], []
        for k in range(k0, k0 + count):
            pos = data0 + k * N * (1.0 - eps)
            q = int(round(pos))
            if q < 0 or q + N > len(x):
                return None, None, None
            delta = q - pos
            b, mg, mp = self._peak(x[q:q + N], self.down)
            bf = b - delta
            fr.append(bf)
            out.append(int(round(bf - f)) % N)
            if (k - k0) % 4 == 0:                         # per-chip SNR estimate (processing gain removed)
                p = np.abs(np.fft.fft(x[q:q + N] * self.down)) ** 2
                noise = max(float(np.median(p)) / 0.693, 1e-12)
                snr.append(max(p.max() / noise - 1.0, 1e-3) / N)
        return out, fr, snr

    def _decode_body(self, x, data0, f, cfo_hz, ppm, t_pre, a0=0):
        N, sf = self.N, self.sf
        eps = 0.0
        # clock error from the carrier error: both come from the same crystal (carrier frequency known by caller)
        eps = getattr(self, "carrier_hz", 0.0) and cfo_hz / self.carrier_hz or 0.0
        hdr, fr, snr = self._symbols(x, data0, f, eps, 8)
        if hdr is None:
            return None
        # header symbols are always (4*v + 1): the fractional part of their bins pins the carrier/timing offset
        res = [((b - f - 1.0) + 2.0) % 4.0 - 2.0 for b in fr]
        f = f + float(np.median(res))
        hdr = [int(round(b - f)) % N for b in fr]
        for conv in _CONVS:
            h = self._parse_header(hdr, conv)
            if h:
                break
        else:
            return None
        plen, cr, has_crc = h["len"], h["cr"], h["crc"]
        nsym = n_symbols(plen, sf, cr, has_crc, self.ldro)
        syms, _, snr = self._symbols(x, data0, f, eps, nsym)
        if syms is None:
            return None
        res = self._decode_frame(syms, conv, plen, cr, has_crc)
        if res is None:
            return None
        payload, crc_ok = res
        end = int(a0 + data0 + nsym * N)
        s = float(np.median(snr)) if snr else 0.0
        return Frame(payload, crc_ok, cr, has_crc, 10 * math.log10(max(s, 1e-9)), cfo_hz, int(t_pre), end, sf)

    # ---- bits -> bytes
    def _blocks(self, syms, conv, plen, cr, has_crc):
        """Deinterleave all blocks to nibbles.  conv = (gray_mode, rev, offset)."""
        gmode, rev, off = conv
        sf, N = self.sf, self.N
        nibs = []
        pos = 0
        first = True
        errs = 0
        while pos < len(syms):
            if first:
                sf_app, cw_len, red, crr = sf - 2, 8, True, 4
            else:
                red = self.ldro
                sf_app, cw_len, crr = (sf - 2 if self.ldro else sf), 4 + cr, cr
            blk = syms[pos:pos + cw_len]
            if len(blk) < cw_len:
                break
            pos += cw_len
            cws = [0] * sf_app
            for i, raw in enumerate(blk):
                v = (raw - off) % N
                if red:
                    v >>= 2
                g = _gray(v) if gmode == 0 else _igray(v)
                for j in range(sf_app):
                    bit = (g >> (sf_app - 1 - j)) & 1
                    k = (i - j - 1) % sf_app
                    cws[k] |= bit << (cw_len - 1 - i)
            for cw in cws:
                nb, e = _cw_decode(cw, crr, rev)
                errs += e
                nibs.append(nb)
            first = False
        return nibs, errs

    def _parse_header(self, hdr_syms, conv):
        nibs, _ = self._blocks(hdr_syms, conv, 0, 4, True)
        if len(nibs) < 5:
            return None
        n0, n1, n2, n3, n4 = nibs[:5]
        c4, c30 = _hdr_checksum(n0, n1, n2)
        if n3 != c4 or n4 != c30:
            return None
        plen, cr, crc = (n0 << 4) | n1, n2 >> 1, n2 & 1
        if plen == 0 or not (1 <= cr <= 4):
            return None
        return {"len": plen, "cr": cr, "crc": bool(crc)}

    def _decode_frame(self, syms, conv, plen, cr, has_crc):
        nibs, errs = self._blocks(syms, conv, plen, cr, has_crc)
        nibs = nibs[5:]
        need = 2 * (plen + (2 if has_crc else 0))
        if len(nibs) < need:
            return None
        raw = [nibs[2 * i] | (nibs[2 * i + 1] << 4) for i in range(need // 2)]
        data = [b ^ WHITEN[i] for i, b in enumerate(raw)]
        payload = data[:plen]
        if not has_crc:
            return bytes(payload), None
        if plen < 2:
            return bytes(payload), None
        calc = crc16(payload[:-2]) ^ (payload[-1] | (payload[-2] << 8))
        rx = data[plen] | (data[plen + 1] << 8)
        rx_raw = raw[plen] | (raw[plen + 1] << 8)                # in case the CRC bytes are not whitened
        return bytes(payload), (calc == rx or calc == rx_raw)

    # ---- public
    def process(self, x, carrier_hz=0.0):
        """Find and decode all frames in a complex baseband buffer (1 sample/chip).  Returns a list of Frame."""
        self.carrier_hz = carrier_hz
        x = np.asarray(x, dtype=np.complex64)
        frames = []
        done = -1
        for (w, n_win, strength) in self._scan(x):
            t = w * (self.N // 4)
            if t < done:
                continue
            fr = self.decode_at(x, t, n_win)
            if fr:
                frames.append(fr)
                done = fr.end
        return frames


# conventions tried in order for the header: (gray mode, nibble reversed, symbol offset)
_CONVS = [(0, True, 1), (0, False, 1), (1, True, 1), (1, False, 1), (0, True, 0), (0, False, 0), (1, True, 0), (1, False, 0)]
