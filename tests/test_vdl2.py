"""VDL2 decoder tests: Reed-Solomon, header FEC, framing, AVLC/ACARS parsing and a full over-the-air round trip.
The transmitter used here is written from the same protocol description, so these prove the decoder is self-consistent,
not that it hears real aircraft."""
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import vdl2 as v                      # noqa: E402
from andysdr.acars import build_block              # noqa: E402


def _frame(text="POS N5123.4 W00012.3 FL350", reg="G-EUXY", icao=0x4CA2B1):
    chars, _ = build_block(reg=reg, label="H1", bid="3", text=text, flight="BA0123")
    return v.build_avlc(icao, 0x2A1C00, bytes(chars))


def _run(iq, chunk=2400):
    got = []
    d = v.Vdl2Decoder(on_message=got.append)
    for i in range(0, len(iq), chunk):
        d.process(iq[i:i + chunk])
    return d, got


def test_reed_solomon_fixes_and_detects():
    rnd = random.Random(1)
    for _ in range(60):
        d = [rnd.randrange(256) for _ in range(249)]
        cw = d + v.rs_encode(d)
        assert v.rs_decode(list(cw)) == 0
        c = list(cw)
        errs = rnd.sample(range(255), rnd.randrange(1, 4))
        for p in errs:
            c[p] ^= rnd.randrange(1, 256)
        assert v.rs_decode(c) == len(errs) and c == cw
        c = list(cw)
        ps = rnd.sample(range(255), 4)
        for p in ps:
            c[p] ^= rnd.randrange(1, 256)
        assert v.rs_decode(c, ps[:2]) >= 0 and c == cw        # 2 known-bad bytes + 2 unknown ones


def test_header_single_bit_error_is_fixed():
    for datalen in (100, 2461, 0x1000):
        tr = v._rev(datalen, 17)
        word = tr << 5
        f = 0
        for i, h in enumerate(v._H):
            f |= v._par(word & h) << (4 - i)
        word |= f
        for bit in range(22):
            bad = word ^ (1 << bit)
            fixed, syn = v.header_fix(bad)
            assert fixed == word and syn != 0


def test_burst_bits_decode_without_radio():
    rnd = random.Random(2)
    for n in (20, 200, 300, 600):
        fr = bytes(rnd.randrange(256) for _ in range(n))
        bits = v.build_burst_bits([fr])
        d = bits ^ v._keystream(len(bits))
        datalen, syn = v.read_header(d[:25])
        assert syn == 0
        assert v.decode_payload(d[25:], datalen) == [fr]
        # damage one byte of the payload: Reed-Solomon repairs it (short blocks only carry enough parity for one)
        bad = d.copy()
        p = 25 + 8 * rnd.randrange(0, (len(bad) - 25) // 8 - 8)
        bad[p:p + 8] ^= 1
        assert v.decode_payload(bad[25:], datalen) == [fr]


def test_avlc_and_acars_parse():
    a = v.parse_avlc(_frame())
    assert a["src"]["addr"] == 0x4CA2B1 and a["dst"]["addr"] == 0x2A1C00
    assert a["acars"]["reg"] == "G-EUXY" and a["acars"]["label"] == "H1" and "FL350" in a["acars"]["text"]
    bad = bytearray(_frame())
    bad[12] ^= 0x10
    assert v.parse_avlc(bytes(bad)) is None                  # checksum catches it


def test_over_the_air_round_trip_with_offsets_and_noise():
    rnd = random.Random(3)
    for n in (0, 40, 150, 200):
        fr = _frame("X" * n)
        for chunk in (2400, 777):
            iq = v.modulate([fr], lead=0.07, tail=0.1, noise=0.04, cfo=rnd.uniform(-600, 600), phase0=rnd.uniform(0, 6))
            d, got = _run(iq, chunk)
            assert d.acars == 1 and got[0]["reg"] == "G-EUXY" and got[0]["via"] == "VDL2"
            assert got[0]["icao"] == "4CA2B1" and got[0]["gs"] == "2A1C00"


def test_two_frames_in_one_burst_and_long_burst():
    f1, f2 = _frame("ONE"), _frame("TWO", reg="G-ABCD")
    d, got = _run(v.modulate([f1, f2], noise=0.03))
    assert d.frames == 2 and [m["reg"] for m in got] == ["G-EUXY", "G-ABCD"]
    long = v.build_avlc(0x123456, 0x2A1C00, None, lcf=0x03) + bytes(range(256)) * 2   # a burst needing two RS blocks
    long = long[:-2]
    fc = v.fcs16(long) ^ 0xFFFF
    long += bytes([fc & 0xFF, fc >> 8])
    d, _ = _run(v.modulate([long], noise=0.03))
    assert d.bursts == 1 and d.frames == 1


def test_back_to_back_bursts_and_noise_only():
    rnd = random.Random(4)
    parts = [v.modulate([_frame("M%d" % k)], lead=rnd.uniform(0.01, 0.3), tail=0.02, noise=0.03,
                        cfo=rnd.uniform(-300, 300), phase0=k) for k in range(4)]
    d, got = _run(np.concatenate(parts))
    assert d.acars == 4
    rng = np.random.default_rng(9)
    d, got = _run((rng.standard_normal(96000) + 1j * rng.standard_normal(96000)).astype(np.complex64) * 0.3)
    assert d.bursts == 0 and not got
