"""Remember what each captured picture / recording was: satellite or station name, frequency, time.

Whenever a picture decoder (WEFAX, SSTV, LRPT, GOES) starts, or a recording is made, one line is appended
to `capture_log.jsonl` in the AndySDR data folder. The Weather Images and Recordings tabs then match every
file to the latest matching line (by the timestamp in its file name) to show "Meteor-M N2-3 · 137.9000 MHz ·
Wed 18 Nov 10:12" and to name downloads sensibly. Files from before this existed simply show no frequency.
"""
import datetime as dt
import json
import os
import re
import threading
import time

from .config import data_dir

_lock = threading.Lock()
_TS = re.compile(r"(\d{8})_(\d{6})")
_PASS = re.compile(r"\s*pass \(max.*\)\s*$")

LABELS = {"lrpt": "Meteor-M LRPT", "wefax": "Weather fax (WEFAX)", "sstv": "SSTV picture",
          "goes": "GOES HRIT", "record": "Audio recording"}
MAX_AGE = {"lrpt": 2 * 3600, "goes": 2 * 3600, "wefax": 48 * 3600, "sstv": 48 * 3600, "record": 3600}


def _path():
    return os.path.join(data_dir(), "capture_log.jsonl")


def note(kind, freq, name="", mode=""):
    """Log that a capture of `kind` has started on `freq` Hz (name = job / satellite name, if known)."""
    entry = {"t": time.time(), "kind": kind, "freq": int(freq or 0),
             "name": _PASS.sub("", name or ""), "mode": mode or ""}
    try:
        with _lock, open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def entries(limit=3000):
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def _kind_of(base):
    for k in ("lrpt", "wefax", "sstv", "goes"):
        if base.startswith(k + "_"):
            return k
    return "record" if base.startswith("audio_") else ""


def _preset_name(kind, freq):
    """Station / satellite name for a frequency, from the scheduler's quick-pick list."""
    if not freq:
        return ""
    try:
        from .scheduler import PRESETS
    except Exception:
        return ""
    for p in PRESETS:
        if p["action"] == kind and abs(p["freq"] - freq) <= 2000:
            return p["name"]
    return ""


def _freq_from_name(base):
    m = re.search(r"_(\d+\.\d+)MHz", base)
    return int(round(float(m.group(1)) * 1e6)) if m else 0


def describe(rel, mtime, size=0, ents=None):
    """Describe a file under the images / recordings folder -> dict for the GUI."""
    base = rel.replace("\\", "/").split("/")[0]
    kind = _kind_of(base)
    m = _TS.search(base)
    t = mtime
    if m:
        try:
            t = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            pass
    freq, name, mode = _freq_from_name(base), "", ""
    if kind and ents:
        best = None
        for e in ents:
            if e.get("kind") == kind and e["t"] <= t + 30 and t - e["t"] <= MAX_AGE[kind]:
                if best is None or e["t"] > best["t"]:
                    best = e
        if best:
            freq = freq or best.get("freq", 0)
            name, mode = best.get("name", ""), best.get("mode", "")
    if not name:
        name = _preset_name(kind, freq) or LABELS.get(kind, "Picture")
        if kind == "lrpt" and freq in (137100000, 137900000):
            name = "Meteor-M LRPT"
    when = time.strftime("%a %d %b %H:%M", time.localtime(t))
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("_") or "capture"
    parts = [stem]
    if freq:
        parts.append(f"{freq / 1e6:.4f}MHz")
    parts.append(time.strftime("%Y%m%d_%H%M", time.localtime(t)))
    leaf = os.path.basename(rel)
    # keep the original name's own suffix (e.g. the channel name inside an LRPT folder)
    if "/" in rel.replace("\\", "/"):
        parts.append(os.path.splitext(leaf)[0])
    dl = "_".join(parts) + os.path.splitext(leaf)[1].lower()
    return {"f": rel.replace("\\", "/"), "t": t, "kind": kind, "name": name, "freq": freq, "mode": mode,
            "when": when, "size": size, "dl": dl}
