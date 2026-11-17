"""Receive-only MeshCore with just an SDR (no LoRa board needed).

The radio engine hands this class the raw IQ stream.  It mixes the MeshCore channel to baseband, resamples it to one
sample per LoRa chip, runs the LoRa demodulator (lora.py), parses the MeshCore packets (meshpacket.py) and keeps:

* a node list built from adverts (repeaters, room servers, companions, sensors, with names and positions),
* decoded channel messages (public channel + any #hashtag / keyed channel you add),
* a live packet log and statistics, including packets that were heard but could not be decoded.

Nothing is transmitted.  Direct messages are end-to-end encrypted and are only counted.
"""
import hashlib
import json
import logging
import os
import queue
import threading
import time
from fractions import Fraction

import numpy as np
from scipy import signal

from . import meshpacket as mp
from .config import data_dir
from .lora import LoraDemod

log = logging.getLogger("andysdr.meshsdr")

PRESET = {"freq": 869618000, "bw": 62500, "sf": 8, "cr": 8}
CHUNK_S = 1.0                      # decode every second of new data
MAX_LOG = 300
MAX_MSG = 500


class MeshSdr:
    def __init__(self, settings, emit):
        self.s = settings
        self.emit = emit
        self.running = False
        self.lock = threading.RLock()
        self.q = queue.Queue(maxsize=400)
        self.thread = None
        self.nodes = {}                  # pubkey hex -> dict
        self.messages = []
        self.packets = []
        self.stats = self._new_stats()
        self.channels = {}               # name -> 16-byte secret
        self._dirty = False
        self._last_emit = 0.0
        self._phase = 0.0
        self._seen = []                  # (abs_start, payload hash) of recently decoded frames
        self._nodes_path = os.path.join(data_dir(), "meshsdr_nodes.json")
        self._msg_path = os.path.join(data_dir(), "meshsdr_messages.jsonl")
        self._load()
        self._load_channels()

    # ---------------------------------------------------------------- settings
    def cfg(self):
        c = dict(PRESET)
        c.update(self.s.get("meshsdr") or {})
        for k in ("freq", "bw", "sf", "cr"):
            c[k] = int(c[k])
        return c

    def set_cfg(self, **kw):
        c = dict(self.s.get("meshsdr") or {})
        for k in ("freq", "bw", "sf", "cr"):
            if k in kw and kw[k] not in (None, ""):
                v = float(kw[k])
                if k == "freq" and v < 1e6:
                    v *= 1e6                               # accepted in MHz
                if k == "bw" and v < 1000:
                    v *= 1000                              # accepted in kHz
                c[k] = int(round(v))
        c["sf"] = max(5, min(12, int(c.get("sf", 8))))
        self.s["meshsdr"] = c
        self.s.save()
        self._mark()

    def _load_channels(self):
        self.channels = {"Public": mp.PUBLIC_SECRET}
        for spec in self.s.get("meshsdr_channels") or []:
            sec = mp.channel_secret(spec)
            if sec:
                self.channels[spec if spec.startswith("#") else "key " + hashlib.sha256(sec).hexdigest()[:6]] = sec

    def add_channel(self, spec):
        spec = (spec or "").strip()
        if spec and not spec.startswith("#") and len(spec) not in (24, 32) and spec.lower() != "public":
            spec = "#" + spec
        if not mp.channel_secret(spec):
            return False
        lst = [x for x in (self.s.get("meshsdr_channels") or []) if x != spec]
        lst.append(spec)
        self.s["meshsdr_channels"] = lst
        self.s.save()
        self._load_channels()
        self._mark()
        return True

    def remove_channel(self, spec):
        self.s["meshsdr_channels"] = [x for x in (self.s.get("meshsdr_channels") or []) if x != spec]
        self.s.save()
        self._load_channels()
        self._mark()

    # ---------------------------------------------------------------- storage
    def _new_stats(self):
        return {"started": 0.0, "frames": 0, "bad_crc": 0, "not_mesh": 0, "by_type": {}, "last_frame": 0.0,
                "best_snr": None, "samples": 0}

    def _load(self):
        try:
            with open(self._nodes_path, "r", encoding="utf-8") as f:
                self.nodes = json.load(f)
        except (OSError, ValueError):
            self.nodes = {}
        try:
            with open(self._msg_path, "r", encoding="utf-8") as f:
                self.messages = [json.loads(l) for l in f.readlines()[-MAX_MSG:] if l.strip()]
        except (OSError, ValueError):
            self.messages = []

    def _save_nodes(self):
        try:
            with open(self._nodes_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.nodes, f)
            os.replace(self._nodes_path + ".tmp", self._nodes_path)
        except OSError:
            pass

    def clear(self):
        with self.lock:
            self.nodes, self.messages, self.packets = {}, [], []
            self.stats = self._new_stats()
            if self.running:
                self.stats["started"] = time.time()
        self._save_nodes()
        try:
            os.remove(self._msg_path)
        except OSError:
            pass
        self._mark()

    # ---------------------------------------------------------------- control
    def start(self):
        if self.running:
            return
        self.running = True
        self.stats["started"] = time.time()
        self._phase = 0.0
        self._seen = []
        self.q = queue.Queue(maxsize=400)
        self.thread = threading.Thread(target=self._worker, name="meshsdr", daemon=True)
        self.thread.start()
        self._mark()

    def stop(self):
        if not self.running:
            return
        self.running = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        self._save_nodes()
        self._mark()

    def shutdown(self):
        self.stop()
        self._save_nodes()

    def feed(self, iq, fs, offset):
        """Called by the engine for every block of raw IQ (centre-relative offset of the MeshCore channel in Hz)."""
        if not self.running:
            return
        n = len(iq)
        w = -2 * np.pi * offset / fs
        osc = np.exp(1j * (self._phase + w * np.arange(n))).astype(np.complex64)
        self._phase = (self._phase + w * n) % (2 * np.pi)
        try:
            self.q.put_nowait((iq * osc, fs))
        except queue.Full:
            pass                                        # decoder is behind: drop rather than stall the radio

    # ---------------------------------------------------------------- DSP worker
    def _worker(self):
        from .lora import n_symbols
        cfg = self.cfg()
        bw, sf = cfg["bw"], cfg["sf"]
        dm = LoraDemod(sf, bw)
        N = dm.N
        keep = (n_symbols(255, sf, 4, True, dm.ldro) + 30) * N      # longest packet + preamble, in chips
        acc, acc_n = [], 0                                        # raw (mixed) blocks waiting for decimation
        mid = np.zeros(0, dtype=np.complex64)                     # ~4 samples per chip history
        base = 0                                                  # absolute chip index of the first resampled chip
        fs_in = None
        d1 = up = down = 1
        while self.running:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            x, fs = item
            if fs_in != fs:                                       # (re)configure the sample-rate conversion
                fs_in, acc, acc_n = fs, [], 0
                mid, base = np.zeros(0, dtype=np.complex64), 0
                d1 = max(1, int(fs // (bw * 4)))
                frac = Fraction(int(bw * d1), int(fs)).limit_denominator(4000)
                up, down = frac.numerator, frac.denominator
            acc.append(x)
            acc_n += len(x)
            self.stats["samples"] += len(x)
            if acc_n < CHUNK_S * fs:
                continue
            raw = np.concatenate(acc)
            m = len(raw) // d1
            lo = raw[:m * d1].reshape(m, d1).mean(axis=1)         # boxcar decimation to ~4x the LoRa bandwidth
            left = raw[m * d1:]
            acc, acc_n = ([left], len(left)) if len(left) else ([], 0)
            mid = np.concatenate([mid, lo.astype(np.complex64)])
            buf = signal.resample_poly(mid, up, down).astype(np.complex64)   # whole history at once: no joins
            try:
                frames = dm.process(buf, carrier_hz=float(cfg["freq"]))
            except Exception:                                     # noqa: BLE001 - a bad buffer must not kill the worker
                log.exception("lora decode")
                frames = []
            now = time.time()
            for fr in frames:
                key = base + fr.start
                h = hashlib.md5(fr.payload).hexdigest()
                if any(abs(key - k) < 3 * N and h == hh for k, hh in self._seen):
                    continue
                self._seen.append((key, h))
                self._on_frame(fr, now)
            # forget history older than one maximum-length packet (a whole number of resampler periods)
            keep_mid = int(keep * down / up)
            drop = max(0, len(mid) - keep_mid) // down * down
            if drop:
                mid = mid[drop:]
                base += drop // down * up
            self._seen = [(k, h) for k, h in self._seen[-80:] if k > base - 3 * N]
            self._tick()
        self._tick(force=True)

    # ---------------------------------------------------------------- packets
    def _on_frame(self, fr, now):
        st = self.stats
        st["last_frame"] = now
        if not fr.crc_ok and fr.crc_ok is not None:
            st["bad_crc"] += 1
            self._log(now, fr, None, "heard a LoRa frame but its checksum failed (%d bytes)" % len(fr.payload), None)
            return
        st["frames"] += 1
        st["best_snr"] = fr.snr_db if st["best_snr"] is None else max(st["best_snr"], fr.snr_db)
        d = mp.parse(fr.payload)
        if d is None:
            st["not_mesh"] += 1
            self._log(now, fr, None, "LoRa frame that is not a MeshCore packet (%d bytes)" % len(fr.payload), None)
            return
        st["by_type"][d["type_name"]] = st["by_type"].get(d["type_name"], 0) + 1
        advert = group = None
        if d["ptype"] == 4:
            advert = mp.parse_advert(d["payload"])
            if advert:
                self._node(advert, d, fr, now)
        elif d["ptype"] in (5, 6):
            group = mp.decode_group(d["payload"], self.channels)
            if group and "text" in group and d["ptype"] == 5:
                self._message(group, d, fr, now)
        self._log(now, fr, d, mp.summarize(d, advert, group), advert)

    def _node(self, a, d, fr, now):
        with self.lock:
            n = self.nodes.get(a["pubkey"], {})
            n.update({"pubkey": a["pubkey"], "name": a["name"] or n.get("name", ""), "type": a["type_name"],
                      "last": now, "adv_ts": a["timestamp"], "hops": d["hops"], "snr": round(fr.snr_db, 1),
                      "count": n.get("count", 0) + 1, "first": n.get("first", now)})
            if a["lat"] is not None:
                n["lat"], n["lon"] = a["lat"], a["lon"]
            self.nodes[a["pubkey"]] = n
        self._mark()

    def _message(self, g, d, fr, now):
        m = {"t": now, "channel": g["channel"], "sender": g["sender"], "text": g["text"], "hops": d["hops"],
             "snr": round(fr.snr_db, 1)}
        with self.lock:
            self.messages.append(m)
            self.messages = self.messages[-MAX_MSG:]
        try:
            with open(self._msg_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(m) + "\n")
        except OSError:
            pass
        self._mark()

    def _log(self, now, fr, d, text, advert):
        via = []
        if d:
            for h in d["path"]:                                   # name the repeaters this packet travelled through
                names = [n["name"] for k, n in self.nodes.items() if k.startswith(h) and n.get("type") == "repeater" and n.get("name")]
                via.append(names[0] if len(names) == 1 else h)
        e = {"t": now, "type": d["type_name"] if d else "?", "route": d["route_name"] if d else "", "hops": d["hops"] if d else None,
             "via": via, "len": len(fr.payload), "snr": round(fr.snr_db, 1), "cfo": round(fr.cfo_hz), "text": text}
        with self.lock:
            self.packets.append(e)
            self.packets = self.packets[-MAX_LOG:]
        self._mark()

    # ---------------------------------------------------------------- publishing
    def _mark(self):
        self._dirty = True

    def _tick(self, force=False):
        now = time.time()
        if (self._dirty and now - self._last_emit > 1.0) or force:
            self._dirty = False
            self._last_emit = now
            self.emit("meshsdr", self.snapshot())
            if now % 30 < 1.5:
                self._save_nodes()

    def snapshot(self):
        cfg = self.cfg()
        with self.lock:
            nodes = sorted(self.nodes.values(), key=lambda n: -n.get("last", 0))
            st = dict(self.stats)
            st["by_type"] = dict(st["by_type"])
            return {"running": self.running, "cfg": cfg, "nodes": nodes[:2000], "messages": self.messages[-200:],
                    "packets": self.packets[-120:], "stats": st,
                    "channels": [{"name": k, "hash": "%02x" % mp.chan_hash(v)} for k, v in self.channels.items()],
                    "user_channels": list(self.s.get("meshsdr_channels") or []), "now": time.time()}
