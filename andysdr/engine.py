"""The radio: owns the receiver hardware, DSP, scanner, decoders. Runs in its own thread."""
import logging
import os
import queue
import threading
import time
import wave

import numpy as np

from . import devices
from .config import data_dir
from .decoders import DecoderManager
from .dsp import (AUDIO_RATE, BLOCK_SECONDS, MODES, Receiver, Spectrum, find_peaks_db,
                  pick_sample_rate)
from .memory import MemoryBank

log = logging.getLogger("andysdr.engine")

SPEC_MIN_DB, SPEC_MAX_DB = -130.0, -10.0


class Radio:
    def __init__(self, settings):
        self.s = settings
        self.memory = MemoryBank()
        self.listeners = []          # callables(kind, payload)
        self.cmd_q = queue.Queue()
        self.dev = None
        self.dev_list = []
        self.rx = None
        self.spec = Spectrum(2048)
        self.decoders = DecoderManager(settings, self._emit)
        self.running = False
        self.thread = None
        self.freq = int(self.s["frequency"])
        self.mode = self.s["mode"]
        self.scan = {"state": "manual"}   # manual | scan | search | hold
        self.activity = {}                # search hits
        self.search_lockouts = set()
        self.discard = 0
        self.last_spec = None
        self.spec_fresh = False
        self.rec = None
        self.priority = None              # (bank, ch)
        self._last_state = 0
        self.level_db = -120.0
        self.sq_open = False
        self.channel = None               # (bank, ch, entry) when on a memory channel

    # ------------------------------------------------------------------ plumbing
    def add_listener(self, fn):
        self.listeners.append(fn)

    def _emit(self, kind, payload):
        for fn in list(self.listeners):
            try:
                fn(kind, payload)
            except Exception:
                log.exception("listener")

    def call(self, fn, *args, **kw):
        """Run a function inside the radio thread (thread-safe control)."""
        self.cmd_q.put((fn, args, kw))

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, name="radio", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        self.decoders.stop()
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
        self.s["frequency"] = self.freq
        self.s["mode"] = self.mode
        self.s.save()

    # ------------------------------------------------------------------ devices
    def refresh_devices(self):
        extra = self.s.get("rtl_tcp_servers", [])
        self.dev_list = devices.list_devices(extra)
        return [{"key": d.key, "label": d.label, "kind": d.kind} for d in self.dev_list]

    def _open_device(self, key):
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
        if not self.dev_list:
            self.refresh_devices()
        dev = next((d for d in self.dev_list if d.key == key), None)
        if dev is None:
            real = [d for d in self.dev_list if d.kind != "sim"]
            dev = real[0] if (real and not key) else self.dev_list[-1]
        try:
            # pick a sample rate the DSP chain divides evenly
            want = int(self.s.get("sample_rate") or 0)
            dev.sample_rate = want if (want and want % 240000 == 0 and want in dev.sample_rates()) else 2400000
            dev.center = self._hw_center_for(self.freq, 2400000)
            dev.gain = float(self.s["rf_gain"])
            dev.agc = bool(self.s["agc"])
            dev.open()
            rates = dev.sample_rates()
            sr = want if want in rates and want % 240000 == 0 else pick_sample_rate(rates)
            if sr != dev.sample_rate:
                dev.set_sample_rate(sr)
            dev.set_ppm(self.s["ppm"])
            if self.s["antenna"]:
                dev.set_antenna(self.s["antenna"])
            self.dev = dev
            self.s["device"] = dev.key
            self._emit("event", {"level": "info", "text": f"Receiver: {dev.label} @ {sr / 1e6:.2f} MS/s"})
        except Exception as e:
            log.exception("open device")
            self._emit("event", {"level": "error",
                                 "text": f"Could not open {dev.label}: {e}. Using the simulator."})
            dev = devices.SimDevice()
            dev.open()
            self.dev = dev
        self.rx = Receiver(self.dev.sample_rate)
        self.rx.deemph_us = self.s["deemphasis_us"]
        self.rx.set_mode(self.mode)
        self.rx.squelch_db = float(self.s["squelch"])
        self.dev.set_center(self._hw_center_for(self.freq, self.dev.sample_rate))
        self.discard = 2
        self._emit("device", self.device_info())

    def device_info(self):
        if not self.dev:
            return {}
        i = self.dev.info()
        i["sample_rate"] = self.dev.sample_rate
        i["center"] = self.dev.center
        i["soapy"] = devices.soapy_status()
        return i

    # ------------------------------------------------------------------ tuning
    def _hw_center_for(self, f, fs):
        # keep the wanted signal away from the DC spike at the centre
        return int(f + fs * 0.25)

    def _tune(self, f, mode=None, force=False):
        f = int(f)
        fs = self.dev.sample_rate if self.dev else 2400000
        if mode and mode in MODES and mode != self.mode:
            self.mode = mode
            if self.rx:
                self.rx.set_mode(mode)
        self.freq = f
        if self.dev:
            off = f - self.dev.center
            if force or abs(off) > fs * 0.42 or abs(off) < 15000:
                self.dev.set_center(self._hw_center_for(f, fs))
                self.discard = 2
                self.spec_fresh = False
                return True
        return False

    def offset(self):
        return self.freq - (self.dev.center if self.dev else self.freq)

    # ------------------------------------------------------------------ public controls
    def set_frequency(self, f, mode=None):
        def do():
            self._stop_scan()
            self.channel = None
            self._tune(f, mode)
            self._push_state(True)
        self.call(do)

    def step(self, direction):
        def do():
            if self.scan["state"] in ("scan", "search", "hold"):
                # in scan mode the arrows change direction / skip
                self.scan["dir"] = 1 if direction > 0 else -1
                self.scan["resume_at"] = 0
                if self.scan["state"] == "hold":
                    self.scan["state"] = self.scan.get("prev", "scan")
                return
            self.channel = None
            self._tune(self.freq + direction * int(self.s["step"]))
            self._push_state(True)
        self.call(do)

    def set_mode(self, mode):
        def do():
            if mode in MODES:
                self.mode = mode
                self.rx.set_mode(mode)
                self._push_state(True)
        self.call(do)

    def set_gain(self, g):
        def do():
            self.s["rf_gain"] = float(g)
            if self.dev:
                self.dev.set_gain(float(g))
            self._push_state(True)
        self.call(do)

    def set_agc(self, on):
        def do():
            self.s["agc"] = bool(on)
            if self.dev:
                self.dev.set_agc(bool(on))
            self._push_state(True)
        self.call(do)

    def set_squelch(self, db):
        def do():
            self.s["squelch"] = float(db)
            if self.rx:
                self.rx.squelch_db = float(db)
            self._push_state(True)
        self.call(do)

    def set_step(self, hz):
        self.s["step"] = int(hz)
        self.call(lambda: self._push_state(True))

    def set_device(self, key):
        self.call(self._open_device, key)

    def device_setting(self, key, value):
        def do():
            if key == "antenna":
                self.s["antenna"] = value
                self.dev.set_antenna(value)
            elif key == "ppm":
                self.s["ppm"] = int(value)
                self.dev.set_ppm(int(value))
            elif key == "sample_rate":
                self.s["sample_rate"] = int(value)
                self._open_device(self.dev.key)
                return
            else:
                self.dev.write_setting(key, value)
            self._emit("device", self.device_info())
        self.call(do)

    def set_decoder(self, dec_id, sub="auto"):
        def do():
            self.decoders.start(dec_id, sub)
            self._push_state(True)
        self.call(do)

    def recall(self, bank, ch):
        def do():
            c = self.memory.all()["banks"][str(int(bank))]["channels"].get(str(int(ch)))
            if not c:
                self._emit("event", {"level": "warn", "text": f"Bank {bank} channel {ch} is empty"})
                return
            self._stop_scan()
            self._go_channel(int(bank), int(ch), c)
            self._push_state(True)
        self.call(do)

    def _go_channel(self, bank, ch, c):
        self.channel = (bank, ch, c)
        self._tune(c["freq"], c.get("mode"))
        dec = c.get("decoder", "")
        if dec and dec != self.decoders.active and self.s.get("auto_decoder", True):
            self.decoders.start(dec, "auto")

    def store(self, bank, ch=None, name=""):
        def do():
            slot = ch if ch is not None else self.memory.free_slot(bank)
            if slot is None:
                self._emit("event", {"level": "warn", "text": f"Bank {bank} is full"})
                return
            e = self.memory.store(bank, slot, {"freq": self.freq, "mode": self.mode, "name": name,
                                               "decoder": self.decoders.active or ""})
            self._emit("memory", self.memory.all())
            self._emit("event", {"level": "info",
                                 "text": f"Stored {self.freq / 1e6:.4f} MHz in bank {bank} ch {slot}"})
            self.channel = (int(bank), slot, e)
            self._push_state(True)
        self.call(do)

    def lockout(self):
        """L/O button: lock out current memory channel, or current search frequency."""
        def do():
            if self.scan["state"] in ("hold", "search") or self.scan.get("prev") == "search":
                self.search_lockouts.add(self.freq)
                self._emit("event", {"level": "info", "text": f"Search lockout {self.freq / 1e6:.4f} MHz"})
                if self.scan["state"] == "hold":
                    self.scan["state"] = self.scan.get("prev", "search")
            elif self.channel:
                b, ch, c = self.channel
                c = self.memory.update(b, ch, {"lockout": not c.get("lockout")})
                self.channel = (b, ch, c)
                self._emit("memory", self.memory.all())
                if self.scan["state"] == "hold":
                    self.scan["state"] = "scan"
            self._push_state(True)
        self.call(do)

    def set_priority(self, bank=None, ch=None):
        def do():
            self.priority = None if bank is None else (int(bank), int(ch))
            self._push_state(True)
        self.call(do)

    def record(self, on):
        def do():
            if on and not self.rec:
                path = os.path.join(data_dir(), "recordings",
                                    time.strftime("audio_%Y%m%d_%H%M%S_") + f"{self.freq / 1e6:.4f}MHz.wav")
                self.rec = wave.open(path, "wb")
                self.rec.setnchannels(1)
                self.rec.setsampwidth(2)
                self.rec.setframerate(AUDIO_RATE)
                self._emit("event", {"level": "info", "text": f"Recording to {path}"})
            elif not on and self.rec:
                self.rec.close()
                self.rec = None
                self._emit("event", {"level": "info", "text": "Recording stopped"})
            self._push_state(True)
        self.call(do)

    # ------------------------------------------------------------------ scanner
    def start_scan(self):
        def do():
            lst = self.memory.scan_list()
            if not lst:
                self._emit("event", {"level": "warn", "text": "No memory channels to scan – store or import some first"})
                return
            if self.s["squelch"] <= -119:
                self._emit("event", {"level": "warn", "text": "Turn SQUELCH up – scanning needs the squelch closed on noise"})
            self.scan = {"state": "scan", "list": lst, "idx": -1, "dir": 1, "phase": "next",
                         "t": 0, "resume_at": 0}
            self._push_state(True)
        self.call(do)

    def start_search(self, start=None, stop=None, step=None, mode=None, search_mode=None):
        def do():
            if start:
                self.s["search_start"] = int(start)
            if stop:
                self.s["search_stop"] = int(stop)
            if step:
                self.s["search_step"] = int(step)
            if search_mode:
                self.s["search_mode"] = search_mode
            if mode:
                self.mode = mode
                self.rx.set_mode(mode)
            a, b = sorted((int(self.s["search_start"]), int(self.s["search_stop"])))
            fs = self.dev.sample_rate
            span = fs * 0.8
            hops = []
            c = a + span / 2
            while True:
                hops.append(int(c))
                if c + span / 2 >= b:
                    break
                c += span
            self.scan = {"state": "search", "lo": a, "hi": b, "hops": hops, "hop": -1,
                         "phase": "hop", "spec_n": 0, "acc": None, "resume_at": 0, "dir": 1}
            self._push_state(True)
        self.call(do)

    def stop_scan(self):
        self.call(lambda: (self._stop_scan(), self._push_state(True)))

    def _stop_scan(self):
        if self.scan["state"] != "manual":
            self.scan = {"state": "manual"}

    def clear_activity(self):
        self.call(lambda: (self.activity.clear(), self._emit("activity", [])))

    def _scan_tick(self, spec_db):
        """Called each block while scanning. Returns True if audio should be produced."""
        sc = self.scan
        now = time.time()
        st = sc["state"]
        if st == "hold":
            if self.sq_open:
                sc["resume_at"] = now + (self.s["scan_delay"] if sc.get("delay", True) else 0.2)
                return True
            if now < sc.get("resume_at", 0):
                return True
            sc["state"] = sc.get("prev", "scan")
            st = sc["state"]
        if st == "scan":
            return self._scan_memory(spec_db, now)
        if st == "search":
            return self._scan_search(spec_db, now)
        return True

    def _scan_memory(self, spec_db, now):
        sc = self.scan
        lst = sc["list"]
        if sc["phase"] == "dwell":
            if self.discard > 0:
                return False
            if self.sq_open:
                b, ch, c = lst[sc["idx"]]
                sc.update(state="hold", prev="scan", delay=c.get("delay", True),
                          resume_at=now + self.s["scan_delay"])
                self._log_hit(self.freq, self.level_db, c.get("name", ""))
                return True
            if now < sc["t"]:
                return False
            sc["phase"] = "next"
        # choose next channel; skip quickly over ones the spectrum shows are silent
        fs = self.dev.sample_rate
        for _ in range(min(len(lst), 64)):
            sc["idx"] = (sc["idx"] + sc.get("dir", 1)) % len(lst)
            b, ch, c = lst[sc["idx"]]
            off = c["freq"] - self.dev.center
            in_span = abs(off) < fs * 0.42 and abs(off) > 15000
            if in_span and self.spec_fresh and spec_db is not None:
                n = len(spec_db)
                i = int(n / 2 + off / fs * n)
                floor = float(np.median(spec_db))
                lvl = float(np.max(spec_db[max(0, i - 2): i + 3]))
                if lvl < floor + 6:
                    continue          # nothing there – no need to dwell
            self._go_channel(b, ch, c)
            self.discard = max(self.discard, 1)
            sc["phase"] = "dwell"
            sc["t"] = now + float(self.s["scan_dwell"])
            self._push_state(True)
            return False
        return False

    def _scan_search(self, spec_db, now):
        sc = self.scan
        if sc["phase"] == "check":
            # we stopped on a hit – confirm with the real squelch
            if self.discard > 0:
                return False
            if self.sq_open:
                sc.update(state="hold", prev="search", delay=True, resume_at=now + self.s["scan_delay"],
                          phase="hop")
                return True
            if now < sc["t"]:
                return False
            sc["phase"] = "hop"
        if sc["phase"] == "hop":
            sc["hop"] = (sc["hop"] + 1) % len(sc["hops"])
            c = sc["hops"][sc["hop"]]
            if self.dev.center != c:
                self.dev.set_center(c)
                self.discard = 2
            sc["phase"] = "listen"
            sc["spec_n"] = 0
            sc["acc"] = None
            return False
        if sc["phase"] == "listen":
            if self.discard > 0 or spec_db is None:
                return False
            sc["acc"] = spec_db if sc["acc"] is None else np.maximum(sc["acc"], spec_db)
            sc["spec_n"] += 1
            if sc["spec_n"] < 2:
                return False
            fs = self.dev.sample_rate
            peaks = find_peaks_db(sc["acc"], fs, self.dev.center, float(self.s["search_threshold_db"]),
                                  min_sep_hz=max(int(self.s["search_step"]), 5000))
            step = max(1, int(self.s["search_step"]))
            hits = []
            for f, lvl in peaks:
                f = int(round(f / step) * step)
                if sc["lo"] <= f <= sc["hi"] and f not in self.search_lockouts:
                    hits.append((f, lvl))
                    self._log_hit(f, lvl, "")
            if hits and self.s["search_mode"] == "stop":
                f, lvl = max(hits, key=lambda h: h[1])
                self.freq = f      # keep hardware centre, just move the channel
                if abs(self.offset()) < 15000:
                    self._tune(f, force=True)
                sc["phase"] = "check"
                sc["t"] = now + 0.25
                self._push_state(True)
                return False
            sc["phase"] = "hop"
            return False
        return False

    def _log_hit(self, f, lvl, name):
        a = self.activity.get(f)
        if a:
            a["count"] += 1
            a["level"] = round(max(a["level"], lvl), 1)
            a["last"] = time.strftime("%H:%M:%S")
        else:
            ch = next(((b, c, e) for b, c, e in self.memory.scan_list() if abs(e["freq"] - f) < 3000), None)
            self.activity[f] = {"freq": f, "level": round(lvl, 1), "count": 1,
                                "first": time.strftime("%H:%M:%S"), "last": time.strftime("%H:%M:%S"),
                                "name": name or (ch[2]["name"] if ch else ""), "mode": self.mode}
        self._emit("activity", sorted(self.activity.values(), key=lambda x: -x["count"])[:300])

    # ------------------------------------------------------------------ state
    def state(self):
        ch = None
        if self.channel:
            b, c, e = self.channel
            ch = {"bank": b, "ch": c, "name": e.get("name", ""), "lockout": e.get("lockout", False),
                  "tag": e.get("tag", "")}
        sc = self.scan["state"]
        return {
            "freq": self.freq, "mode": self.mode, "step": int(self.s["step"]),
            "gain": float(self.s["rf_gain"]), "agc": bool(self.s["agc"]),
            "squelch": float(self.s["squelch"]), "level": round(self.level_db, 1),
            "sq_open": bool(self.sq_open), "scan": sc, "scan_prev": self.scan.get("prev"),
            "channel": ch, "decoder": self.decoders.active, "decoder_sub": self.decoders.sub,
            "recording": self.rec is not None,
            "center": self.dev.center if self.dev else self.freq,
            "fs": self.dev.sample_rate if self.dev else 2400000,
            "device": self.dev.label if self.dev else "",
            "search": {"start": self.s["search_start"], "stop": self.s["search_stop"],
                       "step": self.s["search_step"], "mode": self.s["search_mode"],
                       "threshold": self.s["search_threshold_db"]},
            "priority": self.priority,
        }

    def _push_state(self, force=False):
        now = time.time()
        if force or now - self._last_state > 0.2:
            self._last_state = now
            self._emit("state", self.state())

    # ------------------------------------------------------------------ main loop
    def _run(self):
        self.refresh_devices()
        self._open_device(self.s["device"])
        pending = np.zeros(0, np.complex64)
        last_pri = 0
        while self.running:
            # control commands
            try:
                while True:
                    fn, a, kw = self.cmd_q.get_nowait()
                    try:
                        fn(*a, **kw)
                    except Exception as e:
                        log.exception("command")
                        self._emit("event", {"level": "error", "text": str(e)})
            except queue.Empty:
                pass
            fs = self.dev.sample_rate
            block = int(fs * BLOCK_SECONDS)
            try:
                x = self.dev.read(block - len(pending))
            except Exception as e:
                log.exception("read")
                self._emit("event", {"level": "error", "text": f"Receiver error: {e}"})
                time.sleep(1)
                try:
                    self._open_device(self.dev.key)
                except Exception:
                    pass
                pending = np.zeros(0, np.complex64)
                continue
            pending = np.concatenate((pending, x)) if len(pending) else x
            if len(pending) < block:
                continue
            iq, pending = pending[:block], pending[block:]
            if self.discard > 0:
                self.discard -= 1
                self.spec_fresh = False
                if self.scan["state"] != "manual":
                    self._scan_tick(None)
                continue

            spec_db = self.spec.process(iq, alpha=0.6 if self.scan["state"] == "manual" else 1.0)
            self.spec_fresh = True
            q = np.clip((spec_db - SPEC_MIN_DB) / (SPEC_MAX_DB - SPEC_MIN_DB) * 255, 0, 255).astype(np.uint8)
            self._emit("spectrum", {"center": self.dev.center, "fs": fs, "freq": self.freq,
                                    "data": q.tobytes(), "min": SPEC_MIN_DB, "max": SPEC_MAX_DB})

            out = self.rx.process(iq, self.offset())
            self.level_db = out["level_db"]
            self.sq_open = out["squelch_open"]

            produce = True
            if self.scan["state"] != "manual":
                produce = self._scan_tick(spec_db)

            # priority channel check (Pro-2045 style: every 2 s, only if in view)
            if self.priority and time.time() - last_pri > 2 and self.scan["state"] in ("manual", "scan"):
                last_pri = time.time()
                self._priority_check(spec_db)

            if produce:
                self.decoders.feed(out)
                audio = out["audio"]
                pcm = np.clip(audio * 32767, -32767, 32767).astype("<i2").tobytes()
                self._emit("audio", pcm)
                if self.rec:
                    self.rec.writeframes(pcm)
            self._push_state()

    def _priority_check(self, spec_db):
        b, ch = self.priority
        c = self.memory.all()["banks"][str(b)]["channels"].get(str(ch))
        if not c or spec_db is None:
            return
        fs = self.dev.sample_rate
        off = c["freq"] - self.dev.center
        if abs(off) > fs * 0.42:
            return
        n = len(spec_db)
        i = int(n / 2 + off / fs * n)
        if spec_db[max(0, i - 2): i + 3].max() > np.median(spec_db) + 10 and self.freq != c["freq"]:
            self._emit("event", {"level": "info", "text": f"Priority channel active: {c['name']}"})
            if self.scan["state"] == "scan":
                self.scan.update(state="hold", prev="scan", resume_at=time.time() + self.s["scan_delay"])
            self._go_channel(b, ch, c)
