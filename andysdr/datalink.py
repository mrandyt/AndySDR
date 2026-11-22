"""Message lists for the aircraft text (ACARS) and weather balloon (radiosonde) decoders.

Everything is kept in memory (newest 1500 ACARS messages, every sonde heard), written to a daily CSV in
%APPDATA%\\AndySDR\\acars\\ and \\sondes\\, and pushed to every screen as "acars" / "sonde" events.
"""
import collections
import csv
import io
import logging
import os
import threading
import time

from .config import data_dir
from .radiosonde import bearing_distance

log = logging.getLogger("andysdr.datalink")

MAX_ACARS = 1500
ACARS_FIELDS = ["date", "time", "freq_mhz", "reg", "flight", "label", "label_name", "mode", "ack", "block", "message"]
SONDE_FIELDS = ["date", "time", "freq_mhz", "serial", "type", "frame", "lat", "lon", "alt_m", "speed_ms", "climb_ms",
                "heading", "sats", "gps_time"]
TRACK_MAX = 400


class DatalinkStore:
    def __init__(self, settings, emit):
        self.s = settings
        self.emit = emit
        self.acars = collections.deque(maxlen=MAX_ACARS)
        self.sondes = {}                              # serial -> record (newest frame) with a short track
        self.next_id = 1
        self._lock = threading.Lock()
        self.dir_a = os.path.join(data_dir(), "acars")
        self.dir_s = os.path.join(data_dir(), "sondes")
        os.makedirs(self.dir_a, exist_ok=True)
        os.makedirs(self.dir_s, exist_ok=True)

    # ------------------------------------------------------------------ ACARS
    def add_acars(self, m, freq=0):
        now = time.time()
        with self._lock:
            for old in reversed(self.acars):                   # the same block is often heard twice
                if now - old["ts"] > 20:
                    break
                if old["reg"] == m["reg"] and old["label"] == m["label"] and old["text"] == m["text"] and old["bid"] == m["bid"]:
                    return None
            rec = dict(m, id=self.next_id, ts=round(now, 1), freq=int(freq or 0),
                       date=time.strftime("%Y-%m-%d", time.localtime(now)), t=time.strftime("%H:%M:%S", time.localtime(now)),
                       label_name=_label_name(m["label"]))
            rec.pop("t_", None)
            self.next_id += 1
            self.acars.append(rec)
        self._write(self.dir_a, "acars", ACARS_FIELDS, {
            "date": rec["date"], "time": rec["t"], "freq_mhz": f"{rec['freq'] / 1e6:.4f}" if rec["freq"] else "",
            "reg": rec["reg"], "flight": rec["flight"], "label": rec["label"], "label_name": rec["label_name"],
            "mode": rec["mode"], "ack": rec["ack"], "block": rec["bid"], "message": rec["text"].replace("\n", " | ")},
            rec["date"])
        self.emit("acars", rec)
        return rec

    # ------------------------------------------------------------------ radiosondes
    def add_sonde(self, r, freq=0):
        now = time.time()
        with self._lock:
            old = self.sondes.get(r["id"])
            track = list(old["track"]) if old else []
            if old and old["frame"] == r["frame"] and now - old["ts"] < 2:
                return None
            track.append([round(r["lat"], 5), round(r["lon"], 5), int(r["alt"])])
            if len(track) > TRACK_MAX:
                track = track[::2]
            rec = {"id": r["id"], "type": r["type"], "frame": r["frame"], "lat": round(r["lat"], 5),
                   "lon": round(r["lon"], 5), "alt": round(r["alt"]), "vh": round(r["vh"], 1), "vu": round(r["vu"], 1),
                   "heading": round(r["heading"]), "sats": r["sats"], "time": r.get("time", ""), "freq": int(freq or 0),
                   "ts": round(now, 1), "first": old["first"] if old else round(now, 1),
                   "n": (old["n"] if old else 0) + 1, "track": track}
            self.sondes[r["id"]] = rec
        lat0, lon0 = self.s.get("adsb_lat"), self.s.get("adsb_lon")
        if lat0 is not None and lon0 is not None:
            try:
                d, b = bearing_distance(float(lat0), float(lon0), rec["lat"], rec["lon"])
                rec["dist_km"], rec["bearing"] = round(d, 1), round(b)
            except (TypeError, ValueError):
                pass
        self._write(self.dir_s, "sondes", SONDE_FIELDS, {
            "date": time.strftime("%Y-%m-%d", time.localtime(now)), "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "freq_mhz": f"{rec['freq'] / 1e6:.3f}" if rec["freq"] else "", "serial": rec["id"], "type": rec["type"],
            "frame": rec["frame"], "lat": rec["lat"], "lon": rec["lon"], "alt_m": rec["alt"], "speed_ms": rec["vh"],
            "climb_ms": rec["vu"], "heading": rec["heading"], "sats": rec["sats"], "gps_time": rec["time"]},
            time.strftime("%Y-%m-%d", time.localtime(now)))
        self.emit("sonde", rec)
        return rec

    # ------------------------------------------------------------------ housekeeping
    def snapshot(self):
        with self._lock:
            return {"acars": list(self.acars)[-800:], "sondes": list(self.sondes.values()),
                    "acars_dir": self.dir_a, "sonde_dir": self.dir_s}

    def clear(self, which):
        with self._lock:
            if which in ("acars", "all"):
                self.acars.clear()
            if which in ("sondes", "all"):
                self.sondes.clear()
        self.emit("datalink_all", self.snapshot())

    def _write(self, folder, stem, fields, row, date):
        if not self.s.get(stem + "_log", True):
            return
        path = os.path.join(folder, f"{stem}_{date}.csv")
        try:
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                if new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            log.warning("%s log: %s", stem, e)

    def export_csv(self, which):
        buf = io.StringIO()
        if which == "acars":
            w = csv.DictWriter(buf, fieldnames=ACARS_FIELDS)
            w.writeheader()
            with self._lock:
                for m in self.acars:
                    w.writerow({"date": m["date"], "time": m["t"], "freq_mhz": f"{m['freq'] / 1e6:.4f}" if m["freq"] else "",
                                "reg": m["reg"], "flight": m["flight"], "label": m["label"], "label_name": m["label_name"],
                                "mode": m["mode"], "ack": m["ack"], "block": m["bid"], "message": m["text"].replace("\n", " | ")})
        else:
            w = csv.DictWriter(buf, fieldnames=["serial", "type", "frames", "last_lat", "last_lon", "last_alt_m", "gps_time", "freq_mhz"])
            w.writeheader()
            with self._lock:
                for r in self.sondes.values():
                    w.writerow({"serial": r["id"], "type": r["type"], "frames": r["n"], "last_lat": r["lat"], "last_lon": r["lon"],
                                "last_alt_m": r["alt"], "gps_time": r["time"],
                                "freq_mhz": f"{r['freq'] / 1e6:.3f}" if r["freq"] else ""})
        return buf.getvalue()


def _label_name(label):
    from .acars import label_name
    return label_name(label)
