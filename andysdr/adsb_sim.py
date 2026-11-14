"""Mode S / ADS-B message encoder + simulated air traffic.

Used by the built-in Simulator receiver (so ADS-B can be tried without hardware) and by the tests.
"""
import math
import random

import numpy as np

from .adsb import CHARSET, _nl, crc24


def with_parity(body11):
    """11 bytes of a DF17 frame -> 14 bytes including the CRC."""
    v = int.from_bytes(body11, "big") << 24
    return body11 + crc24(v, 112).to_bytes(3, "big")


def df17(icao, me):
    return with_parity(bytes([(17 << 3) | 5]) + icao.to_bytes(3, "big") + me)


def me_ident(callsign, cat=3):
    cs = callsign.upper().ljust(8)[:8]
    v = 0
    for ch in cs:
        v = (v << 6) | (CHARSET.index(ch) if ch in CHARSET and ch not in "#_" else 32)
    return bytes([(4 << 3) | cat]) + v.to_bytes(6, "big")


def cpr_encode(lat, lon, odd):
    o = 1 if odd else 0
    dlat = 360.0 / (60 - o)
    yz = int(math.floor(131072 * ((lat % dlat) / dlat) + 0.5))
    rlat = dlat * (yz / 131072.0 + math.floor(lat / dlat))
    dlon = 360.0 / max(_nl(rlat) - o, 1)
    xz = int(math.floor(131072 * ((lon % dlon) / dlon) + 0.5))
    return yz & 0x1FFFF, xz & 0x1FFFF


def me_position(lat, lon, alt_ft, odd, tc=11):
    n = int((alt_ft + 1000) / 25)
    ac12 = ((n & 0x7F0) << 1) | 0x10 | (n & 0xF)
    y, x = cpr_encode(lat, lon, odd)
    return bytes([(tc << 3), (ac12 >> 4) & 0xFF,
                  ((ac12 & 0xF) << 4) | ((1 if odd else 0) << 2) | ((y >> 15) & 3),
                  (y >> 7) & 0xFF, ((y & 0x7F) << 1) | ((x >> 16) & 1), (x >> 8) & 0xFF, x & 0xFF])


def me_velocity(gs_kt, track_deg, vr_fpm):
    ew = gs_kt * math.sin(math.radians(track_deg))
    ns = gs_kt * math.cos(math.radians(track_deg))
    ewv, nsv = int(round(abs(ew))) + 1, int(round(abs(ns))) + 1
    vr = int(round(abs(vr_fpm) / 64)) + 1
    b = bytearray(7)
    b[0] = (19 << 3) | 1
    b[1] = (0x04 if ew < 0 else 0) | ((ewv >> 8) & 3)
    b[2] = ewv & 0xFF
    b[3] = (0x80 if ns < 0 else 0) | ((nsv >> 3) & 0x7F)
    b[4] = ((nsv & 7) << 5) | 0x10 | (0x08 if vr_fpm < 0 else 0) | ((vr >> 6) & 7)
    b[5] = (vr & 0x3F) << 2
    return bytes(b)


def modulate(msg, fs, amp=0.3):
    """Mode S frame -> amplitude envelope sampled at fs (a pulse is 0.5 us)."""
    bits = np.unpackbits(np.frombuffer(msg, np.uint8))
    chips = np.zeros(16 + 2 * len(bits), np.float32)
    for p in (0, 2, 7, 9):
        chips[p] = 1.0
    chips[16::2] = bits
    chips[17::2] = 1 - bits
    # area-average the 0.5 us chips onto the sample grid (what a real receiver's filter does)
    cs = np.concatenate(([0.0], np.cumsum(chips)))
    n = int(len(chips) / 2e6 * fs) + 2
    edges = np.minimum(np.arange(n + 1) * (2e6 / fs), len(chips))
    env = np.diff(np.interp(edges, np.arange(len(cs)), cs)) * (fs / 2e6)
    return (env * amp).astype(np.float32)


class SimTraffic:
    """A handful of aircraft flying about, sending realistic ADS-B message streams."""

    def __init__(self, lat=51.50, lon=-1.00, seed=1090):
        rnd = random.Random(seed)
        self.t = 0.0
        calls = ["BAW123", "EZY45GA", "RYR8KL", "VIR9", "TOM2154", "EXS7MJ", "KLM1023", "DLH4TF", "EIN156", "LOG4AB"]
        self.ac = []
        for i, cs in enumerate(calls):
            ang = rnd.uniform(0, 360)
            dist = rnd.uniform(8, 110)            # NM from the reference point
            self.ac.append({
                "icao": 0x400000 + 0x1234 * (i + 1) + rnd.randint(0, 255),
                "call": cs, "gs": rnd.uniform(160, 480), "trk": rnd.uniform(0, 360),
                "alt": rnd.choice([3200, 6500, 11000, 18500, 24000, 31000, 34000, 38000, 39000]),
                "vr": rnd.choice([0, 0, 0, 1500, -1800, 800]),
                "lat": lat + dist / 60 * math.cos(math.radians(ang)),
                "lon": lon + dist / 60 * math.sin(math.radians(ang)) / math.cos(math.radians(lat)),
                "amp": max(0.02, 0.45 - dist * 0.0035),
                "next_pos": rnd.uniform(0, 0.5), "next_vel": rnd.uniform(0, 0.5), "next_id": rnd.uniform(0, 5),
                "odd": False, "rnd": random.Random(seed + i)})

    def _move(self, a, dt):
        d = a["gs"] * dt / 3600.0                  # NM
        a["lat"] += d / 60 * math.cos(math.radians(a["trk"]))
        a["lon"] += d / 60 * math.sin(math.radians(a["trk"])) / max(math.cos(math.radians(a["lat"])), 0.05)
        a["alt"] = max(500, a["alt"] + a["vr"] * dt / 60)

    def messages(self, t0, t1):
        """All messages sent in [t0, t1): list of (time, 14 bytes, amplitude)."""
        out = []
        for a in self.ac:
            self._move(a, min(t1 - t0, 5.0))
            for k in ("next_pos", "next_vel", "next_id"):      # nothing was running during a gap: don't catch up
                if a[k] < t0:
                    a[k] = t0 + a["rnd"].uniform(0, 0.5)
            while a["next_pos"] < t1:
                tt = max(a["next_pos"], t0)
                a["odd"] = not a["odd"]
                out.append((tt, df17(a["icao"], me_position(a["lat"], a["lon"], a["alt"], a["odd"])), a["amp"]))
                a["next_pos"] += 0.5 + a["rnd"].uniform(-0.05, 0.05)
            while a["next_vel"] < t1:
                out.append((max(a["next_vel"], t0), df17(a["icao"], me_velocity(a["gs"], a["trk"], a["vr"])), a["amp"]))
                a["next_vel"] += 0.5 + a["rnd"].uniform(-0.05, 0.05)
            while a["next_id"] < t1:
                out.append((max(a["next_id"], t0), df17(a["icao"], me_ident(a["call"])), a["amp"]))
                a["next_id"] += 5.0
        out.sort()
        return out
