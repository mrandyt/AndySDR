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

from . import devices, passlog
from .config import data_dir
from .decoders import DecoderManager
from .dsp import Receiver, Spectrum

log = logging.getLogger("andysdr.satrx")

BLOCK_SECONDS = 0.05
SAMPLE_RATE = 1200000            # a whole multiple of 240 kS/s, and easy on a USB hub
ENUM_EVERY = 20.0                # seconds between looks for the dongle
RETRY_AFTER_FAIL = 180.0         # after a failed start, leave the pass to the main radio for a while
ACTIONS = ("lrpt",)              # what this receiver records (137 MHz Meteor pictures)


def takes(sid, sat):
    """Does this second receiver record that satellite?  Only the 137 MHz LRPT ones."""
    return sat.get("action") in ACTIONS and sat.get("kind") != "geo"


class SatRx:
    def __init__(self, radio):
        self.radio = radio
        self.s = radio.s
        self.lock = threading.RLock()
        self.passlog = passlog.PassLog(os.path.join(data_dir(), "passes_rx2.jsonl"))
        self.decoders = DecoderManager(self.s, self._emit)
        self.decoders.freq_fn = lambda: self.freq
        self.decoders.job_fn = lambda: self.job
        self.decoders.rate_fn = lambda: self.fs
        self.decoders.pass_done = self._pass_images_done
        self.dev = None
        self.rx = None
        self.spec = Spectrum(2048)
        self.thread = None
        self.gen = 0
        self.job = None
        self.freq = 0
        self.fs = SAMPLE_RATE
        self.discard = 0
        self.pass_el = None
        self.pass_meter = None
        self.job_gain = None
        self.pass_images = 0
        self.snr = None
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
                "bias": bool(s.get("satrx_bias", False)), "ppm": int(s.get("satrx_ppm", 0) or 0)}

    def configure(self, **kw):
        s = self.s
        if "enabled" in kw:
            s["satrx_enabled"] = bool(kw["enabled"])
        if "device" in kw:
            s["satrx_device"] = str(kw["device"] or "")
        if "gain" in kw:
            lo, hi = self._gain_range
            s["satrx_gain"] = max(lo, min(hi, float(kw["gain"])))
        if "gain_mode" in kw:
            s["satrx_gain_mode"] = "auto" if kw["gain_mode"] == "auto" else "fixed"
        if "bias" in kw:
            s["satrx_bias"] = bool(kw["bias"])
        if "ppm" in kw:
            s["satrx_ppm"] = max(-200, min(200, int(float(kw["ppm"]))))
        s.save()
        self.fail_until = 0.0
        self._found = ([], 0.0)

    # ------------------------------------------------------------------ finding the dongle
    def _main_key(self):
        d = getattr(self.radio, "dev", None)
        return getattr(d, "key", None)

    def _enumerate(self, force=False):
        found, when = self._found
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
        if kind == "image" and self.passlog.open_key:
            self.pass_images += 1
        self.radio._emit(kind, payload)

    def _event(self, level, text):
        self.radio._emit("event", {"level": level, "text": text})

    def _hw_center_for(self, f, fs):
        return int(f + fs * 0.25)                    # keep the signal away from the DC spike at the centre

    # ------------------------------------------------------------------ a pass
    def begin_job(self, job):
        """Open the dongle and start recording.  False = it could not be done (the main radio should take the pass)."""
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
            self.pass_images = 0
            self.job_gain = float(dev.gain)
            self.pass_meter = passlog.PassMeter(self.pass_el)
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
        with self.lock:
            job, self.job = self.job, None
            self.gen += 1
            th, self.thread = self.thread, None
        if th and th is not threading.current_thread():
            th.join(timeout=4)
        with self.lock:
            meter, self.pass_meter = self.pass_meter, None
            self.passlog.open_key = None
            had_gain, self.job_gain = self.job_gain, None
            try:
                self.decoders.stop()                 # closes the recording and hands it to SatDump
            except Exception:                        # noqa: BLE001
                log.exception("second receiver decoder stop")
            self.decoders.pass_key = None
            self.decoders.pass_northbound = False
            if self.dev:
                try:
                    self.dev.close()
                except Exception:                    # noqa: BLE001
                    pass
                self.dev = None
            self.rx = None
            if not job or meter is None:
                return
            sm = meter.summary()
            rec = {"key": job["id"], "sat": job["sat"], "name": str(job["name"]).split(" pass")[0], "action": job.get("action"),
                   "start": int(meter.t0), "end": int(time.time()), "max_el": job.get("max_el"), "gain_mode": job.get("gain_mode"),
                   "stopped": why, "images": self.pass_images, "rx": "rx2", **sm}
            if rec.get("gain") is None and had_gain is not None:
                rec["gain"] = round(had_gain, 1)
            self.pass_images = 0
            self.passlog.add(rec)
            log.info("second receiver pass report: %s peak %s deg, gain %s, SNR peak %s / typical %s dB (score %s, %d samples), %s",
                     rec["name"], rec.get("max_el"), rec.get("gain"), rec.get("snr_peak"), rec.get("snr_med"), rec.get("score"),
                     rec.get("samples", 0), self.passlog.result(rec))

    def _pass_images_done(self, key, shown, total, note=""):
        self.passlog.set_images(key, shown, total, note)
        rec = self.passlog.find(key)
        if rec:
            log.info("second receiver pass report: %s - %s", rec.get("name"), self.passlog.result(rec))
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
            try:
                spec_db = self.spec.process(iq, alpha=0.6)
                m = self.pass_meter
                if m is not None and spec_db is not None and now - m._last >= 1.0:
                    snr = passlog.band_snr(spec_db, dev.center, fs, self.freq, passlog.BAND_HALF_HZ.get("lrpt", 60e3))
                    self.snr = None if snr is None else round(snr, 1)
                    m.add(now, snr, self.job_gain)
                rx = self.rx
                x240 = rx.stage1.process(rx.mixer.process(iq, self.freq - dev.center, fs))
                self.decoders.feed({"iq240": x240})
            except Exception:                        # noqa: BLE001
                log.exception("second receiver block")
        if self.job is not None and self.gen == gen:    # ended by itself (not asked to): the scheduler notices job is None
            threading.Thread(target=self.end_job, args=("receiver stopped",), name="satrx-end", daemon=True).start()

    # ------------------------------------------------------------------ for the screen
    def status(self):
        c = self.cfg()
        cands = self.candidates()
        chosen = self.choose() if c["enabled"] else None
        lo, hi = self._gain_range
        return {"enabled": c["enabled"], "device": c["device"], "gain": c["gain"], "gain_mode": c["gain_mode"],
                "bias": c["bias"], "ppm": c["ppm"], "gain_min": lo, "gain_max": hi,
                "devices": [{"key": d.key, "label": d.label} for d in cands],
                "using": chosen.label if chosen else "", "ready": bool(chosen) and time.time() >= self.fail_until,
                "busy": self.busy(), "snr": self.snr, "error": self.last_error,
                "main_is_rtl": getattr(getattr(self.radio, "dev", None), "kind", "") == "rtlsdr",
                "driver": bool(devices.rtl_lib())}

    def check(self):
        """Open the dongle for a moment and say what it is.  (Not while it is recording a pass.)"""
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
