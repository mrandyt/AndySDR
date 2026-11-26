"""Merge the frames two receivers heard of the same satellite pass.

Meteor sends right-hand circular polarisation. A linear antenna - a V-dipole, a discone, a whip -
fades hard whenever the polarisation turns away from it, which is why a picture from one receiver
has black bands through it even when the satellite is high and the signal is strong.

Two linear antennas at different angles do not fade at the same moment. So when one receiver loses
the signal the other often still has it, and the frames each of them heard can be put together into
one stream that is fuller than either on its own.

Every frame carries its own number, so this needs no guesswork about timing: frames from the two
receivers are keyed by (channel, number), the two sets are unioned, and the result is written out in
order. SatDump then builds the pictures from the merged stream exactly as it would from one receiver.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger(__name__)

ASM = bytes.fromhex("1acffc1d")     # the marker every CADU frame starts with
CADU = 1024                         # bytes per frame, Meteor LRPT
COUNTER_BITS = 24
COUNTER_WRAP = 1 << COUNTER_BITS


def read_frames(path):
    """{(vcid, counter): frame} for a .cadu file. Frames that are the wrong shape are skipped."""
    out = {}
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as e:
        log.warning("merge: cannot read %s: %s", path, e)
        return out
    for i in range(0, len(blob) - CADU + 1, CADU):
        f = blob[i:i + CADU]
        if f[:4] != ASM:
            continue                                    # not a frame start: the file is not what we expect
        vcid = f[5] & 0x3F
        counter = int.from_bytes(f[6:9], "big")
        out[(vcid, counter)] = f
    return out


def _ordered(keys):
    """Frame numbers in the order they were sent, allowing for the counter rolling over mid-pass."""
    keys = sorted(keys)
    if not keys:
        return keys
    nums = [k[1] for k in keys]
    span = max(nums) - min(nums)
    if span < COUNTER_WRAP * 0.75:                      # no roll-over in this pass: plain order is right
        return keys
    low = [k for k in keys if k[1] < COUNTER_WRAP // 2]  # it rolled over: the small numbers came last
    high = [k for k in keys if k[1] >= COUNTER_WRAP // 2]
    return high + low


def merge(paths, out_path):
    """Write one .cadu holding every frame either receiver heard. Returns a report, or None if there was nothing."""
    sets = []
    for p in paths:
        fr = read_frames(p)
        if fr:
            sets.append((p, fr))
        else:
            log.info("merge: no frames in %s", p)
    if not sets:
        return None
    if len(sets) == 1:
        log.info("merge: only one receiver had frames, nothing to merge")
        return None

    all_frames = {}
    for _, fr in sets:
        for k, v in fr.items():
            all_frames.setdefault(k, v)                 # first receiver to hear a frame wins; they are identical

    keys = _ordered(all_frames.keys())
    tmp = out_path + ".part"
    try:
        with open(tmp, "wb") as fh:
            for k in keys:
                fh.write(all_frames[k])
        os.replace(tmp, out_path)
    except OSError as e:
        log.warning("merge: cannot write %s: %s", out_path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    counts = {os.path.basename(p): len(fr) for p, fr in sets}
    total = len(all_frames)
    best = max(len(fr) for _, fr in sets)
    rep = {
        "frames": total,
        "best_single": best,
        "gained": total - best,
        "gained_pct": round(100.0 * (total - best) / best, 1) if best else 0.0,
        "per_receiver": counts,
        "path": out_path,
    }
    log.info("merge: %s frames from %s receivers (best alone %s, +%s = +%.1f%%)",
             total, len(sets), best, total - best, rep["gained_pct"])
    return rep


def describe(rep):
    """One line for the pass log, in the words the person reading it would use."""
    if not rep:
        return ""
    return (f"Both receivers merged: {rep['frames']} frames, "
            f"{rep['gained']} more than the better receiver alone (+{rep['gained_pct']}%)")


class Merger:
    """Waits for both receivers to finish a shared pass, then merges their frames and makes the pictures.

    Each receiver decodes its own recording as usual and gets its own pictures; this adds a third set
    made from both. Nothing here touches what either receiver produced.
    """

    WAIT_SECONDS = 15 * 60          # how long to wait for the slower receiver before giving up

    def __init__(self, images_dir, find_exe, settings, log_cb=None, done_cb=None):
        self.images_dir = images_dir
        self.find_exe = find_exe
        self.settings = settings
        self.log_cb = log_cb or (lambda *_: None)
        self.done_cb = done_cb                      # done_cb(key, report, images) when a merge finishes
        self._waiting = {}                          # key -> {"started": t, "dirs": {rx: dir}}
        self._lock = threading.Lock()

    def expect(self, key):
        """A pass is about to run on both receivers."""
        if not key:
            return
        with self._lock:
            self._waiting[key] = {"started": time.time(), "dirs": {}}
        log.info("merge: expecting both receivers for pass %s", key)

    def report(self, key, out_dir, rx):
        """One receiver has finished decoding. Merges once the other one has too."""
        if not key or not out_dir:
            return
        with self._lock:
            w = self._waiting.get(key)
            if w is None:
                return                              # not a shared pass, or it has already been merged
            w["dirs"][rx] = out_dir
            if time.time() - w["started"] > self.WAIT_SECONDS:
                self._waiting.pop(key, None)
                log.info("merge: pass %s waited too long, giving up", key)
                return
            if len(w["dirs"]) < 2:
                log.info("merge: pass %s has %s of 2 receivers", key, len(w["dirs"]))
                return
            self._waiting.pop(key, None)
            dirs = dict(w["dirs"])
        threading.Thread(target=self._run, args=(key, dirs), daemon=True).start()

    def drop(self, key):
        with self._lock:
            self._waiting.pop(key, None)

    def _cadu(self, out_dir):
        for name in os.listdir(out_dir) if os.path.isdir(out_dir) else []:
            if name.endswith(".cadu"):
                return os.path.join(out_dir, name)
        return None

    def _run(self, key, dirs):
        try:
            if len(set(os.path.normcase(os.path.abspath(d)) for d in dirs.values())) < 2:
                log.warning("merge: both receivers reported the same folder %s", list(dirs.values())[0])
                self.log_cb("Both receivers recorded the pass into the same file, so there was nothing to merge.")
                return
            paths = [p for p in (self._cadu(d) for d in dirs.values()) if p]
            if len(paths) < 2:
                self.log_cb("Both receivers ran, but only one of them found any frames - nothing to merge.")
                return
            name = os.path.basename(sorted(dirs.values())[0]) + "_merged"
            out_dir = os.path.join(self.images_dir, name)
            os.makedirs(out_dir, exist_ok=True)
            merged = os.path.join(out_dir, "meteor_m2-x_lrpt.cadu")
            rep = merge(paths, merged)
            if not rep:
                self.log_cb("Both receivers ran, but their frames could not be merged.")
                return
            self.log_cb(describe(rep))
            images = self._images(merged, out_dir)
            rep["images"] = images
            if images:
                self.log_cb(f"Merged pass: {images} picture(s) from both receivers together, in the Images tab.")
            if self.done_cb:
                try:
                    self.done_cb(key, rep, images)
                except Exception:                   # noqa: BLE001
                    log.exception("merge: done callback")
        except Exception:                           # noqa: BLE001
            log.exception("merge: pass %s", key)

    def _images(self, merged, out_dir):
        """Hand the merged frames back to SatDump. Returns how many pictures came out."""
        exe = self.find_exe("satdump", (self.settings or {}).get("decoder_paths", {}))
        if not exe:
            self.log_cb("The merged frames are saved, but SatDump was not found to make the pictures.")
            return 0
        ok = False
        text = ""
        for level in ("cadu", "frames"):            # what SatDump calls a file of frames differs between versions
            args = [exe, "meteor_m2-x_lrpt", level, merged, out_dir]
            try:
                p = subprocess.run(args, capture_output=True, text=True, timeout=900,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:                       # noqa: BLE001
                log.exception("merge: satdump")
                self.log_cb("The merged frames are saved, but SatDump could not be run on them.")
                return 0
            text += f"$ {' '.join(args)}\n" + (p.stdout or "") + (p.stderr or "") + "\n"
            if p.returncode == 0:
                ok = True
                break
            log.warning("merge: satdump level %s exited %s", level, p.returncode)
        try:
            with open(os.path.join(out_dir, "satdump.log"), "w", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
        except OSError:
            pass
        if not ok:
            self.log_cb("The merged frames are saved, but SatDump could not turn them into pictures.")
            return 0
        n = 0
        for root, _dirs, files in os.walk(out_dir):
            n += sum(1 for f in files if f.lower().endswith(".png"))
        return n
