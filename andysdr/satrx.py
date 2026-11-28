"""A second receiver used only for 137 MHz weather-satellite passes (Meteor LRPT).

Typical set-up: the RSP2 Pro stays free for normal listening while a cheap RTL-SDR dongle (a Nooelec NESDR SMArt v5, say)
on its own 137 MHz antenna records the pass.  The dongle is opened when a pass starts and let go of when it ends, so it
never gets in the way of anything else.  The recording goes through the same decoder path (SatDump) as before.

If the dongle is not plugged in, is the same one the main radio uses, or fails to open, the scheduler simply gives the
pass to the main radio as it always did.
"""
import logging
import os
import threading
import time

import numpy as np

from . import devices, framesnr, freqcheck, gaincal, passlog
from .config import data_dir
from .decoders import DecoderManager
from .dsp import Receiver, Spectrum

log = logging.getLogger("andysdr.satrx")

BLOCK_SECONDS = 0.05
SAMPLE_RATE = 1200000            # a whole multiple of 240 kS/s, and easy on a USB hub
ENUM_EVERY = 20.0                # seconds between looks for the dongle
RETRY_AFTER_FAIL = 180.0         # after a failed start, leave the pass to the main radio for a while
SPEC_MIN_DB, SPEC_MAX_DB = -130.0, -10.0       # same scale as the main spectrum
SPEC_EVERY = 3                    # send every 3rd block (about 7 pictures a second) to the screens
PREVIEW_SECONDS = 90              # a "live view" test stops by itself (unless a longer time is chosen)
PREVIEW_MAX_SECONDS = 4 * 3600    # "until I stop it" is still ended after 4 hours so a forgotten view cannot hold the dongle for ever
PREVIEW_FREQ = 137900000
ACTIONS = ("lrpt",)              # what this receiver records (137 MHz Meteor pictures)

# the settings configure() writes, keyed by the name used in a satrx_cfg request
SETTINGS = {"enabled": "satrx_enabled", "device": "satrx_device", "gain": "satrx_gain",
            "gain_mode": "satrx_gain_mode", "both": "satrx_both", "bias": "satrx_bias", "ppm": "satrx_ppm"}


def takes(sid, sat):
    """Does this second receiver record that satellite?  Only the 137 MHz LRPT ones."""
    return sat.get("action") in ACTIONS and sat.get("kind") != "geo"


class SatRx:
    def __init__(self, radio):
        self.radio = radio
        self.s = radio.s
        self.lock = threading.RLock()
        self.passlog = passlog.PassLog(os.path.join(data_dir(), "passes_rx2.jsonl"))
        self.decoders = DecoderManager(self.s, self._emit, tag="rx2")
        self.decoders.freq_fn = lambda: self.freq
        self.decoders.job_fn = lambda: self.job
        self.decoders.rate_fn = lambda: self.fs
        self.decoders.pass_done = self._pass_images_done
        self.dev = None
        self.rx = None
        self.spec = Spectrum(2048)
        self.gcal = None          # a gain calibration sweep in progress
        self.thread = None
        self.gen = 0
        self.job = None
        self.freq = 0
        self.fs = SAMPLE_RATE
        self.discard = 0
        self.pass_el = None
        self.pass_meter = None
        self.freq_check = None    # whether this receiver is really on the frequency the orbit says
        self.job_gain = None
        self.pass_images = 0
        self.snr = None
        self.snr_hist = []
        self.recent_images = []
        self.cur_el = None
        self.cur_range = None
        self.t_start = 0.0
        self._blk = 0
        self._live_t = 0.0
        self.preview_secs = PREVIEW_SECONDS
        self.preview_open = False
        self._live_snr_t = 0.0
        self.view_bw = 60e3
        self.last_error = ""
        self.fail_until = 0.0
        self._found = ([], 0.0)         # (devices, when)
        self._gain_range = (0.0, 49.6)

    # ------------------------------------------------------------------ settings
    def cfg(self):
        s = self.s
        mode = str(s.get("satrx_gain_mode", "fixed"))
        return {"enabled": bool(s.get("satrx_enabled", False)), "device": str(s.get("satrx_device", "") or ""),
                "gain": float(s.get("satrx_gain", 38.0)), "gain_mode": mode if mode in ("fixed", "auto") else "fixed",
                "both": bool(s.get("satrx_both", False)),
                "bias": bool(s.get("satrx_bias", False)), "ppm": int(s.get("satrx_ppm", 0) or 0)}

    def configure(self, who="", **kw):
        """Change the second receiver's settings.

        Every setting this actually changes is written to the log with its old and new value and who asked, so that a
        setting which turns out later to have moved on its own (the "use both receivers" tickbox has done this twice)
        can be traced back to the request that moved it.
        """
        s = self.s
        before = {name: s.get(name) for name in SETTINGS.values()}
        if "enabled" in kw:
            s["satrx_enabled"] = bool(kw["enabled"])
        if "device" in kw:
            s["satrx_device"] = str(kw["device"] or "")
        if "gain" in kw:
            lo, hi = self._gain_range
            s["satrx_gain"] = max(lo, min(hi, float(kw["gain"])))
        if "gain_mode" in kw:
            s["satrx_gain_mode"] = "auto" if kw["gain_mode"] == "auto" else "fixed"
        if "both" in kw:
            s["satrx_both"] = bool(kw["both"])
        if "bias" in kw:
            s["satrx_bias"] = bool(kw["bias"])
        if "ppm" in kw:
            s["satrx_ppm"] = max(-200, min(200, int(float(kw["ppm"]))))
        s.save()
        for name in SETTINGS.values():
            old, now = before[name], s.get(name)
            if old != now:
                log.info("second receiver: %s %r -> %r  (asked by %s)", name, old, now, who or "?")
        self.fail_until = 0.0
        self._found = ([], 0.0)

    # ------------------------------------------------------------------ finding the dongle
    def _main_key(self):
        d = getattr(self.radio, "dev", None)
        return getattr(d, "key", None)

    def _enumerate(self, force=False):
        found, when = self._found
        if self.job is not None and found:
            # We hold a dongle open (a live view or a pass).  Windows will not read an open dongle's serial number a second
            # time, so a fresh scan would lose our own dongle (it shows up as rtlsdr:0) and a pass that came due during a
            # live view would be silently handed to the main radio.  Keep the list from before we opened it.
            return found
        if force or time.time() - when > ENUM_EVERY:
            try:
                found = list(devices.list_rtl_native())
            except Exception as e:                   # noqa: BLE001
                log.warning("second receiver: looking for dongles: %s", e)
                found = []
            self._found = (found, time.time())
        return found

    def candidates(self, force=False):
        """RTL-SDR dongles the main radio is not using."""
        mine = self._main_key()
        return [d for d in self._enumerate(force) if d.key != mine]

    def choose(self):
        want = self.cfg()["device"]
        cands = self.candidates()
        if want:
            for d in cands:
                if d.key == want:
                    return d
            return None                              # the chosen one is not there (never silently use another)
        return cands[0] if cands else None

    def usable(self):
        """Switched on, a dongle is there, the last start did not fail just now."""
        if not self.cfg()["enabled"] or time.time() < self.fail_until:
            return False
        return self.choose() is not None

    def busy(self):
        return self.job is not None

    def gain_range(self):
        return self._gain_range

    def pass_gain(self, sid):
        """(gain, mode) for the next pass: the fixed gain, or what 'Learn the best' picks."""
        c = self.cfg()
        if c["gain_mode"] == "auto":
            lo, hi = self._gain_range
            return passlog.suggest_gain(self.passlog.for_sat(sid), c["gain"], lo, hi), "auto"
        return c["gain"], "fixed"

    # ------------------------------------------------------------------ plumbing
    def _emit(self, kind, payload):
        if kind == "image":
            if self.passlog.open_key:
                self.pass_images += 1
            try:
                self.recent_images = ([payload["file"]] + [f for f in self.recent_images if f != payload["file"]])[:8]
            except Exception:                        # noqa: BLE001
                pass
        self.radio._emit(kind, payload)

    def _event(self, level, text):
        self.radio._emit("event", {"level": level, "text": text})

    def _hw_center_for(self, f, fs):
        return int(f + fs * 0.25)                    # keep the signal away from the DC spike at the centre

    # ------------------------------------------------------------------ a pass
    def begin_job(self, job):
        """Open the dongle and start recording.  False = it could not be done (the main radio should take the pass)."""
        if self.job is not None and self.job.get("preview"):
            self.end_job("live view ended for a pass")       # a test view never stands in the way of a real pass
        with self.lock:
            if self.job is not None:
                return False
            dev = self.choose()
            if dev is None:
                self.last_error = "second receiver not found"
                self.fail_until = time.time() + RETRY_AFTER_FAIL
                return False
            c = self.cfg()
            try:
                dev.sample_rate = SAMPLE_RATE
                dev.center = self._hw_center_for(job["freq"], SAMPLE_RATE)
                dev.gain = float(job.get("gain") if job.get("gain") is not None else c["gain"])
                dev.agc = False
                dev.bias = c["bias"]
                dev.open()
                if dev.sample_rate != SAMPLE_RATE:
                    dev.set_sample_rate(SAMPLE_RATE)
                dev.set_ppm(c["ppm"])
                lo, hi = dev.gain_range()
                if hi > lo:
                    self._gain_range = (lo, hi)
                dev.set_gain(dev.gain)
                dev.async_read = True                # an unbroken stream: a picture needs every sample, not most of them
            except Exception as e:                   # noqa: BLE001
                log.exception("second receiver open")
                try:
                    dev.close()
                except Exception:                    # noqa: BLE001
                    pass
                self.last_error = f"could not open {dev.label}: {e}"
                self.fail_until = time.time() + RETRY_AFTER_FAIL
                self._event("warn", f"Second receiver {self.last_error} - the main radio will take the pass")
                return False
            self.last_error = ""
            self.dev = dev
            self.fs = SAMPLE_RATE
            self.rx = Receiver(self.fs)
            self.spec = Spectrum(2048)
            self.freq = int(job["freq"])
            self.job = job
            self.discard = 3
            self.snr = None
            self.snr_hist = []
            self.cur_el = self.cur_range = None
            self.t_start = time.time()
            self._blk = 0
            self.pass_images = 0
            self.job_gain = float(dev.gain)
            self.pass_meter = passlog.PassMeter(self.pass_el)
            self.freq_check = freqcheck.Tracker(self._nominal_freq(job), "rx2")
            self._fc_t = 0.0
            self.passlog.open_key = job.get("id")
            self.decoders.pass_key = job.get("id")
            self.decoders.pass_northbound = bool(job.get("northbound"))
            self.decoders.start(job.get("action") or "lrpt", "auto")
            self.gen += 1
            self.thread = threading.Thread(target=self._run, args=(self.gen,), name="satrx", daemon=True)
            self.thread.start()
            log.info("second receiver: %s, gain %.1f (%s), %.4f MHz", dev.label, self.job_gain, job.get("gain_mode"), self.freq / 1e6)
            self._event("info", f"Second receiver recording: {dev.label}, gain {self.job_gain:.1f} dB")
            return True

    def set_gain(self, g):
        """Change the RF gain by hand - also while the dongle is working (a pass or a live view); it is remembered."""
        lo, hi = self._gain_range
        g = max(lo, min(hi, float(g)))
        self.s["satrx_gain"] = g
        try:
            self.s.save()
        except Exception:                            # noqa: BLE001
            log.exception("settings not saved")
        with self.lock:
            dev = self.dev
            if self.job is not None and dev is not None:
                try:
                    dev.set_gain(g)
                    self.job_gain = float(dev.gain)
                except Exception as e:               # noqa: BLE001
                    log.warning("second receiver gain: %s", e)
        self._push_live(force=True)
        return g

    def begin_preview(self, freq=None, bw=None, secs=None):
        """A short live view (no recording) so the antenna and dongle can be seen working any time.
        `freq` (Hz) and `bw` (Hz, the width the signal meter measures) let it be pointed at any steady signal.
        `secs` is how long to watch: None = 90 s, 0 = until stopped (ended after 4 h at the latest)."""
        try:
            freq = int(float(freq)) if freq else PREVIEW_FREQ
            bw = float(bw) if bw else 60e3
            secs = PREVIEW_SECONDS if secs is None else int(float(secs))
        except (TypeError, ValueError):
            return False, "That frequency was not understood"
        if not 30e6 <= freq <= 1.7e9:
            return False, "Choose a frequency between 30 and 1700 MHz"
        bw = max(4e3, min(100e3, bw))
        secs = PREVIEW_MAX_SECONDS if secs <= 0 else max(10, min(PREVIEW_MAX_SECONDS, secs))
        if self.job is not None:
            return False, ("A pass is being recorded right now - the live view below is already showing it"
                           if not self.job.get("preview") else "The live view is already running")
        with self.lock:
            dev = self.choose()
            if dev is None:
                return False, "The second receiver is not ready - press Check it"
            c = self.cfg()
            try:
                dev.sample_rate = SAMPLE_RATE
                dev.center = self._hw_center_for(freq, SAMPLE_RATE)
                dev.gain = float(c["gain"])
                dev.agc = False
                dev.bias = c["bias"]
                dev.open()
                if dev.sample_rate != SAMPLE_RATE:
                    dev.set_sample_rate(SAMPLE_RATE)
                dev.set_ppm(c["ppm"])
                dev.set_gain(dev.gain)
                dev.async_read = True
            except Exception as e:                   # noqa: BLE001
                try:
                    dev.close()
                except Exception:                    # noqa: BLE001
                    pass
                self.last_error = f"could not open {dev.label}: {e}"
                return False, self.last_error
            self.last_error = ""
            self.dev = dev
            self.fs = SAMPLE_RATE
            self.rx = None
            self.spec = Spectrum(2048)
            self.freq = freq
            self.view_bw = bw
            self.preview_secs = secs
            self.preview_open = secs >= PREVIEW_MAX_SECONDS
            self.job = {"id": "preview", "name": "Live view", "preview": True, "freq": freq}
            self.pass_meter = None
            self.discard = 3
            self.snr = None
            self.snr_hist = []
            self.cur_el = self.cur_range = None
            self.t_start = time.time()
            self._blk = 0
            self.job_gain = float(dev.gain)
            self.gen += 1
            self.thread = threading.Thread(target=self._run, args=(self.gen,), name="satrx-view", daemon=True)
            self.thread.start()
        self._push_live(force=True)
        return True, (f"Live view on {freq / 1e6:.3f} MHz until you stop it" if self.preview_open
                      else f"Live view on {freq / 1e6:.3f} MHz for {self.preview_secs} s")

    # ------------------------------------------------------------------ finding the best gain for this dongle
    FREQ_CHECK_EVERY_S = 6.0   # how often to check where the carrier really is during a pass
    GCAL_STUCK = 8.0           # no readings for this long means the dongle has stopped: give up on the sweep
    GCAL_SETTLE = 0.45
    GCAL_READ = 0.35

    def gaincal_start(self, freq=None):
        """Sweep this dongle's gain at the Meteor frequency and find where the aerial, not the dongle, is the limit."""
        if self.busy():
            return False, "The second receiver is busy - try again when the pass or live view has finished."
        ok, msg = self.begin_preview(freq or PREVIEW_FREQ, 100e3, 90)
        if not ok:
            return False, msg
        lo, hi = self._gain_range
        dev = self.dev
        gains = gaincal.plan(lo, hi, getattr(dev, "gain_steps", None))
        self.gcal = {"gains": gains, "i": -1, "t": 0.0, "pts": [], "buf": [],
                     "was": float(self.s.get("satrx_gain", 38.0)), "freq": int(self.freq),
                     "dev": getattr(dev, "key", "") or ""}
        log.info("second receiver gain test: %d steps from %.1f to %.1f dB at %.4f MHz",
                 len(gains), gains[0], gains[-1], self.freq / 1e6)
        self._gcal_next(time.time())
        return True, ""

    def gaincal_stop(self, why="stopped"):
        g, self.gcal = self.gcal, None
        if g:
            try:
                self.set_gain(g["was"])
            except Exception:                        # noqa: BLE001
                pass
            self.radio._emit("satgaincal", {"running": False, "why": why})
            self.stop_preview()

    def _gcal_next(self, now):
        g = self.gcal
        if g is None:
            return
        g["i"] += 1
        if g["i"] >= len(g["gains"]):
            self._gcal_finish()
            return
        g["t"] = now
        g["buf"] = []
        try:
            self.set_gain(float(g["gains"][g["i"]]))
        except Exception:                            # noqa: BLE001
            log.exception("second receiver gain test")
            self.gaincal_stop("the dongle would not change gain")
            return
        self.radio._emit("satgaincal", {"running": True, "step": g["i"] + 1, "steps": len(g["gains"]),
                                        "gain": g["gains"][g["i"]]})

    def _gcal_watch(self, now):
        """Abandon a sweep that has stopped getting readings - see the same method in `engine`."""
        g = self.gcal
        if g is None or g["t"] <= 0.0 or now - g["t"] < self.GCAL_STUCK:
            return
        log.warning("second receiver gain test: no spectrum for %.1f s at step %d of %d - abandoning",
                    now - g["t"], g["i"] + 1, len(g["gains"]))
        self.gaincal_stop("the dongle stopped sending samples part-way through, so the test was abandoned and the gain "
                          "put back. Worth checking no other program has it open, then trying again.")

    def _gcal_tick(self, spec_db, now):
        g = self.gcal
        if g is None:
            return
        if now - g["t"] < self.GCAL_SETTLE:
            return
        g["buf"].append(float(np.median(spec_db)))
        if now - g["t"] < self.GCAL_SETTLE + self.GCAL_READ:
            return
        g["pts"].append((g["gains"][g["i"]], float(np.median(g["buf"])) if g["buf"] else None))
        self._gcal_next(now)

    def _gcal_finish(self):
        g, self.gcal = self.gcal, None
        res = gaincal.choose(g["pts"], keep=g["was"])
        if not res:
            self.set_gain(g["was"])
            self.radio._emit("satgaincal", {"running": False, "why": "not enough readings to decide"})
            self.stop_preview()
            return
        res.update(freq=g["freq"], dev=g["dev"], when=int(time.time()), was=g["was"], band="Meteor 137 MHz")
        self.set_gain(float(res["gain"]))
        log.info("second receiver gain test: %.1f dB chosen at %.4f MHz - %s", res["gain"], g["freq"] / 1e6, res["why"])
        log.info("gain test readings (gain dB -> noise floor dBFS): %s",
                 ", ".join(f"{a:.1f}->{b:.1f}" for a, b in res["points"]))
        self.radio._emit("satgaincal", dict(res, running=False, done=True))
        self.radio._emit("event", {"level": "info",
                                   "text": f"Second receiver RF gain set to {res['gain']:.0f} dB. {res['why']}"})
        self.stop_preview()

    def stop_preview(self):
        if self.job is not None and self.job.get("preview"):
            self.end_job("live view stopped")
            self._push_live(force=True)

    def retune_job(self, f):
        """Follow the Doppler shift without restarting anything."""
        with self.lock:
            dev = self.dev
            if not self.job or not dev:
                return
            f2 = int(f)
            if f2 == self.freq:
                return
            self.freq = f2
            if abs(f2 - dev.center) > self.fs * 0.42:
                dev.set_center(self._hw_center_for(f2, self.fs))
                self.discard = 2

    def end_job(self, why=None):
        if self.gcal is not None:                    # the sweep lives inside a preview job: it cannot outlive it
            self.gaincal_stop("the gain test ended early because the second receiver stopped - the gain was put back")
        with self.lock:
            job, self.job = self.job, None
            self.gen += 1
            th, self.thread = self.thread, None
        if th and th is not threading.current_thread():
            th.join(timeout=4)
        with self.lock:
            meter, self.pass_meter = self.pass_meter, None
            fc, self.freq_check = self.freq_check, None
            self.passlog.open_key = None
            had_gain, self.job_gain = self.job_gain, None
            info = None
            if self.dev and not (job and job.get("preview")):
                try:
                    info = self.dev.stream_info() if hasattr(self.dev, "stream_info") else None
                except Exception:                    # noqa: BLE001
                    info = None
                if info and info["wall_s"] > 5:
                    pct = 100.0 * info["got_s"] / info["wall_s"]
                    self.decoders.log(f"Second receiver stream: {info['got_s']:.0f} s of samples in {info['wall_s']:.0f} s ({pct:.1f}%), {info['dropped_s']:.1f} s dropped")
                    log.info("second receiver stream: %s", info)
            if not (job and job.get("preview")):
                try:
                    self.decoders.stop()             # closes the recording and hands it to SatDump
                except Exception:                    # noqa: BLE001
                    log.exception("second receiver decoder stop")
            self.decoders.pass_key = None
            self.decoders.pass_northbound = False
            if self.dev:
                try:
                    self.dev.async_read = False      # the next user of this dongle object starts from the plain reader
                    self.dev.close()
                except Exception:                    # noqa: BLE001
                    pass
                self.dev = None
            self.rx = None
            self.snr = None
            if not job or meter is None:
                self._push_live(force=True)
                return
            sm = meter.summary()
            rec = {"key": job["id"], "sat": job["sat"], "name": str(job["name"]).split(" pass")[0], "action": job.get("action"),
                   "start": int(meter.t0), "end": int(time.time()), "max_el": job.get("max_el"), "gain_mode": job.get("gain_mode"),
                   "stopped": why, "images": self.pass_images, "rx": "rx2", **sm}
            fres = fc.result() if fc is not None else None
            if fres:
                rec["freq_check"] = {k: fres[k] for k in ("median", "ppm", "spread", "n", "verdict")}
                log.info("%s", freqcheck.sentence(fres))
                if fres["verdict"] in ("oscillator", "orbit"):
                    self._event("warn", freqcheck.sentence(fres))
            if rec.get("gain") is None and had_gain is not None:
                rec["gain"] = round(had_gain, 1)
            if info and info["wall_s"] > 5:
                rec["iq_pct"] = round(100.0 * info["got_s"] / info["wall_s"], 1)
            self.pass_images = 0
            self.passlog.add(rec)
            self._push_live(force=True)
            log.info("second receiver pass report: %s peak %s deg, gain %s, SNR peak %s / typical %s dB (score %s, %d samples), %s",
                     rec["name"], rec.get("max_el"), rec.get("gain"), rec.get("snr_peak"), rec.get("snr_med"), rec.get("score"),
                     rec.get("samples", 0), self.passlog.result(rec))

    def _nominal_freq(self, job):
        """The satellite's own frequency, before Doppler - what the error is measured against."""
        try:
            return float(self.radio.scheduler.sats.cfg()[job["sat"]]["freq"])
        except Exception:                                # noqa: BLE001
            return float(job.get("freq") or 0.0)

    def _pass_images_done(self, key, shown, total, note="", out_dir=None, fill=None):
        self.passlog.set_images(key, shown, total, note, fill)
        try:                                                # what level was actually producing frames, from the .cadu
            framesnr.for_pass(self.passlog, key, out_dir)
        except Exception:                                   # noqa: BLE001
            log.exception("second receiver frame measurement")
        merger = getattr(self.radio, "merger", None)        # if the main radio took this pass too, merge them
        if merger is not None:
            try:
                merger.report(key, out_dir, "rx2")
            except Exception:                               # noqa: BLE001
                log.exception("second receiver merge report")
        rec = self.passlog.find(key)
        if rec:
            log.info("second receiver pass report: %s - %s", rec.get("name"), self.passlog.result(rec))
            try:
                self.radio._emit("pass_done", {"name": rec.get("name"), "rx": "rx2", "images": rec.get("images", 0), "fill": rec.get("fill"),
                                               "max_el": rec.get("max_el"), "poor": bool(rec.get("poor"))})
            except Exception:                        # noqa: BLE001
                log.exception("pass alert")
        try:
            self.radio.scheduler._emit()
        except Exception:                            # noqa: BLE001
            pass

    # ------------------------------------------------------------------ the sample loop
    def _run(self, gen):
        pending = np.zeros(0, np.complex64)
        last_data = time.time()
        reopened = 0
        dev = self.dev
        while self.gen == gen and self.job is not None and dev is not None:
            fs = self.fs
            block = int(fs * BLOCK_SECONDS)
            try:
                x = dev.read(block - len(pending))
            except Exception as e:                   # noqa: BLE001
                log.exception("second receiver read")
                x = np.zeros(0, np.complex64)
                time.sleep(0.2)
            self._gcal_watch(time.time())         # runs even when no samples arrive, which is the case it is for
            if len(x):
                last_data = time.time()
            elif time.time() - last_data > 4.0:      # the dongle stopped delivering: try once more, then give up
                if reopened >= 2:
                    self.last_error = "the second receiver stopped delivering samples"
                    self._event("error", "Second receiver stopped delivering samples - pass recording ended")
                    break
                reopened += 1
                try:
                    dev.close()
                    time.sleep(0.5)
                    dev.open()
                    dev.set_sample_rate(self.fs)
                    dev.set_center(self._hw_center_for(self.freq, self.fs))
                    dev.set_gain(dev.gain)
                    self.discard = 2
                except Exception as e:               # noqa: BLE001
                    log.warning("second receiver reopen: %s", e)
                last_data = time.time()
                continue
            pending = np.concatenate((pending, x)) if len(pending) else x
            if len(pending) < block:
                continue
            iq, pending = pending[:block], pending[block:]
            if self.discard > 0:
                self.discard -= 1
                continue
            now = time.time()
            job = self.job
            if job is None or self.gen != gen:       # the pass or live view was ended while this block was being read
                break
            preview = bool(job.get("preview"))
            try:
                spec_db = self.spec.process(iq, alpha=0.6)
                if spec_db is not None:
                    self._blk += 1
                    if self._blk % SPEC_EVERY == 0:
                        q = np.clip((spec_db - SPEC_MIN_DB) / (SPEC_MAX_DB - SPEC_MIN_DB) * 255, 0, 255).astype(np.uint8)
                        self.radio._emit("satspec", {"center": dev.center, "fs": fs, "freq": self.freq, "data": q.tobytes()})
                if spec_db is not None and self.gcal is not None:
                    self._gcal_tick(spec_db, now)
                m = self.pass_meter
                if spec_db is not None and (m is not None or preview) and now - self._live_snr_t >= (0.25 if preview else 1.0):
                    self._live_snr_t = now
                    half = self.view_bw if preview else passlog.BAND_HALF_HZ.get("lrpt", 60e3)
                    snr = passlog.band_snr(spec_db, dev.center, fs, self.freq, half)
                    fc = self.freq_check
                    if fc is not None and not preview and now - getattr(self, "_fc_t", 0.0) >= self.FREQ_CHECK_EVERY_S:
                        self._fc_t = now
                        try:
                            off = freqcheck.carrier_offset(spec_db, dev.center, fs, self.freq, half)
                            fc.add(now - self.t_start, off, el=self.pass_el,
                                   doppler=(self.freq - fc.freq) if fc.freq else None)
                        except Exception:                # noqa: BLE001
                            log.exception("second receiver frequency check")
                    self.snr = None if snr is None else round(snr, 1)
                    if snr is not None:
                        self.snr_hist = (self.snr_hist + [self.snr])[-240:]
                    if m is not None:
                        m.add(now, snr, self.job_gain)
                    if self.pass_el and not preview:
                        try:
                            la = self.pass_el(now)
                        except Exception:            # noqa: BLE001
                            la = None
                        if la:
                            self.cur_el, self.cur_range = la[0], la[1]
                rx = self.rx
                if not preview and rx is not None:
                    x240 = rx.stage1.process(rx.mixer.process(iq, self.freq - dev.center, fs))
                    self.decoders.feed({"iq240": x240})
            except Exception:                        # noqa: BLE001
                log.exception("second receiver block")
            self._push_live()
            if preview and now - self.t_start > self.preview_secs:
                break
        if self.job is not None and self.gen == gen:    # ended by itself (not asked to): the scheduler notices job is None
            threading.Thread(target=self.end_job, args=("receiver stopped",), name="satrx-end", daemon=True).start()

    # ------------------------------------------------------------------ for the screen
    def live(self):
        """What the 2nd-receiver screen shows while the dongle is working (sent about once a second)."""
        job = self.job
        if not job:
            return {"on": False, "snr": None, "hist": [], "images": self.pass_images, "recent": self.recent_images[:6]}
        return {"on": True, "preview": bool(job.get("preview")), "name": job.get("name"), "freq": self.freq,
                "gain": self.job_gain, "snr": self.snr, "el": None if self.cur_el is None else round(self.cur_el, 1),
                "range": None if self.cur_range is None else int(self.cur_range), "images": self.pass_images,
                "elapsed": int(time.time() - self.t_start) if self.t_start else 0,
                "left": (max(0, int(self.preview_secs - (time.time() - self.t_start))) if not self.preview_open else None) if job.get("preview") else None,
                "open": bool(job.get("preview") and self.preview_open),
                "bw": self.view_bw if job.get("preview") else None, "hist": self.snr_hist[-120:], "recent": self.recent_images[:6], "max_el": job.get("max_el")}

    def _push_live(self, force=False):
        now = time.time()
        if not force and now - self._live_t < (0.25 if (self.job and self.job.get("preview")) else 1.0):
            return
        self._live_t = now
        try:
            self.radio._emit("satrx_live", self.live())
        except Exception:                            # noqa: BLE001
            pass
    def status(self):
        c = self.cfg()
        cands = self.candidates()
        chosen = self.choose() if c["enabled"] else None
        lo, hi = self._gain_range
        return {"enabled": c["enabled"], "device": c["device"], "gain": c["gain"], "gain_mode": c["gain_mode"],
                "both": c["both"], "bias": c["bias"], "ppm": c["ppm"], "gain_min": lo, "gain_max": hi,
                "devices": [{"key": d.key, "label": d.label} for d in cands],
                "using": chosen.label if chosen else "", "ready": bool(chosen) and time.time() >= self.fail_until,
                "busy": self.busy(), "snr": self.snr, "error": self.last_error,
                "main_is_rtl": getattr(getattr(self.radio, "dev", None), "kind", "") == "rtlsdr",
                "driver": bool(devices.rtl_lib()), "live": self.live(),
                "last": (self.passlog.view(1) or [None])[0], "fail_in": max(0, int(self.fail_until - time.time()))}

    def check(self):
        """Open the dongle for a moment and say what it is.  (Not while it is recording a pass.)"""
        if self.job is not None and self.job.get("preview"):
            self.end_job("live view stopped")
        if self.busy():
            return "warn", "The second receiver is recording a pass - try again afterwards"
        dev = self.choose()
        if dev is None:
            cands = self._enumerate(force=True)
            if not devices.rtl_lib():
                return "error", "The RTL-SDR driver (rtlsdr.dll) was not found"
            if not cands:
                return "warn", "No RTL-SDR dongle found. Plug in the Nooelec and check it uses the WinUSB driver (Zadig)"
            return "warn", "The only RTL-SDR dongle found is the one the main radio is using - the second receiver needs its own"
        try:
            dev.sample_rate = SAMPLE_RATE
            dev.center = 137900000
            dev.open()
            try:
                got = dev.read(int(SAMPLE_RATE * 0.2))
                lo, hi = dev.gain_range()
                if hi > lo:
                    self._gain_range = (lo, hi)
                ok = len(got) > 0
            finally:
                dev.close()
        except Exception as e:                       # noqa: BLE001
            return "error", f"Could not open {dev.label}: {e}"
        if not ok:
            return "error", f"{dev.label} opened but gave no samples"
        self.fail_until = 0.0
        return "info", f"Second receiver OK: {dev.label} (gain {lo:.0f}-{hi:.0f} dB)"

    def shutdown(self):
        if self.job is not None:
            self.end_job("program closed")
