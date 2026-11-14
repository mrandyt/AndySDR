"""ADS-B / Mode S aircraft tracking (1090 MHz), built in – no extra program needed.

The radio thread hands every block of IQ samples to :class:`Adsb` while the ADS-B decoder is
running (hardware centred on 1090 MHz).  A worker thread then

  1. turns the IQ into a 2 MS/s amplitude stream (works with any receiver that runs at 2 MS/s or more),
  2. finds Mode S preambles and slices the 56 / 112 bit messages,
  3. checks the 24-bit CRC (single-bit errors in DF17 are repaired for aircraft already known),
  4. decodes identification, position (CPR), altitude, speed, heading, vertical rate and squawk,
  5. keeps the aircraft list, trails, range statistics and a daily CSV log, and
  6. optionally re-broadcasts the messages as SBS/BaseStation (port 30003) and raw AVR (port 30002)
     so other programs (Virtual Radar Server, tar1090, PlanePlotter ...) can use them too.

Every update reaches the GUI as an "adsb" event once a second.
"""
import collections
import csv
import io
import logging
import math
import os
import queue
import socket
import threading
import time

import numpy as np

from .config import data_dir

log = logging.getLogger("andysdr.adsb")

ADSB_FREQ = 1090000000
RATE = 2000000                 # Hz – the amplitude stream the demodulator works on
MSG_SAMPLES = 240              # preamble (16) + 112 bits * 2 samples
KEEP_SECONDS = 300             # aircraft not heard for this long are dropped
LIVE_SECONDS = 90              # "current" aircraft in the GUI
TRAIL_POINTS = 40
CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_###############0123456789######"

# --------------------------------------------------------------------------- CRC-24
_GEN = 0x1FFF409


def crc24(value, nbits):
    """Remainder of an nbits-long message (parity field included) divided by the Mode S polynomial."""
    reg = value
    for i in range(nbits - 1, 23, -1):
        if (reg >> i) & 1:
            reg ^= _GEN << (i - 24)
    return reg & 0xFFFFFF


def _syndromes(nbits):
    return np.array([crc24(1 << (nbits - 1 - p), nbits) for p in range(nbits)], np.uint32)


SYN112 = _syndromes(112)
SYN56 = _syndromes(56)
FIX112 = {int(s): p for p, s in enumerate(SYN112)}      # single-bit error lookup


def crc_of_bytes(b):
    return crc24(int.from_bytes(b, "big"), len(b) * 8)


# --------------------------------------------------------------------------- demodulator
class Demod:
    """IQ amplitude -> two 2 MS/s streams (half a sample apart) -> raw Mode S candidates.

    A 0.5 us pulse that lands between two 2 MS/s samples is smeared over both and fails the strict
    preamble test, so the stream is also built shifted by half a sample; whichever alignment is
    closer to the pulses wins and duplicates are dropped.
    Result items are (bytes, crc remainder, level dBFS)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.fs = 0
        self.buf = np.zeros(0, np.float64)
        self.pos = 0.0
        self.carry = [np.zeros(0, np.float32), np.zeros(0, np.float32)]
        self.base = 0              # 2 MS/s samples already consumed (start of the carry)
        self.recent = {}           # frame bytes -> absolute sample position (duplicate filter)

    def streams(self, mag, fs):
        """Box-car average the amplitude to 4 MS/s, then build the two 2 MS/s phases from it."""
        if fs != self.fs:
            self.reset()
            self.fs = fs
        step = fs / (2.0 * RATE)
        buf = np.concatenate((self.buf, mag)) if len(self.buf) else mag.astype(np.float64)
        n4 = int((len(buf) - self.pos) / step)
        n2 = (n4 - 2) // 2
        if n2 <= 0:
            self.buf = buf
            return None, None
        cs = np.concatenate(([0.0], np.cumsum(buf)))
        edges = self.pos + step * np.arange(2 * n2 + 3)
        o4 = np.diff(np.interp(edges, np.arange(len(cs)), cs)) / step         # 2*n2+2 samples of 0.25 us
        a = ((o4[0:2 * n2:2] + o4[1:2 * n2 + 1:2]) * 0.5).astype(np.float32)
        b = ((o4[1:2 * n2 + 1:2] + o4[2:2 * n2 + 2:2]) * 0.5).astype(np.float32)
        newpos = self.pos + step * 2 * n2
        drop = int(newpos)
        self.buf = buf[drop:]
        self.pos = newpos - drop
        return a, b

    def process(self, mag, fs):
        """Returns a list of (bytes, crc, dbfs) for every plausible Mode S frame in this block."""
        a, b = self.streams(mag, fs)
        if a is None:
            return []
        found = []
        n_new = len(a)
        for ph, s in enumerate((a, b)):
            m = np.concatenate((self.carry[ph], s)) if len(self.carry[ph]) else s
            n = len(m) - MSG_SAMPLES
            if n <= 0:
                self.carry[ph] = m
                continue
            self.carry[ph] = m[n:]
            origin = self.base - (len(m) - n_new)
            for start, data, crc, db in self._detect(m, n):
                found.append((origin + start, data, crc, db))
        self.base += n_new
        found.sort(key=lambda x: x[0])
        out = []
        for pos, data, crc, db in found:
            last = self.recent.get(data)
            if last is not None and abs(pos - last) < 300:
                continue
            self.recent[data] = pos
            out.append((data, crc, db))
        if len(self.recent) > 400:
            lim = self.base - 2000
            self.recent = {k: v for k, v in self.recent.items() if v > lim}
        return out

    def _detect(self, m, n):
        med = float(np.median(m[:n])) or 1e-9
        m0, m1, m2, m3, m4, m5, m6 = (m[i:i + n] for i in range(7))
        m7, m8, m9 = m[7:7 + n], m[8:8 + n], m[9:9 + n]
        cond = (m0 > m1) & (m1 < m2) & (m2 > m3) & (m3 < m0) & (m4 < m0) & (m5 < m0) & (m6 < m0) \
            & (m7 > m8) & (m8 < m9) & (m9 > m6)
        thr = 2.0 * med
        cond &= (m0 > thr) & (m2 > thr) & (m7 > thr) & (m9 > thr)
        high = (m0 + m2 + m7 + m9) / 6.0
        cond &= (m4 < high) & (m5 < high)
        cond &= (m[11:11 + n] < high) & (m[12:12 + n] < high) & (m[13:13 + n] < high) & (m[14:14 + n] < high)
        idx = np.nonzero(cond)[0]
        if len(idx) == 0:
            return []
        if len(idx) > 600:                                   # a very noisy / overloaded block
            idx = np.sort(idx[np.argsort(-high[idx])[:600]])
        base = idx[:, None] + 16 + 2 * np.arange(112)[None, :]
        bits = (m[base] > m[base + 1])
        df = (bits[:, 0].astype(np.int32) << 4) | (bits[:, 1].astype(np.int32) << 3) | \
             (bits[:, 2].astype(np.int32) << 2) | (bits[:, 3].astype(np.int32) << 1) | bits[:, 4]
        long_msg = df >= 16
        sel = np.nonzero(np.isin(df, (0, 4, 5, 11, 16, 17, 18, 20, 21)))[0]
        if len(sel) == 0:
            return []
        b112 = bits[sel]
        crc_long = np.bitwise_xor.reduce(np.where(b112, SYN112[None, :], np.uint32(0)), axis=1)
        crc_short = np.bitwise_xor.reduce(np.where(b112[:, :56], SYN56[None, :], np.uint32(0)), axis=1)
        out, last_end = [], -1
        for k, c in enumerate(sel):
            start = int(idx[c])
            if start < last_end:
                continue
            is_long = bool(long_msg[c])
            n_bits = 112 if is_long else 56
            crc = int(crc_long[k] if is_long else crc_short[k])
            d = int(df[c])
            # cheap early reject: DF17/18 must be clean (or one bit out); DF11 nearly clean
            if d in (17, 18):
                if crc != 0 and crc not in FIX112:
                    continue
            elif d == 11 and crc >= 128:
                continue
            data = np.packbits(b112[k, :n_bits]).tobytes()
            lvl = float(high[c]) * 6.0 / 4.0
            out.append((start, data, crc, 20.0 * math.log10(max(lvl, 1e-6))))
            if d in (11, 17, 18):                          # frames that look real: skip their own length
                last_end = start + (MSG_SAMPLES if is_long else 16 + 112)
        return out


# --------------------------------------------------------------------------- field decoding
def mode_a_to_c(a):
    """Gillham code (Mode A bit layout) -> altitude in hundreds of feet, or None."""
    if (a & 0xFFFF8889) or (a & 0x00F0) == 0:
        return None
    hundreds = 0
    if a & 0x0010: hundreds ^= 0x007
    if a & 0x0020: hundreds ^= 0x003
    if a & 0x0040: hundreds ^= 0x001
    if (hundreds & 5) == 5: hundreds ^= 2
    if hundreds > 5:
        return None
    five = 0
    if a & 0x0002: five ^= 0x0FF
    if a & 0x0004: five ^= 0x07F
    if a & 0x1000: five ^= 0x03F
    if a & 0x2000: five ^= 0x01F
    if a & 0x4000: five ^= 0x00F
    if a & 0x0100: five ^= 0x007
    if a & 0x0200: five ^= 0x003
    if a & 0x0400: five ^= 0x001
    if five & 1:
        hundreds = 6 - hundreds
    return five * 5 + hundreds - 13


def id13_to_modea(f):
    a = 0
    if f & 0x1000: a |= 0x0010
    if f & 0x0800: a |= 0x1000
    if f & 0x0400: a |= 0x0020
    if f & 0x0200: a |= 0x2000
    if f & 0x0100: a |= 0x0040
    if f & 0x0080: a |= 0x4000
    if f & 0x0020: a |= 0x0100
    if f & 0x0008: a |= 0x0200
    if f & 0x0004: a |= 0x0002
    if f & 0x0002: a |= 0x0400
    if f & 0x0001: a |= 0x0004
    return a


def squawk_from_id13(f):
    return "%04x" % id13_to_modea(f & 0x1FFF)


def alt_ac13(msg):
    """Altitude (ft) from the 13-bit AC field of DF0/4/16/20, or None."""
    m_bit, q_bit = msg[3] & 0x40, msg[3] & 0x10
    if m_bit:
        return None                                        # metric altitude – not used in practice
    if q_bit:
        n = ((msg[2] & 31) << 6) | ((msg[3] & 0x80) >> 2) | ((msg[3] & 0x20) >> 1) | (msg[3] & 15)
        return n * 25 - 1000
    h = mode_a_to_c(id13_to_modea(((msg[2] << 8) | msg[3]) & 0x1FFF))
    return None if h is None or h < -12 else h * 100


def alt_ac12(msg):
    """Altitude (ft) from the 12-bit field of airborne position messages."""
    if msg[5] & 1:
        n = ((msg[5] >> 1) << 4) | ((msg[6] & 0xF0) >> 4)
        return n * 25 - 1000
    ac12 = (msg[5] << 4) | (msg[6] >> 4)
    n13 = ((ac12 & 0x0FC0) << 1) | (ac12 & 0x003F)
    h = mode_a_to_c(id13_to_modea(n13))
    return None if h is None or h < -12 else h * 100


def surface_speed(mov):
    if mov == 0 or mov > 124:
        return None
    if mov == 1: return 0.0
    if mov <= 8: return 0.125 + (mov - 2) * 0.125
    if mov <= 12: return 1.0 + (mov - 9) * 0.25
    if mov <= 38: return 2.0 + (mov - 13) * 0.5
    if mov <= 93: return 15.0 + (mov - 39)
    if mov <= 108: return 70.0 + (mov - 94) * 2
    if mov <= 123: return 100.0 + (mov - 109) * 5
    return 175.0


# --- compact position reporting
def _nl(lat):
    lat = abs(lat)
    if lat < 1e-9: return 59
    if abs(lat - 87) < 1e-9: return 2
    if lat > 87: return 1
    return int(math.floor(2 * math.pi / math.acos(1 - (1 - math.cos(math.pi / 30)) / math.cos(math.radians(lat)) ** 2)))


def cpr_global(lat0, lon0, lat1, lon1, last_odd):
    """Airborne global decode from an even (0) and odd (1) frame. Returns (lat, lon) or None."""
    j = math.floor((59 * lat0 - 60 * lat1) / 131072.0 + 0.5)
    r0 = (360.0 / 60) * ((j % 60) + lat0 / 131072.0)
    r1 = (360.0 / 59) * ((j % 59) + lat1 / 131072.0)
    if r0 >= 270: r0 -= 360
    if r1 >= 270: r1 -= 360
    if _nl(r0) != _nl(r1):
        return None
    if last_odd:
        ni = max(_nl(r1) - 1, 1)
        m = math.floor((lon0 * (_nl(r1) - 1) - lon1 * _nl(r1)) / 131072.0 + 0.5)
        lon, lat = (360.0 / ni) * ((m % ni) + lon1 / 131072.0), r1
    else:
        ni = max(_nl(r0), 1)
        m = math.floor((lon0 * (_nl(r0) - 1) - lon1 * _nl(r0)) / 131072.0 + 0.5)
        lon, lat = (360.0 / ni) * ((m % ni) + lon0 / 131072.0), r0
    if lon >= 180: lon -= 360
    if not -90 <= lat <= 90:
        return None
    return lat, lon


def cpr_local(lat_cpr, lon_cpr, odd, ref_lat, ref_lon, surface=False):
    """Decode one frame against a reference position (within 180 NM; 45 NM for surface frames)."""
    zone = 90.0 if surface else 360.0
    dlat = zone / (59 if odd else 60)
    j = math.floor(ref_lat / dlat) + math.floor(0.5 + ((ref_lat % dlat) / dlat) - lat_cpr / 131072.0)
    lat = dlat * (j + lat_cpr / 131072.0)
    dlon = zone / max(_nl(lat) - (1 if odd else 0), 1)
    m = math.floor(ref_lon / dlon) + math.floor(0.5 + ((ref_lon % dlon) / dlon) - lon_cpr / 131072.0)
    lon = dlon * (m + lon_cpr / 131072.0)
    if lon >= 180: lon -= 360
    if lon < -180: lon += 360
    return lat, lon


def distance_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# --------------------------------------------------------------------------- who is it?
COUNTRIES = [
    (0x100000, 0x1FFFFF, "Russia"), (0x300000, 0x33FFFF, "Italy"), (0x340000, 0x37FFFF, "Spain"),
    (0x380000, 0x3BFFFF, "France"), (0x3C0000, 0x3FFFFF, "Germany"), (0x400000, 0x43FFFF, "United Kingdom"),
    (0x440000, 0x447FFF, "Austria"), (0x448000, 0x44FFFF, "Belgium"), (0x450000, 0x457FFF, "Bulgaria"),
    (0x458000, 0x45FFFF, "Denmark"), (0x460000, 0x467FFF, "Finland"), (0x468000, 0x46FFFF, "Greece"),
    (0x470000, 0x477FFF, "Hungary"), (0x478000, 0x47FFFF, "Norway"), (0x480000, 0x487FFF, "Netherlands"),
    (0x488000, 0x48FFFF, "Poland"), (0x490000, 0x497FFF, "Portugal"), (0x498000, 0x49FFFF, "Czechia"),
    (0x4A0000, 0x4A7FFF, "Romania"), (0x4A8000, 0x4AFFFF, "Sweden"), (0x4B0000, 0x4B7FFF, "Switzerland"),
    (0x4B8000, 0x4BFFFF, "Turkey"), (0x4CA000, 0x4CAFFF, "Ireland"), (0x4D0000, 0x4D03FF, "Luxembourg"),
    (0x710000, 0x717FFF, "Saudi Arabia"), (0x718000, 0x71FFFF, "South Korea"), (0x738000, 0x73FFFF, "Israel"),
    (0x768000, 0x76FFFF, "Singapore"), (0x780000, 0x7BFFFF, "China"), (0x7C0000, 0x7FFFFF, "Australia"),
    (0x800000, 0x83FFFF, "India"), (0x840000, 0x87FFFF, "Japan"), (0x896000, 0x896FFF, "UAE"),
    (0xA00000, 0xAFFFFF, "United States"), (0xC00000, 0xC3FFFF, "Canada"), (0xC80000, 0xC87FFF, "New Zealand"),
]

AIRLINES = {
    "BAW": "British Airways", "EZY": "easyJet", "EJU": "easyJet Europe", "RYR": "Ryanair", "RUK": "Ryanair UK",
    "VIR": "Virgin Atlantic", "TOM": "TUI Airways", "TUI": "TUI fly", "EXS": "Jet2", "LOG": "Loganair",
    "WZZ": "Wizz Air", "WUK": "Wizz Air UK", "KLM": "KLM", "DLH": "Lufthansa", "AFR": "Air France",
    "IBE": "Iberia", "VLG": "Vueling", "SAS": "SAS", "FIN": "Finnair", "NAX": "Norwegian",
    "NOZ": "Norwegian", "SWR": "Swiss", "AUA": "Austrian", "BEL": "Brussels Airlines", "TAP": "TAP Air Portugal",
    "EIN": "Aer Lingus", "CFE": "BA CityFlyer", "UAL": "United", "AAL": "American", "DAL": "Delta",
    "ACA": "Air Canada", "UAE": "Emirates", "QTR": "Qatar Airways", "ETD": "Etihad", "THY": "Turkish Airlines",
    "SIA": "Singapore Airlines", "CPA": "Cathay Pacific", "QFA": "Qantas", "ANA": "ANA", "JAL": "Japan Airlines",
    "KAL": "Korean Air", "AIC": "Air India", "ELY": "El Al", "MSR": "EgyptAir", "SVA": "Saudia",
    "FDX": "FedEx", "UPS": "UPS", "DHK": "DHL Air UK", "BCS": "European Air Transport", "GEC": "Lufthansa Cargo",
    "CLX": "Cargolux", "RRR": "Royal Air Force", "EWG": "Eurowings", "CLH": "Lufthansa CityLine",
    "SXS": "SunExpress", "PGT": "Pegasus", "AEE": "Aegean", "LOT": "LOT Polish", "CSA": "Czech Airlines",
    "ICE": "Icelandair", "TVS": "Smartwings", "JBU": "JetBlue", "SWA": "Southwest", "ASA": "Alaska",
    "BEE": "Flybe", "AZA": "ITA Airways", "ITY": "ITA Airways", "TRA": "Transavia", "TVF": "Transavia France",
    "DLA": "Air Dolomiti", "EDW": "Edelweiss", "CND": "Condor", "BMS": "Blue Islands", "SHT": "BA Shuttle",
}


def country_of(icao):
    for lo, hi, name in COUNTRIES:
        if lo <= icao <= hi:
            return name
    return ""


# --------------------------------------------------------------------------- aircraft tracker
class Aircraft:
    __slots__ = ("icao", "flight", "cat", "squawk", "alt", "gs", "trk", "vr", "lat", "lon", "ground", "emerg",
                 "msgs", "first", "seen", "seen_pos", "rssi", "even", "odd", "trail", "dist", "brg", "last_log")

    def __init__(self, icao, now):
        self.icao = icao
        self.flight = self.cat = self.squawk = ""
        self.alt = self.gs = self.trk = self.vr = self.lat = self.lon = None
        self.ground = False
        self.emerg = 0
        self.msgs = 0
        self.first = self.seen = now
        self.seen_pos = 0.0
        self.rssi = -99.0
        self.even = self.odd = None            # (lat_cpr, lon_cpr, time)
        self.trail = collections.deque(maxlen=TRAIL_POINTS)
        self.dist = self.brg = None
        self.last_log = 0.0

    def to_dict(self, now, with_trail=False):
        hexid = "%06X" % self.icao
        flt = self.flight.strip()
        d = {"icao": hexid, "flight": flt, "cat": self.cat, "squawk": self.squawk, "alt": self.alt,
             "gs": None if self.gs is None else round(self.gs), "trk": None if self.trk is None else round(self.trk, 1),
             "vr": self.vr, "lat": None if self.lat is None else round(self.lat, 5),
             "lon": None if self.lon is None else round(self.lon, 5), "ground": self.ground,
             "emerg": self.emerg, "msgs": self.msgs, "seen": round(now - self.seen, 1),
             "seen_pos": round(now - self.seen_pos, 1) if self.seen_pos else None, "rssi": round(self.rssi, 1),
             "country": country_of(self.icao), "airline": AIRLINES.get(flt[:3], "") if len(flt) > 3 and flt[:3].isalpha() else "",
             "dist": None if self.dist is None else round(self.dist, 1),
             "brg": None if self.brg is None else round(self.brg)}
        if with_trail:
            d["trail"] = [[round(a, 4), round(b, 4), c] for a, b, c in self.trail]
        return d


class Tracker:
    def __init__(self, settings, emit):
        self.s = settings
        self.emit = emit
        self.lock = threading.RLock()
        self.ac = {}
        self.known = {}                       # icao -> last time a clean frame was seen (for DF0/4/5/20/21)
        self.total = 0
        self.positions = 0
        self.corrected = 0
        self.max_range = 0.0
        self.recent = collections.deque()     # timestamps for msgs/s
        self.sinks = []                       # callables(kind, msg_bytes, aircraft, now)
        self.log_dir = os.path.join(data_dir(), "adsb")

    # ---- reference position of the receiver
    def ref(self):
        lat, lon = self.s.get("adsb_lat"), self.s.get("adsb_lon")
        try:
            if lat is None or lon is None or lat == "" or lon == "":
                return None
            lat, lon = float(lat), float(lon)
            return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None
        except (TypeError, ValueError):
            return None

    def clear(self):
        with self.lock:
            self.ac.clear()
            self.known.clear()
            self.total = self.positions = self.corrected = 0
            self.max_range = 0.0
            self.recent.clear()

    # ---- main entry
    def raw(self, data, crc, dbfs, now):
        df = data[0] >> 3
        icao, fixed = None, False
        if df in (17, 18):
            if crc != 0:
                p = FIX112.get(crc)
                if p is None:
                    return
                b = bytearray(data)
                b[p // 8] ^= 0x80 >> (p % 8)
                data, fixed = bytes(b), True
            icao = int.from_bytes(data[1:4], "big")
            if fixed and icao not in self.known:
                return
            if df == 18 and (data[0] & 7) not in (0, 1):
                return
        elif df == 11:
            if crc >= 128:
                return
            icao = int.from_bytes(data[1:4], "big")
        else:
            icao = crc
            if icao not in self.known:
                return
        with self.lock:
            self.total += 1
            if fixed:
                self.corrected += 1
            self.recent.append(now)
            if df in (11, 17, 18):
                self.known[icao] = now
            a = self.ac.get(icao)
            if a is None:
                if df not in (11, 17, 18):
                    return
                a = self.ac[icao] = Aircraft(icao, now)
            a.msgs += 1
            a.seen = now
            a.rssi = dbfs if a.msgs == 1 else a.rssi * 0.9 + dbfs * 0.1
            kind = None
            try:
                if df in (17, 18):
                    kind = self._adsb(a, data, now)
                elif df in (0, 4, 16, 20):
                    alt = alt_ac13(data)
                    if alt is not None:
                        a.alt, kind = alt, "alt"
                    if df == 20 and data[4] == 0x20:      # BDS 2,0 aircraft identification
                        v = int.from_bytes(data[5:11], "big")
                        fl = "".join(CHARSET[(v >> (42 - 6 * i)) & 63] for i in range(8)).replace("_", " ").replace("#", "").strip()
                        if fl:
                            a.flight, kind = fl, "ident"
                elif df in (5, 21):
                    a.squawk = squawk_from_id13(((data[2] << 8) | data[3]) & 0x1FFF)
                    kind = "squawk"
            except Exception:                                 # a bad frame must never stop the decoder
                log.exception("decode")
                return
            for fn in self.sinks:
                try:
                    fn(kind, data, a, now)
                except Exception:
                    pass

    def _adsb(self, a, m, now):
        tc = m[4] >> 3
        if 1 <= tc <= 4:
            v = int.from_bytes(m[5:11], "big")
            fl = "".join(CHARSET[(v >> (42 - 6 * i)) & 63] for i in range(8))
            a.flight = fl.replace("_", " ").replace("#", "").strip() or a.flight
            a.cat = "ABCD"[(4 - tc)] + str(m[4] & 7) if tc in (1, 2, 3, 4) else ""
            return "ident"
        if 5 <= tc <= 8 or 9 <= tc <= 18 or 20 <= tc <= 22:
            surface = tc <= 8
            if surface:
                a.ground = True
                mov = ((m[4] & 7) << 4) | (m[5] >> 4)
                sp = surface_speed(mov)
                if sp is not None:
                    a.gs = sp
                if m[5] & 0x08:
                    a.trk = (((m[5] & 7) << 4) | (m[6] >> 4)) * 360.0 / 128
            else:
                a.ground = False
                if tc <= 18:
                    alt = alt_ac12(m)
                    if alt is not None:
                        a.alt = alt
            odd = bool(m[6] & 4)
            lat_c = ((m[6] & 3) << 15) | (m[7] << 7) | (m[8] >> 1)
            lon_c = ((m[8] & 1) << 16) | (m[9] << 8) | m[10]
            if odd:
                a.odd = (lat_c, lon_c, now)
            else:
                a.even = (lat_c, lon_c, now)
            self._position(a, lat_c, lon_c, odd, surface, now)
            return "pos"
        if tc == 19:
            st = m[4] & 7
            if st in (1, 2):
                ew = ((m[5] & 3) << 8) | m[6]
                ns = ((m[7] & 0x7F) << 3) | (m[8] >> 5)
                if ew and ns:
                    ew, ns = ew - 1, ns - 1
                    if m[5] & 4: ew = -ew
                    if m[7] & 0x80: ns = -ns
                    k = 4 if st == 2 else 1
                    a.gs = math.hypot(ew, ns) * k
                    a.trk = (math.degrees(math.atan2(ew, ns)) + 360) % 360
            elif st in (3, 4):
                if m[5] & 4:
                    a.trk = (((m[5] & 3) << 8) | m[6]) * 360.0 / 1024
                sp = ((m[7] & 0x7F) << 3) | (m[8] >> 5)
                if sp:
                    a.gs = (sp - 1) * (4 if st == 4 else 1)
            vr = ((m[8] & 7) << 6) | (m[9] >> 2)
            if vr:
                a.vr = (vr - 1) * 64 * (-1 if m[8] & 8 else 1)
            return "vel"
        if tc == 28 and (m[4] & 7) == 1:
            a.squawk = squawk_from_id13(((m[5] << 8) | m[6]) & 0x1FFF)
            a.emerg = m[5] >> 5
            return "squawk"
        return None

    def _position(self, a, lat_c, lon_c, odd, surface, now):
        res = None
        ref = self.ref()
        if not surface and a.even and a.odd and abs(a.even[2] - a.odd[2]) <= 10:
            res = cpr_global(a.even[0], a.even[1], a.odd[0], a.odd[1], odd)
            if res and ref and distance_nm(ref[0], ref[1], res[0], res[1]) > 450:
                res = None                    # not plausible for a receiver here – probably a bad decode
        if res is None:
            anchor = None
            if a.lat is not None and now - a.seen_pos < 120:
                anchor = (a.lat, a.lon)
            elif ref:
                anchor = ref
            if anchor:
                res = cpr_local(lat_c, lon_c, odd, anchor[0], anchor[1], surface)
                if ref and not a.lat and distance_nm(ref[0], ref[1], res[0], res[1]) > (180 if not surface else 45):
                    res = None
        if res is None:
            return
        lat, lon = res
        if a.lat is not None and a.seen_pos:
            dt = max(now - a.seen_pos, 0.05)
            jump = distance_nm(a.lat, a.lon, lat, lon)
            if jump > 5 and jump / dt * 3600 > 2000:          # faster than any aircraft: bad frame
                return
        a.lat, a.lon, a.seen_pos = lat, lon, now
        self.positions += 1
        if not a.trail or abs(a.trail[-1][0] - lat) + abs(a.trail[-1][1] - lon) > 0.0015:
            a.trail.append((lat, lon, a.alt))
        if ref:
            a.dist = distance_nm(ref[0], ref[1], lat, lon)
            a.brg = bearing_deg(ref[0], ref[1], lat, lon)
            if a.dist > self.max_range and a.dist < 450:
                self.max_range = a.dist
        self._log(a, now)

    # ---- daily CSV log (one line per aircraft every 30 s)
    def _log(self, a, now):
        if not self.s.get("adsb_log", True) or now - a.last_log < 30:
            return
        a.last_log = now
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            path = os.path.join(self.log_dir, time.strftime("adsb_%Y-%m-%d.csv", time.localtime(now)))
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "icao", "callsign", "lat", "lon", "alt_ft", "gs_kt", "track", "vrate_fpm", "squawk"])
                w.writerow([time.strftime("%H:%M:%S", time.localtime(now)), "%06X" % a.icao, a.flight.strip(),
                            round(a.lat, 5), round(a.lon, 5), a.alt, None if a.gs is None else round(a.gs),
                            None if a.trk is None else round(a.trk), a.vr, a.squawk])
        except OSError:
            pass

    # ---- views
    def prune(self, now):
        with self.lock:
            for k in [k for k, a in self.ac.items() if now - a.seen > KEEP_SECONDS]:
                del self.ac[k]
            for k in [k for k, t in self.known.items() if now - t > KEEP_SECONDS]:
                del self.known[k]
            while self.recent and now - self.recent[0] > 10:
                self.recent.popleft()

    def stats(self, now):
        with self.lock:
            live = [a for a in self.ac.values() if now - a.seen <= LIVE_SECONDS]
            return {"aircraft": len(live), "with_pos": sum(1 for a in live if a.lat is not None and now - a.seen_pos <= LIVE_SECONDS),
                    "msgs": self.total, "rate": round(len(self.recent) / 10.0, 1), "positions": self.positions,
                    "corrected": self.corrected, "max_range": round(self.max_range, 1)}

    def snapshot(self, now, with_trail=False):
        with self.lock:
            return [a.to_dict(now, with_trail) for a in sorted(self.ac.values(), key=lambda x: -x.seen)
                    if now - a.seen <= KEEP_SECONDS]

    def export_csv(self, now):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["icao", "callsign", "airline", "country", "lat", "lon", "alt_ft", "gs_kt", "track", "vrate_fpm",
                    "squawk", "distance_nm", "bearing", "messages", "seen_s_ago", "rssi_dbfs"])
        for d in self.snapshot(now):
            w.writerow([d["icao"], d["flight"], d["airline"], d["country"], d["lat"], d["lon"], d["alt"], d["gs"],
                        d["trk"], d["vr"], d["squawk"], d["dist"], d["brg"], d["msgs"], d["seen"], d["rssi"]])
        return buf.getvalue()

    def aircraft_json(self, now):
        """dump1090-compatible aircraft.json (readable by tar1090, Virtual Radar Server, etc.)."""
        out = []
        for d in self.snapshot(now):
            if d["seen"] > LIVE_SECONDS:
                continue
            e = {"hex": d["icao"].lower(), "messages": d["msgs"], "seen": d["seen"], "rssi": d["rssi"]}
            if d["flight"]: e["flight"] = d["flight"].ljust(8)
            if d["alt"] is not None: e["alt_baro"] = "ground" if d["ground"] else d["alt"]
            if d["gs"] is not None: e["gs"] = d["gs"]
            if d["trk"] is not None: e["track"] = d["trk"]
            if d["vr"] is not None: e["baro_rate"] = d["vr"]
            if d["squawk"]: e["squawk"] = d["squawk"]
            if d["lat"] is not None:
                e["lat"], e["lon"], e["seen_pos"] = d["lat"], d["lon"], d["seen_pos"]
            if d["cat"]: e["category"] = d["cat"]
            out.append(e)
        return {"now": round(now, 1), "messages": self.total, "aircraft": out}


# --------------------------------------------------------------------------- re-broadcast (SBS / raw)
class FeedServer:
    """Tiny TCP broadcast server: every connected client gets each line as it happens."""

    def __init__(self, port, name):
        self.port, self.name = port, name
        self.sock = None
        self.clients = []
        self.lock = threading.Lock()
        self.running = False

    def start(self):
        if self.running:
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.listen(5)
        except OSError as e:
            log.warning("%s feed port %s: %s", self.name, self.port, e)
            return False
        self.sock, self.running = s, True
        threading.Thread(target=self._accept, name=f"adsb-{self.name}", daemon=True).start()
        return True

    def _accept(self):
        while self.running:
            try:
                c, _ = self.sock.accept()
                c.settimeout(0.5)
                with self.lock:
                    self.clients.append(c)
            except OSError:
                break

    def send(self, line):
        if not self.clients:
            return
        data = line.encode("ascii", "replace")
        with self.lock:
            for c in list(self.clients):
                try:
                    c.sendall(data)
                except OSError:
                    self.clients.remove(c)
                    try:
                        c.close()
                    except OSError:
                        pass

    def stop(self):
        self.running = False
        with self.lock:
            for c in self.clients:
                try:
                    c.close()
                except OSError:
                    pass
            self.clients = []
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def _sbs_time(now):
    t = time.localtime(now)
    ms = int((now % 1) * 1000)
    return time.strftime("%Y/%m/%d", t), time.strftime("%H:%M:%S", t) + ".%03d" % ms


# --------------------------------------------------------------------------- the engine used by the radio
class Adsb:
    def __init__(self, settings, emit):
        self.s = settings
        self.emit = emit
        self.active = False
        self.demod = Demod()
        self.tracker = Tracker(settings, emit)
        self.q = queue.Queue(maxsize=80)
        self.dropped = 0
        self.sbs = FeedServer(int(settings.get("adsb_sbs_port", 30003)), "sbs")
        self.raw = FeedServer(int(settings.get("adsb_raw_port", 30002)), "raw")
        self.tracker.sinks.append(self._feed_out)
        self._last_emit = 0.0
        self._last_note = 0.0
        self.thread = threading.Thread(target=self._work, name="adsb", daemon=True)
        self.thread.start()
        if self.s.get("adsb_share"):
            self.set_share(True)

    # ---- control
    def start(self):
        self.demod.reset()
        self.active = True
        self.emit("adsb_cfg", self.config())

    def stop(self):
        self.active = False
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.emit("adsb_cfg", self.config())

    def shutdown(self):
        self.active = False
        self.sbs.stop()
        self.raw.stop()

    def clear(self):
        self.tracker.clear()
        self._emit_now()

    def set_position(self, lat, lon):
        try:
            if lat in (None, "") or lon in (None, ""):
                self.s["adsb_lat"] = self.s["adsb_lon"] = None
            else:
                lat, lon = float(lat), float(lon)
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
                self.s["adsb_lat"], self.s["adsb_lon"] = round(lat, 5), round(lon, 5)
        except (TypeError, ValueError):
            self.emit("event", {"level": "warn", "text": "ADS-B: latitude / longitude not understood"})
            return
        self.s.save()
        self.emit("adsb_cfg", self.config())

    def set_share(self, on):
        self.s["adsb_share"] = bool(on)
        self.s.save()
        if on:
            ok1 = self.sbs.start()
            ok2 = self.raw.start()
            if not (ok1 and ok2):
                self.emit("event", {"level": "warn", "text": "ADS-B feed: a port is already in use – see Receiver tab"})
        else:
            self.sbs.stop()
            self.raw.stop()
        self.emit("adsb_cfg", self.config())

    def set_option(self, key, value):
        if key == "log":
            self.s["adsb_log"] = bool(value)
            self.s.save()
            self.emit("adsb_cfg", self.config())

    def config(self):
        ref = self.tracker.ref()
        return {"active": self.active, "lat": ref[0] if ref else None, "lon": ref[1] if ref else None,
                "share": bool(self.s.get("adsb_share")), "sbs_port": int(self.s.get("adsb_sbs_port", 30003)),
                "raw_port": int(self.s.get("adsb_raw_port", 30002)), "log": bool(self.s.get("adsb_log", True)),
                "log_dir": self.tracker.log_dir}

    def snapshot(self):
        now = time.time()
        return {"cfg": self.config(), "aircraft": self.tracker.snapshot(now, True), "stats": self.tracker.stats(now),
                "t": now}

    # ---- sample input (radio thread)
    def feed(self, iq, fs):
        if not self.active:
            return
        mag = np.abs(iq - iq.mean()).astype(np.float32)
        try:
            self.q.put_nowait((mag, int(fs)))
        except queue.Full:
            self.dropped += 1

    # ---- worker
    def _work(self):
        while True:
            try:
                mag, fs = self.q.get(timeout=0.5)
            except queue.Empty:
                mag = None
            now = time.time()
            if mag is not None and self.active:
                try:
                    for data, crc, dbfs in self.demod.process(mag, fs):
                        self.tracker.raw(data, crc, dbfs, now)
                except Exception:
                    log.exception("adsb demod")
            if now - self._last_emit >= 1.0:
                self._emit_now(now)

    def _emit_now(self, now=None):
        now = now or time.time()
        self._last_emit = now
        self.tracker.prune(now)
        if not self.active and not self.tracker.ac:
            return
        st = self.tracker.stats(now)
        st["dropped"] = self.dropped
        self.emit("adsb", {"t": now, "active": self.active, "aircraft": self.tracker.snapshot(now), "stats": st})
        if self.active and now - self._last_note >= 15 and st["aircraft"]:
            self._last_note = now
            self.emit("decoder", {"t": time.strftime("%H:%M:%S"), "decoder": "adsb",
                                  "text": f"ADS-B: {st['aircraft']} aircraft ({st['with_pos']} with position), "
                                          f"{st['rate']} msg/s"})

    # ---- outputs
    def _feed_out(self, kind, data, a, now):
        if self.raw.clients:
            self.raw.send("*" + data.hex().upper() + ";\n")
        if self.sbs.clients and kind:
            d, t = _sbs_time(now)
            hexid = "%06X" % a.icao
            flt = a.flight.strip()
            alt = "" if a.alt is None else str(a.alt)
            gs = "" if a.gs is None else str(round(a.gs))
            trk = "" if a.trk is None else str(round(a.trk))
            lat = "" if a.lat is None else "%.5f" % a.lat
            lon = "" if a.lon is None else "%.5f" % a.lon
            vr = "" if a.vr is None else str(a.vr)
            sq = a.squawk or ""
            g = "-1" if a.ground else "0"
            em = "-1" if a.emerg or sq in ("7500", "7600", "7700") else "0"
            mt = {"ident": 1, "pos": 3, "vel": 4, "alt": 5, "squawk": 6}.get(kind, 8)
            if mt == 1:
                row = [flt, "", "", "", "", "", "", "", "", "", "", ""]
            elif mt == 3:
                row = ["", alt, "", "", lat, lon, "", "", "0", "0", "0", g]
            elif mt == 4:
                row = ["", "", gs, trk, "", "", vr, "", "", "", "", ""]
            elif mt == 5:
                row = ["", alt, "", "", "", "", "", "", "0", "0", "0", g]
            elif mt == 6:
                row = ["", "", "", "", "", "", "", sq, "0", em, "0", ""]
            else:
                row = [""] * 12
            self.sbs.send(f"MSG,{mt},1,1,{hexid},1,{d},{t},{d},{t}," + ",".join(row) + "\r\n")
