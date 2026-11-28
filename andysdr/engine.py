"""The radio: owns the receiver hardware, DSP, scanner, decoders. Runs in its own thread."""
import logging
import os
import queue
import threading
import time
import wave
import io
from collections import deque

import numpy as np

from . import aero, capturemeta, devices, ft8, satmerge
from .adsb import ADSB_FREQ, Adsb
from .meshsdr import MeshSdr
from .ism import IsmSensors
from .datalink import DatalinkStore
from .dmrdb import DmrDb
from .repeaters import Repeaters
from .lastheard import LastHeard
from .autodetect import SignalClassifier, UNSETTLED
from .config import DEFAULTS as SETTINGS_DEFAULTS, data_dir
from .decoders import DecoderManager, find_exe
from .pager import PagerStore
from .dsp import (AUDIO_RATE, BLOCK_SECONDS, MODES, Receiver, Spectrum, find_peaks_db,
                  pick_sample_rate)
from .memory import MemoryBank
from .scheduler import Scheduler
from .biasmon import BiasMonitor, judge
from . import framesnr, freqcheck, gaincal, passlog, rigctl
from .sstv import SstvDecoder

log = logging.getLogger("andysdr.engine")

SPEC_MIN_DB, SPEC_MAX_DB = -130.0, -10.0
AG_HIGH_DB = -45.0       # auto RF gain: a signal stronger than this (in the channel, dB from full scale) is eased down
AG_BLOCKS = 6            # ... if it stays that strong for this many 50 ms blocks
AG_SETTLE_S = 1.0        # ... and then waits this long for the radio to settle before the next step
AG_STEP_FRAC = 0.06      # each step lowers the gain by this fraction of the receiver's gain range ...
AG_STEP_MAX_DB = 3.0     # ... but never more than this, or a wide-range receiver moves in huge jumps
AG_MAX_DROP = 0.35       # ... but never by more than this fraction of the range in total
AG_QUIET_DB = 10.0       # auto RF gain: the channel is "quiet" when it is this far below the strong-signal level ...
AG_RECOVER_S = 6.0       # ... for this long: the gain then climbs back one step (so a weak caller after a strong one is still heard)
AG_OVL_DROP = 0.5        # when the receiver itself reports ADC overload, the gain may be eased down this fraction of the range
AG_DROP_MAX_DB = 20.0    # ... and whatever the fractions work out at, never further down than this in total

# Decoders the automatic gain must leave alone. Two kinds. Those that are handed raw IQ (ADS-B, MeshCore, the
# sensor bands, AERO, the satellites) have no VFO channel worth measuring, so the level the automatic gain reads is
# not the signal it thinks it is - and ADS-B has no gain of its own, so every step taken here is a step off its
# range. And those where the amplitude *is* the information: ACARS is AM, a weather fax or an SSTV picture is drawn
# by amplitude, so moving the gain mid-message corrupts the thing being decoded rather than rescuing it. What is
# left - FM voice, pagers, packet - is what the automatic gain was written for, where only the frequency carries
# meaning and backing off a distorting signal genuinely helps.
AG_STEADY = frozenset({"adsb", "meshsdr", "ism", "aero", "goes", "metop", "lrpt", "acars", "wefax", "sstv",
                       "vdl2", "ft8", "sonde"})
FREQ_CHECK_EVERY_S = 6.0     # how often to check where the carrier really is during a pass

AUTO_MODE_VOTES = 4          # readings in a row, all taken on a real signal, before the demodulator is changed
AUTO_MODE_HOLD_S = 15.0      # ... and this long between changes, so a change can be heard before the next one
AUTO_MODE_MIN_SNR = 8.0      # with no squelch to go by, a reading this weak is the gap between bursts, not a signal
AUTO_MODE_MAX_SWAPS = 3      # changed its mind this often on one frequency: stop, it clearly cannot tell

AG_HOLD_S = 60.0         # ... but not for this long after a strong signal pushed it down again soon after a climb
MUTE_WHEN_DECODING = ("dsd", "tetra", "tetrapol", "pager", "data")
REPLAY_MAX_S = 120                    # the replay buffer remembers up to two minutes of what you heard


class Radio:
    def __init__(self, settings):
        self.s = settings
        self.memory = MemoryBank()
        self.listeners = []          # callables(kind, payload)
        self.cmd_q = queue.Queue()
        self.dev = None
        self.dev_list = []
        self.rx = None
        self.spec = Spectrum(4096)
        self.decoders = DecoderManager(settings, self._emit)
        self.pager = PagerStore(settings, self._emit)
        self.datalink = DatalinkStore(settings, self._emit)
        self.decoders.datalink = self.datalink
        self.adsb = Adsb(settings, self._emit)
        self.decoders.adsb = self.adsb
        self.meshsdr = MeshSdr(settings, self._emit)
        self.decoders.meshsdr = self.meshsdr
        # Lets a trunking decoder retune AndySDR to follow a call onto its voice channel and back again. Inside the
        # slice already being captured that costs nothing: `_tune` just moves which part of it is listened to.
        self.rigctl = rigctl.RigctlServer(lambda: self.freq, self.set_frequency,
                                          log_cb=lambda t: self.decoders.log(t),
                                          get_mode=lambda: self.mode)
        self.decoders.rigctl = self.rigctl
        self.ism = IsmSensors(self._emit, freq_fn=lambda: self.freq, on_new=lambda row, text: self.decoders.log(text))
        self.decoders.ism = self.ism
        self.decoders.freq_fn = lambda: self.freq
        self.decoders.job_fn = lambda: self.job
        self.decoders.dial_fn = lambda: (ft8.match_freq(self.freq, self.mode) or (None, self.freq))[1]
        self.decoders.rate_fn = lambda: self.dev.sample_rate if self.dev else 2400000
        self.decoders.on_pager = lambda line: self.pager.add_line(
            line, self.freq, self.channel[2].get("name", "") if self.channel else "")
        self.dmrdb = DmrDb(settings, self._emit_dmrdb,
                           memory_tg=lambda: {t["tgid"]: t.get("name", "") for t in self.memory.data.get("talkgroups", [])
                                              if t.get("name")})
        self.repeaters = Repeaters(settings, self._emit)
        self.lastheard = LastHeard(settings, self._emit, self.dmrdb, freq_fn=lambda: self.freq,
                                   on_new=lambda row, text: self.decoders.log(text))
        self.decoders.on_dsd_line = self.lastheard.feed
        self.running = False
        self.thread = None
        self.freq = int(self.s["frequency"])
        self.mode = self.s["mode"]
        self.scan = {"state": "manual"}   # manual | scan | search | hold
        self.activity = {}                # search hits
        self.search_lockouts = set()
        self.temp_lock = {}               # ("m", bank, ch) or ("f", Hz) -> (expires_at, label)  -  temporary "skip for a while"
        self.discard = 0
        self.last_spec = None
        self.spec_fresh = False
        self.rec = None
        self.replay = deque(maxlen=int(REPLAY_MAX_S / BLOCK_SECONDS))   # (time, pcm, freq, mode) of every block that had sound
        self.priority = None              # (bank, ch)
        self._last_state = 0
        self.level_db = -120.0
        self.ag_cur = None        # auto RF gain: the gain actually in use (None = the knob's value)
        self.gcal = None          # a gain calibration sweep in progress (see gaincal)
        self.ag_freq = None
        self.ag_over = 0
        self.ag_t = 0.0
        self.ag_log_t = 0.0
        self.ag_quiet_t = None    # auto RF gain: since when the channel has been quiet (None = not quiet)
        self.ag_raised_t = 0.0    # ... when the gain last climbed back
        self.ag_hold_t = 0.0      # ... no climbing back before this time
        self.ag_ovl_seen = 0      # ... ADC overload events counted by the receiver so far
        self.ag_ovl_t = 0.0       # ... when the receiver last reported an overload
        self.pass_images = 0      # pictures that came out while the pass was running
        self.job_gain = None      # RF gain chosen for the satellite pass being recorded (None = the knob's value)
        self.pass_meter = None    # measures the signal during a satellite pass
        self.freq_check = None    # ...and whether the receiver is really on the frequency the orbit says
        self.pass_el = None       # ts -> (elevation, range km) of that satellite
        self.passlog = passlog.PassLog(os.path.join(data_dir(), "passes.jsonl"))
        self.decoders.pass_done = self._pass_images_done
        # when both receivers take the same pass, this puts the frames they each heard together
        self.merger = satmerge.Merger(self.decoders.images_dir, find_exe, self.s,
                                      log_cb=lambda t: self.decoders.log(t),
                                      done_cb=self._merged_pass_done)
        # The .cadu files of every pass already recorded are sitting in the images folder, so how much of each one
        # actually arrived can be worked out now rather than only from here on. Reading them takes a moment, so it
        # happens out of the way of starting up.
        threading.Thread(target=self._backfill_frames, daemon=True, name="framesnr-backfill").start()
        self.sq_open = False
        self.channel = None               # (bank, ch, entry) when on a memory channel
        self.classifier = SignalClassifier()
        self.signal = None                # last automatic identification
        self.auto_owned = False           # the running decoder was started by auto-identify
        self.auto_failed = set()          # decoders that could not start (program missing)
        self.sstv_watch = None            # listens for SSTV headers while auto-identify is on
        self.auto_gen = 0                 # bumps on every retune so late results are ignored
        self.auto_busy = False
        self.auto_mode_prev = None        # mode before auto-identify switched it
        self.auto_mode_lock = False       # you picked a mode by hand: Auto leaves the mode alone until you retune
        self.auto_mode_vote = (None, 0)   # (mode, how many readings in a row asked for it) - see _auto_voice_mode
        self.auto_mode_t = 0.0            # when the demodulator was last changed by auto-identify
        self.auto_mode_swaps = 0          # how many times on this frequency: too many means it cannot tell, so stop
        self.pre = deque(maxlen=60)       # last ~3 s of demodulated blocks, replayed into a decoder auto-identify starts
        self.auto_q = queue.Queue(maxsize=1)
        threading.Thread(target=self._auto_worker, name="autoid", daemon=True).start()
        self.job = None                   # scheduled capture in progress
        self.job_prev = None
        self.scheduler = Scheduler(self, settings)
        from .satrx import SatRx
        self.satrx = SatRx(self)             # an optional second receiver for 137 MHz Meteor passes
        self.biasmon = BiasMonitor()
        self._bias_emit = 0.0
        from .meshlink import MeshLink
        self.mesh = MeshLink(settings, self._emit)   # MeshCore messaging via a companion board
        from .gps import GpsLink
        self.gps = GpsLink(settings, self._emit, self._gps_position)   # USB GPS unit -> my position

    # ------------------------------------------------------------------ plumbing
    def add_listener(self, fn):
        self.listeners.append(fn)

    def _emit(self, kind, payload):
        if kind == "image" and self.passlog.open_key:
            self.pass_images += 1
        for fn in list(self.listeners):
            try:
                fn(kind, payload)
            except Exception:
                log.exception("listener")

    def _emit_dmrdb(self, kind, payload):
        self._emit(kind, payload)
        if kind == "dmrdb" and payload.get("state") == "ready":
            try:
                self.lastheard.relookup()            # names for people heard before the list arrived
            except Exception:
                log.exception("relookup")

    def call(self, fn, *args, **kw):
        """Run a function inside the radio thread (thread-safe control)."""
        self.cmd_q.put((fn, args, kw))

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, name="radio", daemon=True)
        self.thread.start()
        self.scheduler.start()
        self.mesh.autoconnect()
        self.gps.autostart()

    def _gps_position(self, lat, lon, alt):
        """A GPS fix arrived (or the unit moved): update satellite passes and the ADS-B reference position."""
        try:
            if self.scheduler.sats.set_observer(lat, lon, alt):
                self.scheduler.sats.refresh_tles()
                self.scheduler._emit()
            self.adsb.set_position(lat, lon)
        except Exception:
            log.exception("gps position")

    def stop(self):
        try:
            self.dmrdb.stop()
        except Exception:
            pass
        try:
            self.gps.stop()
        except Exception:
            pass
        self.scheduler.stop()
        try:
            self.satrx.shutdown()
        except Exception:
            pass
        try:
            self.mesh.stop()
        except Exception:
            pass
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        self.decoders.stop()
        self.rigctl.stop()
        self.adsb.shutdown()
        self.meshsdr.shutdown()
        from .decoders import stop_pulse
        stop_pulse()
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
            # saved receiver not found (e.g. listed under a new name) - use the first real one
            dev = real[0] if (real and key != "sim") else self.dev_list[-1]
        self._fix_saved_freq(dev)
        try:
            # pick a sample rate the DSP chain divides evenly
            want = int(self.s.get("sample_rate") or 0)
            dev.sample_rate = want if (want and want in dev.sample_rates()) else 2400000
            dev.center = self._hw_center_for(self.freq, 2400000)
            dev.gain = float(self.s["rf_gain"])
            dev.agc = bool(self.s["agc"])
            dev.open()
            rates = dev.sample_rates()
            sr = want if want in rates else pick_sample_rate(rates)
            if sr != dev.sample_rate:
                dev.set_sample_rate(sr)
            dev.set_ppm(self.s["ppm"])
            if self.s["antenna"]:
                dev.set_antenna(self.s["antenna"])
            for k, v in (self.s.get("dev_switches", {}).get(dev.key, {}) or {}).items():     # bias-T / notches as you left them
                try:
                    dev.write_setting(k, v)
                except Exception:
                    log.exception("restore receiver switch %s", k)
            self.dev = dev
            self._bias_check()
            self.s["device"] = dev.key
            self._emit("event", {"level": "info", "text": f"Receiver: {dev.label} @ {sr / 1e6:.2f} MS/s"})
        except Exception as e:
            log.exception("open device")
            self._emit("event", {"level": "error",
                                 "text": f"Could not open {dev.label}: {e}. Using the simulator."})
            dev = devices.SimDevice()
            dev.open()
            self.dev = dev
        if isinstance(self.dev, devices.SimDevice):
            self.dev.ref_fn = self.adsb.tracker.ref
        self.rx = Receiver(self.dev.sample_rate)
        self.rx.nb_on = bool(self.s.get("noise_blanker", True))
        self.rx.notch.configure(man_on=bool(self.s.get("notch_on", False)), man_hz=float(self.s.get("notch_hz", 1000)),
                                man_q=float(self.s.get("notch_q", 20)), auto_on=bool(self.s.get("auto_notch", False)))
        self.rx.deemph_us = self.s["deemphasis_us"]
        self.rx.bw_lookup = self._remembered_bw
        self.rx.set_mode(self.mode)
        self.rx.squelch_db = float(self.s["squelch"])
        self.dev.set_center(self._hw_center_for(self.freq, self.dev.sample_rate))
        self.discard = 2
        self._emit("device", self.device_info())

    def _fix_saved_freq(self, dev):
        """A saved frequency the receiver can't tune (e.g. kHz typed as MHz in an older version) makes the RSP
        refuse to start at all, so bring it back into range before the radio is opened."""
        try:
            lo, hi = dev.freq_range()
        except Exception:
            lo, hi = 1e3, 2e9
        f = int(self.freq)
        if lo <= f <= hi:
            return
        g = f
        while g > hi and g >= 1000:
            g //= 1000                        # 131725000000 -> 131725000 (131.725 MHz)
        if not (lo <= g <= hi):
            g = int(SETTINGS_DEFAULTS["frequency"]) if lo <= SETTINGS_DEFAULTS["frequency"] <= hi else int(lo)
        log.warning("saved frequency %d Hz is outside %s's range - using %d Hz", f, dev.label, g)
        self.freq = g
        self.s["frequency"] = g
        self._emit("event", {"level": "warn", "text": f"The saved frequency was out of range - reset to {g / 1e6:,.4f} MHz"})

    def device_info(self):
        if not self.dev:
            return {}
        i = self.dev.info()
        i["sample_rate"] = self.dev.sample_rate
        i["center"] = self.dev.center
        i["antenna"] = self.s["antenna"] or getattr(self.dev, "antenna", "") or ""     # the input in use, so the GUI can show it
        i["soapy"] = devices.soapy_status()
        return i

    # ------------------------------------------------------------------ tuning
    def _hw_center_for(self, f, fs):
        if self.decoders.active == "adsb":
            return int(f)                     # ADS-B uses the whole 2 MHz around 1090 MHz
        # keep the wanted signal away from the DC spike at the centre
        return int(f + fs * 0.25)

    def _tune(self, f, mode=None, force=False):
        f = int(f)
        if self.dev:
            lo, hi = self.dev.freq_range()
            if not (lo <= f <= hi):
                # a mistyped frequency (e.g. kHz typed as MHz) must never reach the receiver: the RSP refuses it and locks up
                log.warning("refused to tune to %d Hz - outside %s's range (%d - %d Hz)", f, self.dev.label, lo, hi)
                self._emit("event", {"level": "warn", "text": f"{f / 1e6:,.4f} MHz is outside what this receiver can tune "
                                                              f"({lo / 1e6:g} - {hi / 1e6:g} MHz) - check the frequency you typed"})
                return False
        if self.decoders.active in ("goes", "metop") and abs(f - self.freq) > 1000000:
            self.decoders.stop()              # moved off the GOES channel
        if self.decoders.active == "aero" and abs(f - self.freq) > 1000000:
            self.decoders.stop()              # moved off the Inmarsat L-band channels
        if self.decoders.active == "meshsdr" and abs(f - self.freq) > 100000:
            self.decoders.stop()              # tuned away from the MeshCore channel
        if self.decoders.active == "ism" and abs(f - self.freq) > 1000000:
            self.decoders.stop()              # moved off the sensor band: rtl_433 would only be listening to nothing
        if self.decoders.active == "adsb" and abs(f - ADSB_FREQ) > 1000000:
            self.decoders.stop()              # tuned away from 1090 MHz: the aircraft decoder is no use here
        if abs(f - self.freq) > 1000:
            self._auto_reset()
        fs = self.dev.sample_rate if self.dev else 2400000
        if mode and mode in MODES and mode != self.mode:
            self.mode = mode
            if self.rx:
                self.rx.set_mode(mode)
        self.freq = f
        if self.dev:
            off = f - self.dev.center
            if force or abs(off) > fs * 0.42 or (abs(off) < 15000 and self.decoders.active != "adsb"):
                self.dev.set_center(self._hw_center_for(f, fs))
                self.discard = 2
                self.spec_fresh = False
                return True
        return False

    # ------------------------------------------------------------------ auto-identify
    def _auto_reset(self):
        self.classifier.reset()
        self.classifier.history.clear()
        self.auto_gen += 1
        self.sstv_watch = None
        self.auto_mode_lock = False
        self.auto_mode_vote = (None, 0)
        self.auto_mode_t = 0.0
        self.auto_mode_swaps = 0
        if self.auto_mode_prev:              # put back the mode auto-identify changed
            if self.rx and self.auto_mode_prev != self.mode:
                self.mode = self.auto_mode_prev
                self.rx.set_mode(self.mode)
            self.auto_mode_prev = None
        if self.signal is not None:
            self.signal = None
            self._emit("signal", None)
        if self.auto_owned and self.decoders.active:
            self.decoders.stop()
        self.auto_owned = False

    def set_auto_identify(self, on):
        def do():
            self.s["auto_identify"] = bool(on)
            self.s.save()
            if not on:
                self._auto_reset()
            self._push_state(True)
        self.call(do)

    def _auto_feed(self, out):
        if self.decoders.active in ("adsb", "ft8", "goes", "metop", "meshsdr", "ism", "aero"):
            return
        # band plan says FT8 / FT4 here and something is on the air: start that decoder
        if (self.s.get("auto_identify", True) and self.decoders.active in (None, "") and not self.job
                and self.sq_open and "ft8" not in self.auto_failed and self.scan["state"] in ("manual", "hold")):
            m = ft8.match_freq(self.freq, self.mode)
            if m:
                ok = self.decoders.start("ft8", m[0])
                if ok:
                    self.auto_owned = True
                    self._set_signal({"kind": m[0].upper(), "label": f"{m[0].upper()} channel (band plan) – decoding",
                                      "decoder": "ft8", "stable": True, "conf": 1.0})
                    self._emit("event", {"level": "info", "text": f"Auto: {m[0].upper()} channel {m[1] / 1e6:.4f} MHz – decoder started"})
                    self._push_state(True)
                else:
                    self.auto_failed.add("ft8")
                    self._emit("event", {"level": "warn", "text": "Auto: FT8 channel found, but its decoder program "
                                                                  "(decode_ft8) is missing – see Help › Decoders"})
                return
        if not self.s.get("auto_identify", True) or self.job or self.scan["state"] not in ("manual", "hold"):
            return
        if not self.sq_open:
            self.classifier.reset()
            self.pre.clear()
            return
        if self.decoders.active in (None, ""):
            self.pre.append(out)
        # SSTV pictures start with a VIS header – watch for it continuously so none are missed
        recording_sound = self.rec is not None or bool(self.job and self.job.get("action") == "record")
        if self.decoders.active in (None, "") or (self.auto_owned and self.decoders.active != "sstv"):
            if self.mode != "WFM" and not recording_sound:        # a sound recording makes sound only - no pictures
                if self.sstv_watch is None:
                    self.sstv_watch = SstvDecoder(self.decoders.images_dir, watch_only=True)
                self.sstv_watch.process(out["audio_ungated"])
                if self.sstv_watch.state == "found":
                    mode = self.sstv_watch.vis_found
                    self.decoders.adopt_sstv(self.sstv_watch)
                    self.sstv_watch = None
                    self.auto_owned = True
                    self._set_signal({"kind": "SSTV", "label": f"SSTV picture – {mode}", "decoder": "sstv",
                                      "stable": True, "conf": 1.0})
                    self._emit("event", {"level": "info", "text": f"Auto: SSTV {mode} picture – decoding"})
                    self._push_state(True)
                    return
        got = self.classifier.collect(out)
        if got and not self.auto_busy:
            try:
                self.auto_q.put_nowait((got, self.mode, self.freq, self.auto_gen))
                self.auto_busy = True
            except queue.Full:
                pass

    def _auto_worker(self):
        """Runs the (fairly heavy) analysis off the radio thread so samples are never dropped."""
        while True:
            (iq, audio), mode, freq, gen = self.auto_q.get()
            try:
                res = self.classifier.classify(iq, audio, mode, freq)
            except Exception:
                log.exception("classify")
                res = None
            self.call(self._auto_result, res, gen)

    def _auto_result(self, res, gen):
        self.auto_busy = False
        if gen != self.auto_gen or not self.s.get("auto_identify", True) or self.job:
            return
        r = self.classifier.decide(res)
        if r and r.get("kind") == UNSETTLED and self.decoders.active == "dsd" and self.decoders.dsd_recent_sync(30):
            # the classifier cannot name it, but DSD-FME is really locked onto a digital voice signal (a steady run of
            # clean frames): the decoder's lock is the better evidence, so say that instead of "Unidentified"
            r = dict(r, kind="Digital voice", label="Digital voice – decoder locked", decoder="dsd",
                     sub=self.decoders.sub or "auto", conf=0.9)
        if r:
            key = (r.get("kind"), bool(r.get("stable")), r.get("decoder"), r.get("sub"))
            if key != getattr(self, "_auto_logged", None):       # one line per change, so a diagnostics file shows why Auto chose what it did
                self._auto_logged = key
                log.info("auto-identify: %s%s -> %s/%s  (%.4f MHz, snr %s dB, bw %s kHz, duty %s, cv %s, baud %s, line %s, dev %s, levels %s)",
                         r.get("kind"), "" if r.get("stable") else " (not yet stable)", r.get("decoder") or "-", r.get("sub") or "-",
                         self.freq / 1e6, r.get("snr"), r.get("bw"), r.get("duty"), r.get("cv"),
                         r.get("baud", "-"), r.get("line", "-"), r.get("dev", "-"), r.get("levels", "-"))
            self._auto_apply(r)

    def _set_signal(self, r):
        self.signal = r
        self._emit("signal", r)

    def _auto_apply(self, r):
        self._set_signal(r)
        if not r.get("stable"):
            return
        dec, sub = r.get("decoder") or "", r.get("sub") or "auto"
        active = self.decoders.active
        if active and not self.auto_owned:
            return                               # the user (or the memory channel) chose a decoder
        if dec and dec in self.auto_failed:
            return
        if dec:
            if dec == "sstv" and active == "sstv":
                return
            if active == dec and (dec != "dsd" or self.decoders.sub == sub):
                return
            if active == "dsd":
                # A digital decoder that is really locked (a steady run of clean frames) is not swapped or restarted
                # because one second of the classifier looks different (DMR <-> generic 4FSK, TETRA ...).  Tuning to
                # another channel resets auto-identify, so a new signal still gets a fresh decision.
                if self.decoders.dsd_recent_sync(30):
                    return
                # a specific type (DMR, NXDN ...) is not downgraded to the generic "try everything" Auto in the first seconds
                if (dec == "dsd" and sub == "auto" and self.decoders.sub != "auto"
                        and time.time() - getattr(self.decoders, "dsd_t0", 0.0) < 20):
                    return
            mode_before = self.mode
            if r.get("mode") and r["mode"] != self.mode and r["mode"] in MODES:
                self.mode = r["mode"]
                self.rx.set_mode(self.mode)
            ok = self.decoders.start(dec, sub)
            if not ok:
                self.auto_failed.add(dec)
                if self.mode != mode_before:          # nothing to decode with – keep the user's mode
                    self.mode = mode_before
                    self.rx.set_mode(self.mode)
                self._emit("event", {"level": "warn", "text": f"Auto: {r['label']} found, but its decoder "
                                                              "program is missing – see Help › Decoders"})
                return
            self.auto_owned = True
            if dec in ("dsd", "pager", "data", "tetra", "tetrapol"):
                for past in list(self.pre):          # hand over what was heard while the signal was being identified
                    self.decoders.feed(past)
            self.pre.clear()
            if self.mode != mode_before and not self.auto_mode_prev:
                self.auto_mode_prev = mode_before
            self._emit("event", {"level": "info", "text": f"Auto: {r['label']} – decoder started"})
            self._push_state(True)
        elif active and self.auto_owned and r["kind"] in ("FM voice", "AM voice", "SSB voice"):
            # back to plain voice: stop what auto-identify started (an SSTV picture in progress is kept)
            if active == "dsd" and self.decoders.dsd_recent_sync(30):
                return                          # it locked a moment ago: a fading call can look like voice - do not drop the decoder
            if not (active == "sstv" and self.decoders.sstv and self.decoders.sstv.state == "image"):
                self.decoders.stop()
                self.auto_owned = False
                if self.auto_mode_prev and self.auto_mode_prev != self.mode:
                    self.mode = self.auto_mode_prev
                    self.rx.set_mode(self.mode)
                self.auto_mode_prev = None
                self._push_state(True)

        elif not active and r["kind"] in ("FM voice", "AM voice", "SSB voice"):
            self._auto_voice_mode(r)

    def _auto_voice_mode(self, r):
        """Plain analogue voice and no decoder: make sure the demodulator matches what is on the air.

        The hard part is not naming the mode, it is knowing when to act on the name. A bursty signal - ACARS is the
        obvious one - is silence most of the time, and the classifier goes on reading between the bursts, where
        there is nothing to read. Those readings are noise, and noise is as likely to look like FM as AM, so a
        simple "two readings in a row" vote flips the demodulator back and forth about once a second. Demodulating
        an AM signal as FM sounds exactly like bad distortion, which is how this was found.

        So: only readings taken while there is actually a signal get a vote, a run of them is needed, and there is a
        quiet period after each change. And if it still ends up changing its mind repeatedly on one frequency, it
        stops trying until you retune - a classifier that cannot decide is worse than no classifier, because it is
        corrupting the audio every time it swaps.
        """
        want = r.get("mode")
        if not want and r["kind"] == "FM voice" and self.mode in ("DIG", "DIG25"):
            want = "NFM"                     # not digital after all, but a digital demodulator is still selected
        cur = self.mode
        if (not want or getattr(self, "auto_mode_lock", False) or cur in ("WFM", "CW") or want == cur
                or (want == "NFM" and cur == "FM")):
            self.auto_mode_vote = (None, 0)
            return
        if getattr(self, "auto_mode_swaps", 0) >= AUTO_MODE_MAX_SWAPS:
            return                           # it has changed its mind too often here: leave the mode alone
        if not self._auto_on_signal(r):
            return                           # between bursts: no evidence either way, so the vote is left as it was
        now = time.time()
        if now - getattr(self, "auto_mode_t", 0.0) < AUTO_MODE_HOLD_S:
            return                           # one change at a time: give it a chance to be heard before the next
        vm, vn = getattr(self, "auto_mode_vote", (None, 0))
        vn = vn + 1 if vm == want else 1
        self.auto_mode_vote = (want, vn)
        if vn < AUTO_MODE_VOTES:
            return
        self.auto_mode_vote = (None, 0)
        self.auto_mode_t = now
        self.auto_mode_swaps = getattr(self, "auto_mode_swaps", 0) + 1
        self.mode = want
        self.rx.set_mode(want)
        what = {"NFM": "analogue FM voice", "AM": "AM voice", "USB": "upper-sideband voice", "LSB": "lower-sideband voice"}.get(want, r["kind"])
        self._emit("event", {"level": "info", "text": f"Auto: {what} – switched to {want}"})
        if self.auto_mode_swaps >= AUTO_MODE_MAX_SWAPS:
            self._emit("event", {"level": "warn", "text": "Auto: this signal keeps looking like more than one mode, "
                                                          "so the demodulator is being left alone until you retune. "
                                                          "Set the mode by hand if Auto has it wrong."})
        self._push_state(True)

    def _auto_on_signal(self, r):
        """Is there really a signal to judge, or is this the quiet between bursts?

        The squelch is the honest answer where it is set; where it is wide open, fall back on the classifier's own
        signal-to-noise figure. Either way the point is the same: do not let silence cast a vote.
        """
        if self.sq_open:
            return True
        if float(self.s.get("squelch", -119)) > -115:
            return False                     # a squelch is set and it is shut: nothing is being received
        snr = r.get("snr")
        return snr is None or float(snr) >= AUTO_MODE_MIN_SNR

    # ------------------------------------------------------------------ scheduled captures
    def begin_job(self, job):
        def do():
            if self.job is None:
                self.job_prev = {"freq": self.freq, "mode": self.mode, "decoder": self.decoders.active,
                                 "sub": self.decoders.sub, "scan": self.scan["state"],
                                 "recording": self.rec is not None}
            self._stop_scan()
            self._auto_reset()
            self.channel = None
            self.job = job
            self.pass_meter, self.pass_images, self.job_gain = None, 0, None
            self.passlog.open_key = None
            if job.get("sat") and not job.get("geo"):              # a satellite pass: measure it, and use the gain picked for it
                self.pass_meter = passlog.PassMeter(self.pass_el)
                self.freq_check = freqcheck.Tracker(self._nominal_freq(job), "rx1")
                self.passlog.open_key = job["id"]
                g = job.get("gain")
                if g is not None and self.dev and not (self.s["agc"] or getattr(self.dev, "agc", False)):
                    self.job_gain = float(g)
                    self.ag_cur = None
                    self.dev.set_gain(self.job_gain)
                    log.info("pass gain %.1f (%s)", self.job_gain, job.get("gain_mode"))
            self.decoders.pass_key = job.get("id")
            self._tune(job["freq"], job.get("mode"), force=True)
            act = job.get("action")
            if act in ("wefax", "sstv", "lrpt", "goes", "metop") and self.rec:
                self.rec.close()               # picture jobs keep the picture only, not the sound
                self.rec = None
            self.decoders.pass_northbound = bool(job.get("northbound"))      # LRPT: which way the satellite is heading
            if act in ("wefax", "sstv", "lrpt", "goes", "metop", "pager", "data", "dsd"):
                self.decoders.start(act, "auto")
            elif act == "record" and not self.rec:
                self.record(True)
            self._push_state(True)
        self.call(do)

    def _pass_tick(self, spec_db, fs, now):
        m = self.pass_meter
        if m is None or not self.job or now - m._last < 1.0 or not self.dev:
            return
        half = passlog.BAND_HALF_HZ.get(self.job.get("action"), 20e3)
        snr = passlog.band_snr(spec_db, self.dev.center, fs, self.freq, half)
        m.add(now, snr, self.job_gain if self.job_gain is not None else (None if self.s["agc"] else float(self.s["rf_gain"])))
        fc = self.freq_check
        if fc is not None and now - getattr(self, "_fc_t", 0.0) >= FREQ_CHECK_EVERY_S:
            self._fc_t = now
            try:
                off = freqcheck.carrier_offset(spec_db, self.dev.center, fs, self.freq, half)
                look = self.pass_el(now) if callable(getattr(self, "pass_el", None)) else None
                fc.add(now - m.t0, off, el=(look[0] if look else None),
                       doppler=(self.freq - fc.freq) if fc.freq else None)
            except Exception:                              # noqa: BLE001
                log.exception("frequency check")

    def _nominal_freq(self, job):
        """The satellite's own frequency, before any Doppler correction - what the error is measured against."""
        try:
            return float(self.scheduler.sats.cfg()[job["sat"]]["freq"])
        except Exception:                                  # noqa: BLE001
            return float(job.get("freq") or 0.0)

    def _finish_pass(self, job, why):
        meter, self.pass_meter = self.pass_meter, None
        fc, self.freq_check = self.freq_check, None
        self.passlog.open_key = None
        had_gain, self.job_gain = self.job_gain, None
        if had_gain is not None and self.dev:                       # the knob's gain comes back
            self.ag_cur = None
            self.dev.set_gain(float(self.s["rf_gain"]))
        if not job or meter is None:
            return
        sm = meter.summary()
        rec = {"key": job["id"], "sat": job["sat"], "name": str(job["name"]).split(" pass")[0], "action": job.get("action"),
               "start": int(meter.t0), "end": int(time.time()), "max_el": job.get("max_el"), "gain_mode": job.get("gain_mode"),
               "stopped": why, "images": self.pass_images, **sm}
        fres = fc.result() if fc is not None else None
        if fres:
            rec["freq_check"] = {k: fres[k] for k in ("median", "ppm", "spread", "n", "verdict")}
            log.info("%s", freqcheck.sentence(fres))
            if fres["verdict"] in ("oscillator", "orbit"):
                self._emit("event", {"level": "warn", "text": freqcheck.sentence(fres)})
        if rec.get("gain") is None and had_gain is not None:
            rec["gain"] = round(had_gain, 1)
        self.pass_images = 0
        self.passlog.add(rec)
        log.info("pass report: %s peak %s deg, gain %s, SNR peak %s / typical %s dB (score %s, %d samples), %s",
                 rec["name"], rec.get("max_el"), rec.get("gain"), rec.get("snr_peak"), rec.get("snr_med"), rec.get("score"),
                 rec.get("samples", 0), self.passlog.result(rec))

    def _backfill_frames(self):
        try:
            time.sleep(8)                                # let the radio get going first
            framesnr.backfill(self.passlog, self.decoders.images_dir)
        except Exception:                                # noqa: BLE001
            log.exception("frame coverage backfill")

    def _pass_images_done(self, key, shown, total, note="", out_dir=None, fill=None):
        """SatDump finished working on a recorded pass: say how many pictures came out."""
        self.passlog.set_images(key, shown, total, note, fill)
        try:                                                # what level was actually producing frames, from the .cadu
            framesnr.for_pass(self.passlog, key, out_dir)
        except Exception:                                   # noqa: BLE001
            log.exception("frame measurement")
        self.merger.report(key, out_dir, "rx1")             # if the second receiver took this pass too, merge them
        rec = self.passlog.find(key)
        if rec:
            log.info("pass report: %s - %s", rec.get("name"), self.passlog.result(rec))
            self._emit("pass_done", {"name": rec.get("name"), "rx": "", "images": rec.get("images", 0), "fill": rec.get("fill"),
                                     "max_el": rec.get("max_el"), "poor": bool(rec.get("poor"))})
        self._emit("schedule", self.scheduler.status())

    def _merged_pass_done(self, key, rep, images):
        """The merged frames from both receivers have been turned into pictures."""
        try:
            self.passlog.set_merged(key, rep, images)
            rec = self.passlog.find(key) or {}
            mf = (rec.get("merged") or {}).get("fill")
            if mf is not None:
                self._emit("pass_done", {"name": rec.get("name"), "rx": "", "merged_fill": mf, "fill": rec.get("fill"), "images": images})
        except Exception:                                   # noqa: BLE001
            log.exception("merged pass report")
        self._emit("schedule", self.scheduler.status())

    def retune_job(self, f):
        """Nudge the frequency during a satellite pass (Doppler) without restarting anything."""
        def do():
            if not self.job:
                return
            f2 = int(f)
            if f2 == self.freq:
                return
            self.freq = f2
            fs = self.dev.sample_rate if self.dev else 2400000
            if self.dev and abs(f2 - self.dev.center) > fs * 0.42:
                self.dev.set_center(self._hw_center_for(f2, fs))
                self.discard = 2
            self._push_state()
        self.call(do)

    def end_job(self, why=None):
        def do():
            job, prev = self.job, self.job_prev or {}
            self.job, self.job_prev = None, None
            if job and job.get("action") in ("wefax", "sstv", "lrpt", "goes", "metop", "pager", "data", "dsd"):
                self.decoders.stop()
            self.decoders.pass_northbound = False
            self._finish_pass(job, why)
            self.decoders.pass_key = None
            if job and job.get("action") == "record" and self.rec and not prev.get("recording"):
                self.rec.close()
                self.rec = None
            if prev:
                self._tune(prev["freq"], prev["mode"], force=True)
                if prev.get("decoder"):
                    self.decoders.start(prev["decoder"], prev.get("sub") or "auto")
                if prev.get("scan") == "scan":
                    self.start_scan()
            self._push_state(True)
        self.call(do)

    def offset(self):
        return self.freq - (self.dev.center if self.dev else self.freq)

    def aero_centre(self):
        """AERO: move the VFO onto the carrier the carrier finder measured (fine tuning, a few Hz)."""
        def do():
            a = self.decoders.aero
            if not a or self.decoders.active != "aero":
                self._emit("event", {"level": "warn", "text": "Start the AERO decoder first"})
                return
            m = a.measurement()
            if not m:
                self._emit("event", {"level": "warn", "text": "AERO: no carrier found yet – tune close to a narrow carrier and wait a few seconds"})
                return
            off = m[0]
            if abs(off) < 5:
                self._emit("event", {"level": "info", "text": "AERO: already centred on the carrier"})
                return
            self._stop_scan()
            self.channel = None
            self._tune(int(round(self.freq + off)))
            self._push_state(True)
            self._emit("event", {"level": "info", "text": f"AERO: moved {off:+.0f} Hz onto the carrier ({(self.freq) / 1e6:.6f} MHz)"})
        self.call(do)

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
                self.auto_mode_lock = True          # a mode chosen by hand is respected until the next retune
                self.mode = mode
                self.rx.set_mode(mode)
                self._push_state(True)
        self.call(do)

    def _remembered_bw(self, mode):
        """Width a mode starts with: the one chosen by hand for it (not in Auto - then the screens suggest one)."""
        if self.s.get("bw_auto", True):
            return 0
        return int((self.s.get("bw_modes") or {}).get(mode, 0) or 0)

    def set_bandwidth(self, hz, auto=False):
        """hz = channel filter width in Hz (0 = mode default).  auto=True: the screens are following the
        suggested width (not remembered); auto=False: chosen by hand, remembered per mode."""
        def do():
            h = int(hz or 0)
            self.s["bw_auto"] = bool(auto)
            if not auto:
                m = dict(self.s.get("bw_modes") or {})
                if h:
                    m[self.mode] = h
                else:
                    m.pop(self.mode, None)
                self.s["bw_modes"] = m
            if self.rx:
                self.rx.set_bandwidth(h)
            self._push_state(True)
        self.call(do)

    def set_gain_with_squelch(self, g, was):
        """Change the RF gain and carry the squelch with it.

        Squelch is a level in dBFS, so it only means anything against a particular gain: drop the gain by 30 dB and
        every signal drops 30 dB with it, leaving a squelch that nothing can open.  Moving the two together keeps the
        radio behaving exactly as it did, with the same signals opening it.  -120 means wide open, so leave that alone.
        """
        try:
            shift = float(g) - float(was)
        except (TypeError, ValueError):
            shift = 0.0
        sq = float(self.s["squelch"])
        moved = None
        if abs(shift) >= 0.5 and sq > -119:
            moved = max(-119.0, min(0.0, sq + shift))
            self.set_squelch(moved)
        self.set_gain(float(g))
        return moved

    def set_gain(self, g):
        def do():
            self.s["rf_gain"] = float(g)
            self.ag_cur = None
            if self.dev:
                self.dev.set_gain(float(g))
            self._push_state(True)
        self.call(do)

    def _knob(self):
        """The RF gain that applies now: the knob, or the gain picked for a satellite pass."""
        return float(self.job_gain) if self.job_gain is not None else float(self.s["rf_gain"])

    # ------------------------------------------------------------------ finding the best gain
    GCAL_STUCK = 8.0           # no readings for this long means the receiver has stopped: give up on the sweep
    GCAL_SETTLE = 0.45        # seconds to let the receiver settle after a gain change
    GCAL_READ = 0.35          # ...then this long collecting noise-floor readings

    def gaincal_start(self, band=""):
        """Sweep the RF gain and find the lowest setting at which the aerial, not the receiver, is what you hear."""
        dev = self.dev
        if dev is None:
            return False, "No receiver is open."
        if self.job is not None:
            return False, "A scheduled capture is running - try again when it has finished."
        if self.scan["state"] != "manual":
            return False, "Stop scanning or searching first: the gain test needs the radio to stay on one frequency."
        if self.s["agc"] or getattr(dev, "agc", False):
            return False, "Turn the receiver's hardware AGC off first, or it will undo the test."
        if self.gcal is not None:
            return False, "The gain test is already running."
        lo, hi = dev.gain_range()
        gains = gaincal.plan(lo, hi, getattr(dev, "gain_steps", None))
        self.gcal = {"gains": gains, "i": -1, "t": 0.0, "pts": [], "buf": [], "band": str(band or ""),
                     "freq": int(self.freq), "was": self._knob(), "ovl0": int(getattr(dev, "overloads", 0) or 0),
                     "ovl_from": None, "dev": getattr(dev, "key", "") or "", "started": time.time()}
        log.info("gain test: %d steps from %.1f to %.1f dB at %.4f MHz", len(gains), gains[0], gains[-1], self.freq / 1e6)
        self._gcal_next(time.time())
        return True, ""

    def gaincal_stop(self, why="stopped"):
        g, self.gcal = self.gcal, None
        if g and self.dev:
            try:
                self.dev.set_gain(float(g["was"]))        # put the knob back where it was
            except Exception:                             # noqa: BLE001
                pass
        if g:
            self._emit("gaincal", {"running": False, "why": why})
            self._push_state(True)

    def _gcal_next(self, now):
        g = self.gcal
        g["i"] += 1
        if g["i"] >= len(g["gains"]):
            self._gcal_finish()
            return
        g["t"] = now
        g["buf"] = []
        try:
            self.dev.set_gain(float(g["gains"][g["i"]]))
        except Exception:                                 # noqa: BLE001
            log.exception("gain test")
            self.gaincal_stop("the receiver would not change gain")
            return
        self._emit("gaincal", {"running": True, "step": g["i"] + 1, "steps": len(g["gains"]),
                               "gain": g["gains"][g["i"]]})

    def _gcal_watch(self, now):
        """Abandon a sweep that has stopped getting readings.

        The sweep only advances when a block of samples arrives. If the receiver stops delivering - another program has
        grabbed it, the USB has dropped, the driver has errored - every step after that silently never happens, and the
        gain is left wherever the test last put it. That is worse than not running the test at all, so give up and put
        the gain back.
        """
        g = self.gcal
        if g is None or g["t"] <= 0.0 or now - g["t"] < self.GCAL_STUCK:
            return
        log.warning("gain test: no spectrum for %.1f s at step %d of %d - abandoning",
                    now - g["t"], g["i"] + 1, len(g["gains"]))
        self.gaincal_stop("the receiver stopped sending samples part-way through, so the test was abandoned and the "
                          "gain put back. Worth checking no other program has the receiver open, then trying again.")

    def _gaincal_tick(self, spec_db, now):
        g = self.gcal
        if g is None or spec_db is None or self.dev is None:
            return
        if now - g["t"] < self.GCAL_SETTLE:
            return
        g["buf"].append(float(np.median(spec_db)))        # the noise floor: the middle of the spectrum, ignoring signals
        if now - g["t"] < self.GCAL_SETTLE + self.GCAL_READ:
            return
        floor = float(np.median(g["buf"])) if g["buf"] else None
        g["pts"].append((g["gains"][g["i"]], floor))
        ovl = int(getattr(self.dev, "overloads", 0) or 0)
        if ovl > g["ovl0"] and g["ovl_from"] is None:
            g["ovl_from"] = g["gains"][g["i"]]
        g["ovl0"] = ovl
        self._gcal_next(now)

    def _gcal_finish(self):
        g, self.gcal = self.gcal, None
        res = gaincal.choose(g["pts"], overload_from=g["ovl_from"], keep=g["was"])
        if not res:
            if self.dev:
                self.dev.set_gain(float(g["was"]))
            self._emit("gaincal", {"running": False, "why": "not enough readings to decide"})
            self._push_state(True)
            return
        res.update(band=g["band"], freq=g["freq"], dev=g["dev"], when=int(time.time()), was=g["was"])
        self._gcal_save(res)
        sq_was = float(self.s["squelch"])
        res["squelch_was"] = sq_was
        shift = float(res["gain"]) - float(g["was"])
        if abs(shift) >= 0.5 and sq_was > -119:
            res["squelch"] = max(-119.0, min(0.0, sq_was + shift))
            self.s["squelch"] = res["squelch"]
            if self.rx:
                self.rx.squelch_db = res["squelch"]
        self.s["rf_gain"] = float(res["gain"])
        self.s.save()
        self.ag_cur = None
        if self.dev:
            self.dev.set_gain(float(res["gain"]))
        log.info("gain test: %.1f dB chosen at %.4f MHz (%s) - %s", res["gain"], g["freq"] / 1e6, g["band"] or "no band", res["why"])
        log.info("gain test readings (gain dB -> noise floor dBFS): %s",
                 ", ".join(f"{a:.1f}->{b:.1f}" for a, b in res["points"]))
        self._emit("gaincal", dict(res, running=False, done=True))
        note = f"RF gain set to {res['gain']:.0f} dB."
        if res.get("squelch") is not None:
            note += f" Squelch moved with it, {sq_was:.0f} to {res['squelch']:.0f} dB, so the same signals still open it."
        self._emit("event", {"level": "info", "text": note + " " + res["why"]})
        self._push_state(True)

    def _gcal_save(self, res):
        """Remember the answer for this band and receiver, so it can be put back when you come here again."""
        try:
            saved = dict(self.s.get("gain_cal") or {})
        except Exception:                                 # noqa: BLE001
            saved = {}
        key = f"{res['dev']}|{res['band'] or round(res['freq'] / 1e6, 1)}"
        saved[key] = {k: res[k] for k in ("gain", "band", "freq", "dev", "when", "why", "slope")}
        if len(saved) > 60:
            for k in sorted(saved, key=lambda k: saved[k].get("when", 0))[:len(saved) - 60]:
                saved.pop(k, None)
        self.s["gain_cal"] = saved
        self.s.save()

    def gaincal_saved(self):
        try:
            return sorted((dict(v, key=k) for k, v in (self.s.get("gain_cal") or {}).items()),
                          key=lambda v: v.get("when", 0), reverse=True)
        except Exception:                                 # noqa: BLE001
            return []

    @staticmethod
    def _ag_step(span):
        """How far one automatic-gain step moves, in dB.

        A fraction of the receiver's range alone is wrong: the SDRplay's range is 0-100, so six per cent of it is a
        6 dB jump, which is audible on voice and is enough on its own to throw a decoder. Capped so a wide-range
        receiver steps as gently as a narrow one.
        """
        return max(1.0, min(AG_STEP_FRAC * span, AG_STEP_MAX_DB))

    def _autogain_tick(self, now):
        """Auto RF gain (optional): the RF GAIN knob is the most gain you want. When a signal in the channel is so strong that it
        distorts (a digital decoder will not lock on a distorted signal), ease the gain down a step at a time; going to another
        frequency starts again from the knob's value. It never raises the gain above the knob."""
        dev = self.dev
        if dev is None:
            return
        ceiling = self._knob()
        steady = getattr(getattr(self, "decoders", None), "active", None) in AG_STEADY
        if not self.s.get("auto_gain") or self.job_gain is not None or self.s["agc"] or getattr(dev, "agc", False) \
                or self.scan["state"] != "manual" or steady:
            if self.ag_cur is not None:                  # switched off or not applicable: the knob's value comes back
                self.ag_cur = None
                dev.set_gain(ceiling)
            self.ag_over = 0
            return
        if self.ag_cur is None or self.freq != self.ag_freq:
            self.ag_freq = self.freq
            self.ag_over = 0
            if self.ag_cur is not None and self.ag_cur != ceiling:
                dev.set_gain(ceiling)
            self.ag_cur = ceiling
        self.ag_over = self.ag_over + 1 if self.level_db > AG_HIGH_DB else 0
        ovl = int(getattr(dev, "overloads", 0) or 0)
        overloaded = ovl > self.ag_ovl_seen               # the receiver's own ADC overload report: strong signals somewhere in the band
        self.ag_ovl_seen = ovl
        if overloaded:
            self.ag_ovl_t = now
        lo, hi = dev.gain_range()
        span = max(1.0, float(hi) - float(lo))
        # Climb back: once the strong signal has gone (the channel is quiet for a while) the gain returns one step at a time
        # towards the knob, so a weaker caller on the same frequency is not lost to a gain set for the strong one before it.
        if self.ag_cur < ceiling and self.level_db < AG_HIGH_DB - AG_QUIET_DB and now - self.ag_ovl_t >= AG_RECOVER_S:
            if self.ag_quiet_t is None:
                self.ag_quiet_t = now
            if now - self.ag_quiet_t >= AG_RECOVER_S and now >= self.ag_hold_t and now - self.ag_t >= AG_SETTLE_S:
                self.ag_cur = min(ceiling, self.ag_cur + self._ag_step(span))
                self.ag_t = self.ag_raised_t = now
                self.ag_quiet_t = now
                dev.set_gain(self.ag_cur)
                return
        else:
            self.ag_quiet_t = None
        if (self.ag_over < AG_BLOCKS and not overloaded) or now - self.ag_t < AG_SETTLE_S:
            return
        if now - self.ag_raised_t < 20.0:                # it was pushed down again soon after climbing: stop trying for a while
            self.ag_hold_t = now + AG_HOLD_S
        drop = min((AG_OVL_DROP if overloaded else AG_MAX_DROP) * span, AG_DROP_MAX_DB)
        floor = max(float(lo), ceiling - drop)
        if self.ag_cur <= floor:
            return
        self.ag_cur = max(floor, self.ag_cur - self._ag_step(span))
        self.ag_t = now
        self.ag_over = 0
        dev.set_gain(self.ag_cur)
        if now - self.ag_log_t > 5:
            self.ag_log_t = now
            log.info("auto RF gain: %s - gain eased to %.1f (knob %.1f)",
                     "receiver overload" if overloaded else "strong signal (%.0f dB)" % self.level_db, self.ag_cur, ceiling)

    def set_agc(self, on):
        def do():
            self.s["agc"] = bool(on)
            if self.dev:
                self.dev.set_agc(bool(on))
            self._push_state(True)
        self.call(do)

    def set_notch(self, on=None, hz=None, q=None, auto=None):
        """Audio notch filters: manual (on / pitch in Hz / sharpness Q) and the auto-notch."""
        def do():
            for key, v in (("notch_on", on), ("notch_hz", hz), ("notch_q", q), ("auto_notch", auto)):
                if v is not None:
                    self.s[key] = bool(v) if key in ("notch_on", "auto_notch") else float(v)
            if self.rx:
                self.rx.notch.configure(man_on=bool(self.s.get("notch_on", False)), man_hz=float(self.s.get("notch_hz", 1000)),
                                        man_q=float(self.s.get("notch_q", 20)), auto_on=bool(self.s.get("auto_notch", False)))
            self._push_state(True)
        self.call(do)

    def set_hotspot(self, name=None, freq=None, cc=None, ts=None, tg=None):
        """Save the DMR hotspot profile (frequency, colour code, timeslot, talkgroup)."""
        def do():
            if name is not None:
                self.s["hotspot_name"] = str(name)[:30] or "My hotspot"
            if freq:
                self.s["hotspot_freq"] = int(freq)
            if cc is not None:
                self.s["hotspot_cc"] = max(0, min(15, int(cc)))
            if ts is not None:
                self.s["hotspot_ts"] = 1 if int(ts) == 1 else 2
            if tg is not None:
                self.s["hotspot_tg"] = str(tg)[:10]
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
                self._bias_check()
            elif key == "ppm":
                self.s["ppm"] = int(value)
                self.dev.set_ppm(int(value))
            elif key == "sample_rate":
                self.s["sample_rate"] = int(value)
                self._open_device(self.dev.key)
                return
            else:
                self.dev.write_setting(key, value)
                if key in ("bias_t", "fm_notch", "dab_notch", "biastee"):        # remember it for the next start
                    sw = dict(self.s.get("dev_switches", {}) or {})
                    sw[self.dev.key] = {**sw.get(self.dev.key, {}), key: str(value).lower() in ("1", "true", "on", "yes")}
                    self.s["dev_switches"] = sw
                    self.s.save()
                    self._bias_check()
            self._emit("device", self.device_info())
        self.call(do)

    BIAS_KEYS = ("bias_t", "biastee")

    def _bias_state(self):
        """(on, can_reach_antenna) for the open receiver; on is None if it has no bias-T switch."""
        d = self.dev
        if d is None:
            return None, False
        fl = getattr(d, "flags", None)
        if isinstance(fl, dict) and "bias_t" in fl:
            on = bool(fl["bias_t"])
            eff = True
            if getattr(d, "hw", None) in (2, 4, 7):
                eff = (getattr(d, "antenna", "") or "Antenna A") == "Antenna B"
            return on, eff
        if hasattr(d, "bias") and hasattr(d, "write_setting"):
            return bool(d.bias), True
        return None, False

    def _bias_tick(self, spec_db, now):
        on, eff = self._bias_state()
        if on is None:
            return
        d = self.dev
        key = (d.center, round(float(self.s["rf_gain"]), 1), getattr(d, "antenna", ""), d.sample_rate, on)
        self.biasmon.update(float(np.median(spec_db)), key, now, getattr(d, "overloads", 0))
        if now - self._bias_emit >= 1.0:
            self._bias_emit = now
            self._emit("bias", self.bias_snapshot())

    def bias_snapshot(self):
        on, eff = self._bias_state()
        d = self.dev
        return self.biasmon.snapshot(on, eff, getattr(d, "antenna", "") or ("Antenna A" if getattr(d, "hw", None) == 2 else ""),
                                     getattr(d, "overloads", 0))

    def bias_test(self):
        """Switch the bias-T off for a few seconds, then back on, and compare the noise floor (a real working check)."""
        on, eff = self._bias_state()
        m = self.biasmon
        if on is None or m.testing:
            return
        if not on:
            self._emit("event", {"level": "warn", "text": "Bias-T check: switch bias-T on first (it compares power on and off)."})
            return
        if self.s["agc"] or getattr(self.dev, "agc", False):
            m.result = {"state": "blocked", "text": "Power check not run: Hardware AGC is on, and it turns the gain down when an LNA lifts the signal, "
                                                    "so the change would be hidden. Untick Hardware AGC (Settings › Receivers), set a fixed RF gain, then run it again."}
            self._emit("event", {"level": "warn", "text": "Bias-T check: untick Hardware AGC first (it hides the change)."})
            self._emit("bias", self.bias_snapshot())
            return
        if self.job is not None:
            self._emit("event", {"level": "warn", "text": "Bias-T check: wait until the scheduled capture has finished (power is cut for a few seconds)."})
            return
        dev = self.dev
        key = "bias_t" if isinstance(getattr(dev, "flags", None), dict) else "biastee"
        m.testing = True
        m.result = {"state": "running", "text": "Testing: bias-T off for 6 s, then on for 8 s ..."}

        def run():
            try:
                self.call(lambda: dev.write_setting(key, "false"))
                time.sleep(6.0)
                off_db = m.recent(3)
                self.call(lambda: dev.write_setting(key, "true"))
                time.sleep(8.0)
                on_db = m.recent(3)
                if on_db is None or off_db is None:
                    m.result = {"state": "none", "text": "Not enough signal data to compare - try again."}
                else:
                    v, text = judge(on_db, off_db)
                    m.result = {"state": v, "text": text, "on_db": round(on_db, 1), "off_db": round(off_db, 1)}
            finally:
                self.call(lambda: dev.write_setting(key, "true"))      # always leave it as it was (on)
                m.testing = False
                m.baseline = None
            self._emit("bias", self.bias_snapshot())
        threading.Thread(target=run, name="bias-test", daemon=True).start()

    def _bias_check(self):
        """SDRplay RSP2 / RSPdx only put bias-T power on Antenna B: say so if it is on but another input is selected."""
        d = self.dev
        flags = getattr(d, "flags", None)
        if not flags or not flags.get("bias_t") or getattr(d, "hw", None) not in (2, 4, 7):
            return
        ant = getattr(d, "antenna", "") or "Antenna A"
        if ant != "Antenna B":
            self._emit("event", {"level": "warn", "text": f"Bias-T is ON but {ant} is selected - the RSP only powers an antenna or LNA "
                                                          "through Antenna B. Choose Antenna B in Settings › Receivers."})

    def set_decoder(self, dec_id, sub="auto"):
        def do():
            if dec_id == "adsb":
                self._begin_adsb()
            elif dec_id == "meshsdr":
                self.start_meshsdr()
            elif dec_id == "ism":
                self.start_ism()
            elif dec_id in ("goes", "metop"):
                self._stop_scan()
                self.channel = None
                self._tune(1694100000 if dec_id == "goes" else 1701300000, "DIG25", force=True)
                self.decoders.start(dec_id, sub)
            elif dec_id == "aero":
                self._stop_scan()
                self.channel = None
                lo, hi = aero.BAND_MHZ
                f = self.freq if lo * 1e6 <= self.freq <= hi * 1e6 else int(aero.CHANNELS[0]["mhz"] * 1e6)
                self._tune(f, "DIG25", force=True)            # IQ decoder: the receiver is re-centred, the VFO stays on the channel
                self.decoders.start(dec_id, sub)
            else:
                if dec_id == "acars" and self.mode != "AM":
                    self.mode = "AM"                      # ACARS is AM audio
                    self.rx.set_mode("AM")
                elif dec_id == "tetrapol" and self.mode not in ("DIG", "DIG25"):
                    self.mode = "DIG"                     # a 12.5 kHz wide digital channel (the decoder reads the IQ itself)
                    self.rx.set_mode("DIG")
                elif dec_id == "vdl2" and self.mode != "DIG25":
                    self.mode = "DIG25"                   # a 25 kHz wide digital channel (the decoder reads the IQ itself)
                    self.rx.set_mode("DIG25")
                elif dec_id == "sonde" and (self.mode != "NFM" or self.rx.bandwidth() < 14000):
                    self.mode = "NFM"                     # the sonde's FSK needs a wider channel than a voice radio
                    self.rx.set_mode("NFM")
                    self.rx.set_bandwidth(16000)
                if dec_id in ("wefax", "sstv", "lrpt") and self.rec:
                    self.rec.close()           # picture decoders keep the picture only, not the sound
                    self.rec = None
                    self._emit("event", {"level": "info", "text": "Sound recording stopped – picture decoders save the picture only"})
                was_adsb = self.decoders.active == "adsb"
                self.decoders.start(dec_id, sub)
                if was_adsb:
                    self._tune(self.freq, force=True)       # back to the normal off-centre tuning
            self._push_state(True)
        self.call(do)

    # ------------------------------------------------------------------ ADS-B
    def _begin_adsb(self):
        """Tune to 1090 MHz, centre the hardware exactly on it and start the aircraft decoder."""
        self._stop_scan()
        self._auto_reset()
        self.channel = None
        if self.dev and self.dev.sample_rate < 2000000:
            self.s["sample_rate"] = 2400000
            self._open_device(self.dev.key)
        if not self.decoders.start("adsb"):
            return
        self.freq = ADSB_FREQ
        self.dev.set_center(ADSB_FREQ)
        self.discard = 3
        self.spec_fresh = False
        self._emit("event", {"level": "info", "text": "ADS-B: tuned to 1090 MHz"})
        self._emit("device", self.device_info())

    # ------------------------------------------------------------------ MeshCore over SDR (receive only)
    def start_meshsdr(self):
        def do():
            self._stop_scan()
            self._auto_reset()
            self.channel = None
            cfg = self.meshsdr.cfg()
            if self.dev and self.dev.sample_rate < 250000:
                self.s["sample_rate"] = 2400000
                self._open_device(self.dev.key)
            self._tune(cfg["freq"], "DIG", force=True)
            if not self.decoders.start("meshsdr"):
                return
            self.discard = 3
            self._emit("event", {"level": "info", "text": "MeshCore: listening on %.3f MHz (SF%d, %.1f kHz) – receive only"
                                 % (cfg["freq"] / 1e6, cfg["sf"], cfg["bw"] / 1e3)})
            self._push_state(True)
        self.call(do)

    def stop_meshsdr(self):
        def do():
            if self.decoders.active == "meshsdr":
                self.decoders.stop()
            self._push_state(True)
        self.call(do)

    # ------------------------------------------------------------------ ISM sensors (rtl_433)
    def start_ism(self, freq=None):
        """Tune to an ISM sensor frequency (433.92 MHz unless told otherwise) and start rtl_433 on the receiver."""
        def do():
            f = int(freq or 0)
            if not 100e6 < f < 1.1e9:
                f = 433920000
            self._stop_scan()
            self._auto_reset()
            self.channel = None
            if self.dev and self.dev.sample_rate < 250000:
                self.s["sample_rate"] = 2400000
                self._open_device(self.dev.key)
            self.decoders.stop()
            self._tune(f, "DIG", force=True)
            if not self.decoders.start("ism"):
                return
            self.discard = 3
            self._emit("event", {"level": "info", "text": "Sensors: listening on %.4f MHz with rtl_433" % (f / 1e6)})
            self._push_state(True)
        self.call(do)

    def stop_ism(self):
        def do():
            if self.decoders.active == "ism":
                self.decoders.stop()
            self._push_state(True)
        self.call(do)

    def start_adsb(self):
        self.call(lambda: (self._begin_adsb(), self._push_state(True)))

    def stop_adsb(self):
        def do():
            if self.decoders.active == "adsb":
                self.decoders.stop()
                self._tune(self.freq, force=True)
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
        if dec == "adsb":
            # only when the user recalled the channel – a scan must not be cancelled by a 1090 MHz memory
            if self.s.get("auto_decoder", True) and self.scan["state"] == "manual" and self.decoders.active != "adsb":
                self._begin_adsb()
        elif dec and dec != self.decoders.active and self.s.get("auto_decoder", True):
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

    def store_repeater(self, bank, freq_hz, mode, name, tone="", decoder=""):
        """Put one repeater (its output frequency) into the first free slot of a bank."""
        def do():
            slot = self.memory.free_slot(int(bank))
            if slot is None:
                self._emit("event", {"level": "warn", "text": f"Bank {bank} is full"})
                return
            self.memory.store(int(bank), slot, {"freq": int(freq_hz), "mode": mode, "name": name,
                                                "decoder": decoder, "tone": tone})
            self._emit("memory", self.memory.all())
            self._emit("event", {"level": "info", "text": f"Stored {name} ({freq_hz / 1e6:.4f} MHz) in bank {bank} ch {slot}"})
        self.call(do)

    def store_repeaters(self, bank, channels, replace=False, bank_name=None, scan=False):
        def do():
            added, overflow = self.memory.import_channels(int(bank), channels, replace=replace, bank_name=bank_name,
                                                          max_banks=1)
            self._emit("memory", self.memory.all())
            txt = f"Stored {added} repeaters in bank {bank}"
            if overflow:
                txt += f" - {overflow} did not fit (a bank holds 100; empty another bank or narrow the list)"
            self._emit("event", {"level": "info" if not overflow else "warn", "text": txt})
            if scan and added:
                self._begin_scan(only_bank=int(bank))
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

    SEARCH_SKIP_S = 20

    def skip(self):
        """SKIP key: leave whatever the scanner is stopped on (or waiting on) and carry straight on to the next memory
        channel / search frequency.  Nothing is locked out - it will be visited again on the next pass."""
        def do():
            sc = self.scan
            st = sc["state"]
            if st not in ("scan", "search", "hold"):
                self._emit("event", {"level": "info", "text": "SKIP works while scanning or searching - press SCAN or SRCH first"})
                return
            base = sc.get("prev", "scan") if st == "hold" else st
            on_hit = st == "hold" or sc.get("phase") == "check"
            sc["state"] = base
            sc["phase"] = "hop" if base == "search" else "next"
            sc["resume_at"] = 0
            note = ""
            if base == "search" and on_hit:
                # a search re-listens to the same span at once and would stop on the same signal again, so a
                # signal that is skipped is ignored for a short while (memory scanning simply moves to the next channel)
                self.temp_lock[("f", int(self.freq))] = (time.time() + self.SEARCH_SKIP_S, f"{self.freq / 1e6:.4f} MHz")
                note = f" (ignored for {self.SEARCH_SKIP_S} s)"
            self._emit("event", {"level": "info", "text": f"Skipped {self.freq / 1e6:.4f} MHz{note} - scanning on"})
            self._push_state(True)
        self.call(do)

    # ------------------------------------------------------------------ temporary lockout (skip for a while)
    def _temp_locked(self, key, now):
        v = self.temp_lock.get(key)
        if v is None:
            return False
        if v[0] <= now:
            del self.temp_lock[key]
            return False
        return True

    def _temp_locked_freq(self, f, now, tol):
        for k in list(self.temp_lock):
            if k[0] == "f" and abs(k[1] - f) <= tol and self._temp_locked(k, now):
                return True
        return False

    def lockout_temp(self, minutes=30):
        """SKIP button: ignore what the scanner is stopped on (or the current channel/frequency) for a while.
        minutes = 0 means until AndySDR is closed or the skips are cleared."""
        def do():
            now = time.time()
            exp = now + float(minutes) * 60 if minutes and float(minutes) > 0 else float("inf")
            sc = self.scan
            on_search = sc["state"] == "search" or (sc["state"] == "hold" and sc.get("prev") == "search")
            if self.channel and not on_search:
                b, ch, c = self.channel
                key, label = ("m", b, ch), (c.get("name") or f"{self.freq / 1e6:.4f} MHz")
            else:
                key, label = ("f", int(self.freq)), f"{self.freq / 1e6:.4f} MHz"
            self.temp_lock[key] = (exp, label)
            when = "this session" if exp == float("inf") else f"{int(float(minutes))} min"
            self._emit("event", {"level": "info", "text": f"Skipping {label} for {when}"})
            if sc["state"] == "hold":                  # carry straight on to the next channel / frequency
                sc["state"] = sc.get("prev", "scan")
                sc["phase"] = "hop" if sc["state"] == "search" else "next"
            self._push_state(True)
        self.call(do)

    def lockout_clear(self):
        def do():
            n = len(self.temp_lock) + len(self.search_lockouts)
            self.temp_lock.clear()
            self.search_lockouts.clear()
            self._emit("event", {"level": "info", "text": f"Cleared {n} skipped channel(s) – scanning them again" if n else "Nothing was being skipped"})
            self._push_state(True)
        self.call(do)

    def lockout_info(self):
        now = time.time()
        for k in list(self.temp_lock):
            self._temp_locked(k, now)                  # drop the expired ones
        items = []
        for k, (exp, label) in self.temp_lock.items():
            left = "session" if exp == float("inf") else f"{max(1, int((exp - now) / 60 + 0.5))} min"
            items.append(f"{label} ({left})")
        items += [f"{f / 1e6:.4f} MHz (search)" for f in sorted(self.search_lockouts)]
        cur = False
        if self.channel:
            cur = ("m", self.channel[0], self.channel[1]) in self.temp_lock
        return {"count": len(items), "list": items[:25], "current": cur}

    def set_priority(self, bank=None, ch=None):
        def do():
            self.priority = None if bank is None else (int(bank), int(ch))
            self._push_state(True)
        self.call(do)

    def replay_pcm(self, secs):
        """Sound heard in the last `secs` seconds (silences squeezed out), as (pcm bytes, freq, mode) or None."""
        secs = max(5, min(REPLAY_MAX_S, float(secs or 30)))
        t_from = time.time() - secs
        parts, last_t, freq, mode = [], None, 0, ""
        gap = b"\0\0" * int(AUDIO_RATE * 0.5)
        for t, pcm, f, m in list(self.replay):
            if t < t_from:
                continue
            if last_t is not None and t - last_t > 0.4:
                parts.append(gap)                          # a pause between two transmissions
            parts.append(pcm)
            last_t, freq, mode = t, f, m
        return (b"".join(parts), freq, mode) if parts else None

    def replay_wav(self, secs):
        got = self.replay_pcm(secs)
        if not got:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(AUDIO_RATE)
            w.writeframes(got[0])
        return buf.getvalue()

    def replay_save(self, secs):
        """Keep the replay buffer as a normal recording. Returns the file name or None."""
        got = self.replay_pcm(secs)
        if not got:
            return None
        pcm, freq, mode = got
        name = time.strftime("audio_%Y%m%d_%H%M%S_") + f"{freq / 1e6:.4f}MHz.wav"
        path = os.path.join(data_dir(), "recordings", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(AUDIO_RATE)
            w.writeframes(pcm)
        capturemeta.note("record", freq, f"Replay of the last {int(secs)} s", mode)
        return name

    def record(self, on):
        def do():
            if on and not self.rec:
                if self.decoders.active in ("wefax", "sstv", "lrpt"):
                    self._emit("event", {"level": "info", "text": "A picture decoder is running – it saves the picture only, so no sound is recorded"})
                    return
                path = os.path.join(data_dir(), "recordings",
                                    time.strftime("audio_%Y%m%d_%H%M%S_") + f"{self.freq / 1e6:.4f}MHz.wav")
                # what is it? scheduled job name, else the memory channel name, else Auto ID's label
                what = ((self.job or {}).get("name")
                        or (self.channel[2].get("name", "") if self.channel else "")
                        or ((self.signal or {}).get("label", "") if self.s.get("auto_identify", True) else ""))
                capturemeta.note("record", self.freq, what, self.mode)
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
        self.call(self._begin_scan)

    def _begin_scan(self, only_bank=None):
        """On the radio thread.  only_bank scans just that one bank."""
        lst = self.memory.scan_list(only_bank)
        if not lst:
            self._emit("event", {"level": "warn", "text": "No memory channels to scan – store or import some first"})
            return
        if self.s["squelch"] <= -119:
            self._emit("event", {"level": "warn", "text": "Turn SQUELCH up – scanning needs the squelch closed on noise"})
        self.scan = {"state": "scan", "list": lst, "idx": -1, "dir": 1, "phase": "next",
                     "t": 0, "resume_at": 0}
        self._push_state(True)

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
            if self.temp_lock and self._temp_locked(("m", b, ch), now):
                continue          # skipped for a while (SKIP button)
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
                if sc["lo"] <= f <= sc["hi"] and f not in self.search_lockouts and \
                        not (self.temp_lock and self._temp_locked_freq(f, now, max(2500, step // 2))):
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
            "gain_eff": (round(self.job_gain, 1) if self.job_gain is not None and self.job_gain != float(self.s["rf_gain"]) else round(self.ag_cur, 1) if self.ag_cur is not None and self.ag_cur != float(self.s["rf_gain"]) else None),
            "auto_gain": bool(self.s.get("auto_gain")),
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
            "lockouts": self.lockout_info(),
            "auto_identify": bool(self.s.get("auto_identify", True)),
            "mute_digital_audio": bool(self.s.get("mute_digital_audio", True)),
            "dsd_gate": bool(self.s.get("dsd_gate", False)),
            "bw": self.rx.bandwidth() if self.rx else 0, "bw_auto": bool(self.s.get("bw_auto", True)),
            "auto_owned": self.auto_owned,
            "hotspot": {"name": str(self.s.get("hotspot_name", "My hotspot")), "freq": int(self.s.get("hotspot_freq", 435000000)),
                        "cc": int(self.s.get("hotspot_cc", 1)), "ts": int(self.s.get("hotspot_ts", 2)), "tg": str(self.s.get("hotspot_tg", ""))},
            "notch": {"on": bool(self.s.get("notch_on", False)), "hz": float(self.s.get("notch_hz", 1000)),
                      "q": float(self.s.get("notch_q", 20)), "auto": bool(self.s.get("auto_notch", False)),
                      "found": list(self.rx.notch.active) if self.rx else []},
            "signal": self.signal,
            "job": {"name": self.job["name"], "action": self.job.get("action")} if self.job else None,
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
            self._gcal_watch(time.time())     # runs even when no samples arrive, which is the case it is there for
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
            if self.pass_meter is not None:
                self._pass_tick(spec_db, fs, time.time())
            self._bias_tick(spec_db, time.time())
            self._gaincal_tick(spec_db, time.time())
            q = np.clip((spec_db - SPEC_MIN_DB) / (SPEC_MAX_DB - SPEC_MIN_DB) * 255, 0, 255).astype(np.uint8)
            self._emit("spectrum", {"center": self.dev.center, "fs": fs, "freq": self.freq,
                                    "data": q.tobytes(), "min": SPEC_MIN_DB, "max": SPEC_MAX_DB})

            out = self.rx.process(iq, self.offset())
            self.level_db = out["level_db"]
            self.sq_open = out["squelch_open"]
            self._autogain_tick(time.time())

            produce = True
            if self.scan["state"] != "manual":
                produce = self._scan_tick(spec_db)

            # priority channel check (Pro-2045 style: every 2 s, only if in view)
            if self.priority and time.time() - last_pri > 2 and self.scan["state"] in ("manual", "scan"):
                last_pri = time.time()
                self._priority_check(spec_db)

            if self.decoders.active in ("adsb", "goes", "metop", "meshsdr", "ism", "aero"):
                self.decoders.feed_iq(iq, fs, self.offset())    # these decoders work on the raw samples; no audio
            elif produce:
                try:
                    self._auto_feed(out)
                except Exception:
                    log.exception("auto-identify")
                self.decoders.feed(out)
                audio = out["audio"]
                if self.decoders.active in MUTE_WHEN_DECODING and self.s.get("mute_digital_audio", True):
                    audio = np.zeros_like(audio)         # the decoder plays the decoded voice itself - no coded noise
                pcm = np.clip(audio * 32767, -32767, 32767).astype("<i2").tobytes()
                self._emit("audio", pcm)
                if self.rec:
                    self.rec.writeframes(pcm)
                if np.any(audio):
                    self.replay.append((time.time(), pcm, self.freq, self.mode))
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
