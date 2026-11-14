"""ADS-B decoder tests.  Run:  python -I tests/test_adsb.py   (from the AndySDR folder)"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APPDATA", tempfile.mkdtemp())

import numpy as np  # noqa: E402

from andysdr import adsb, adsb_sim  # noqa: E402


class FakeSettings(dict):
    def save(self):
        pass


def new_tracker(**kw):
    t = adsb.Tracker(FakeSettings(adsb_log=False, **kw), lambda *a: None)
    return t


def feed(t, hexmsg, now):
    b = bytes.fromhex(hexmsg)
    t.raw(b, adsb.crc_of_bytes(b), -20.0, now)


def test_crc():
    assert adsb.crc_of_bytes(bytes.fromhex("8D4840D6202CC371C32CE0576098")) == 0
    assert adsb.crc_of_bytes(bytes.fromhex("8D40621D58C382D690C8AC2863A7")) == 0
    assert adsb.crc_of_bytes(bytes.fromhex("8D4840D6202CC371C32CE0576099")) != 0


def test_known_frames():
    t = new_tracker()
    now = time.time()
    feed(t, "8D4840D6202CC371C32CE0576098", now)                    # identification
    feed(t, "8D40621D58C382D690C8AC2863A7", now)                    # even position
    feed(t, "8D40621D58C386435CC412692AD6", now + 1)                # odd position
    feed(t, "8D485020994409940838175B284F", now)                    # velocity
    a = t.ac[0x4840D6]
    assert a.flight == "KLM1023", a.flight
    b = t.ac[0x40621D]
    # odd frame arrived last -> the odd-frame solution of the classic worked example
    assert abs(b.lat - 52.26578) < 1e-4 and abs(b.lon - 3.93891) < 1e-4, (b.lat, b.lon)
    assert b.alt == 38000, b.alt
    t2 = new_tracker()                                                # even frame last -> 52.25720, 3.91937
    feed(t2, "8D40621D58C386435CC412692AD6", now)
    feed(t2, "8D40621D58C382D690C8AC2863A7", now + 1)
    b2 = t2.ac[0x40621D]
    assert abs(b2.lat - 52.25720) < 1e-4 and abs(b2.lon - 3.91937) < 1e-4, (b2.lat, b2.lon)
    c = t.ac[0x485020]
    assert round(c.gs) == 159 and abs(c.trk - 182.88) < 0.1 and c.vr == -832, (c.gs, c.trk, c.vr)


def test_single_bit_repair():
    t = new_tracker()
    now = time.time()
    feed(t, "8D4840D6202CC371C32CE0576098", now)
    good = bytes.fromhex("8D40621D58C382D690C8AC2863A7")
    t.raw(good, 0, -20.0, now)                                       # make 40621D known
    bad = bytearray(bytes.fromhex("8D4840D6202CC371C32CE0576098"))
    bad[7] ^= 0x10
    t.raw(bytes(bad), adsb.crc_of_bytes(bytes(bad)), -20.0, now + 1)
    assert t.corrected == 1 and t.ac[0x4840D6].msgs == 2


def test_df4_altitude_needs_known_aircraft():
    t = new_tracker()
    now = time.time()
    # DF4 reply: altitude 38000 ft (Q bit set), parity overlaid with the ICAO address
    body = bytearray(4)
    body[0] = (4 << 3)
    n = (38000 + 1000) // 25
    body[2] = (n >> 6) & 31
    body[3] = ((n & 0x20) << 1) | ((n & 0x10) << 1) | 0x10 | (n & 15)
    body[3] = ((n & 0x20) << 2 >> 0) & 0x80 | ((n & 0x10) << 1) & 0x20 | 0x10 | (n & 15)
    v = int.from_bytes(bytes(body), "big") << 24
    par = adsb.crc24(v, 56) ^ 0x40621D
    msg = bytes(body) + par.to_bytes(3, "big")
    t.raw(msg, adsb.crc_of_bytes(msg), -30.0, now)
    assert 0x40621D not in t.ac                                       # unknown aircraft ignored
    feed(t, "8D40621D58C382D690C8AC2863A7", now)
    t.raw(msg, adsb.crc_of_bytes(msg), -30.0, now + 1)
    assert t.ac[0x40621D].alt == 38000, t.ac[0x40621D].alt


def test_encoder_roundtrip():
    msg = adsb_sim.df17(0xABCDEF, adsb_sim.me_ident("BAW123"))
    assert adsb.crc_of_bytes(msg) == 0
    t = new_tracker()
    t.raw(msg, 0, -20, time.time())
    assert t.ac[0xABCDEF].flight == "BAW123"
    me = adsb_sim.me_position(51.5, -0.5, 30000, False)
    me2 = adsb_sim.me_position(51.5, -0.5, 30000, True)
    now = time.time()
    for m, dt in ((me, 0), (me2, 0.5)):
        f = adsb_sim.df17(0xABCDEF, m)
        t.raw(f, 0, -20, now + dt)
    a = t.ac[0xABCDEF]
    assert abs(a.lat - 51.5) < 1e-3 and abs(a.lon + 0.5) < 1e-3 and a.alt == 30000, (a.lat, a.lon, a.alt)
    f = adsb_sim.df17(0xABCDEF, adsb_sim.me_velocity(420, 250, -1200))
    t.raw(f, 0, -20, now + 1)
    assert abs(a.gs - 420) < 2 and abs(a.trk - 250) < 1 and a.vr == -1216 or a.vr == -1152, (a.gs, a.trk, a.vr)


def test_local_decode_with_receiver_position():
    t = new_tracker(adsb_lat=51.5, adsb_lon=-0.5)
    f = adsb_sim.df17(0x123456, adsb_sim.me_position(52.0, 0.2, 12000, True))
    t.raw(f, 0, -20, time.time())
    a = t.ac[0x123456]
    assert abs(a.lat - 52.0) < 1e-3 and abs(a.lon - 0.2) < 1e-3
    assert a.dist and 35 < a.dist < 45, a.dist


def synth(fs, seconds, traffic, dc=0.02, noise=0.004, freq_off=0.0, seed=3):
    """Build IQ at fs containing the traffic's frames."""
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    env = np.zeros(n, np.float32)
    sent = []
    allmsgs = []
    for k in range(int(seconds / 0.05)):                              # step the traffic like the simulator does
        allmsgs += traffic.messages(k * 0.05, (k + 1) * 0.05)
    for tt, msg, amp in allmsgs:
        e = adsb_sim.modulate(msg, fs, amp)
        s = int(tt * fs)
        if s + len(e) < n:
            env[s:s + len(e)] += e
            sent.append(msg)
    t = np.arange(n) / fs
    carrier = np.exp(2j * np.pi * freq_off * t)
    iq = (env * carrier).astype(np.complex64)
    iq += (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * noise + dc
    return iq, sent


def run_demod(fs, seconds=3.0, **kw):
    traffic = adsb_sim.SimTraffic()
    iq, sent = synth(fs, seconds, traffic, **kw)
    d = adsb.Demod()
    t = new_tracker()
    got = 0
    block = int(fs * 0.05)
    now = time.time()
    for i in range(0, len(iq) - block + 1, block):
        blk = iq[i:i + block]
        mag = np.abs(blk - blk.mean()).astype(np.float32)
        for data, crc, db in d.process(mag, fs):
            if crc == 0 and (data[0] >> 3) == 17:
                got += 1
            t.raw(data, crc, db, now + i / fs)
    return got, len(sent), t


def test_demod_2_4_msps():
    got, sent, t = run_demod(2400000, seconds=5.5, freq_off=300000.0)
    assert got >= 0.9 * sent, (got, sent)
    assert len(t.ac) == 10, len(t.ac)
    assert sum(1 for a in t.ac.values() if a.flight) >= 8       # one ident per 5 s each, so a lost frame is a lost callsign
    assert t.positions >= 10


def test_demod_2_0_msps_and_8_msps():
    for fs in (2000000, 8000000):
        got, sent, t = run_demod(fs)
        assert got >= 0.9 * sent, (fs, got, sent)


def test_noise_only_makes_no_aircraft():
    rng = np.random.default_rng(1)
    d = adsb.Demod()
    t = new_tracker()
    fs = 2400000
    for _ in range(40):
        iq = (rng.standard_normal(120000) + 1j * rng.standard_normal(120000)).astype(np.complex64) * 0.01
        mag = np.abs(iq - iq.mean()).astype(np.float32)
        for data, crc, db in d.process(mag, fs):
            t.raw(data, crc, db, time.time())
    assert not t.ac


def test_performance():
    d = adsb.Demod()
    traffic = adsb_sim.SimTraffic()
    iq, _ = synth(2400000, 2.0, traffic)
    blocks = [np.abs(iq[i:i + 120000] - iq[i:i + 120000].mean()).astype(np.float32) for i in range(0, len(iq) - 120000, 120000)]
    t0 = time.perf_counter()
    for b in blocks:
        d.process(b, 2400000)
    per_block = (time.perf_counter() - t0) / len(blocks)
    print(f"   {per_block * 1000:.1f} ms per 50 ms block")
    assert per_block < 0.04


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print("FAIL", name, repr(e))
                fails += 1
    sys.exit(1 if fails else 0)
