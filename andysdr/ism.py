"""ISM-band sensors (rtl_433): weather stations, thermometers, TPMS, doorbells, smart meters ...

rtl_433 is a separate program (GPL) that knows several hundred of these little transmitters on 433.92 MHz, 868 MHz,
315 MHz and 915 MHz.  AndySDR runs it on the receiver it already has open (through its local rtl_tcp link, the same way
it feeds SatDump) and reads the JSON line it prints for every message it understands, for example

    {"time":"2026-11-23 16:12:01","model":"Acurite-Tower","id":1234,"channel":"A","battery_ok":1,
     "temperature_C":12.3,"humidity":65,"mic":"CRC","rssi":-9.4,"snr":14.1}

This module turns those lines into one row per physical sensor (model + id + channel): its latest readings, how many
messages, when first and last heard, signal strength.  Rows are pushed to the screens as "ism" events and kept in a
snapshot for new screens; the whole table can be exported as CSV.

Everything in a message came off the air, so values are only ever kept as short plain text / numbers and anything that
is written to a CSV is made safe against spreadsheet formulas.
"""
import collections
import csv
import io
import json
import logging
import threading
import time

log = logging.getLogger("andysdr.ism")

MAX_SENSORS = 300
MAX_VALUES = 40
# fields that describe the reception, not the sensor; they are shown in their own columns (or not at all)
META = {"time", "model", "id", "channel", "protocol", "mod", "freq", "freq1", "freq2", "rssi", "snr", "noise", "mic",
        "raw_msg", "codes", "bits", "rows", "count", "num_rows", "len", "data", "payload", "pulses", "hop", "message_type",
        "subtype", "dev", "brand", "unit"}
BANDS = {433920000: "433.92 MHz", 868300000: "868.3 MHz", 315000000: "315 MHz", 915000000: "915 MHz"}

READ_UNITS = {   # the units rtl_433 prints with -C si, used for the one-line summary
    "temperature_C": "°C", "temperature_1_C": "°C", "temperature_2_C": "°C", "humidity": "%", "pressure_hPa": " hPa",
    "pressure_kPa": " kPa", "wind_avg_km_h": " km/h", "wind_max_km_h": " km/h", "wind_dir_deg": "°", "rain_mm": " mm",
    "light_lux": " lux", "uv": " UV", "moisture": "%", "power_W": " W", "energy_kWh": " kWh", "current_A": " A",
    "voltage_V": " V", "noise_dB": " dB", "depth_cm": " cm", "co2_ppm": " ppm", "pm2_5_ug_m3": " µg/m³", "pm10_ug_m3": " µg/m³",
}


def _clean_value(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v if v == v and abs(v) < 1e15 else None
    if isinstance(v, str):
        return v[:80]
    return None


def parse_line(line):
    """One line of rtl_433 output -> dict, or None if it is not a JSON message."""
    line = (line or "").strip()
    if not line.startswith("{") or not line.endswith("}"):
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("model") else None


def summary(row):
    """'Acurite-Tower A 1234: 12.3 °C, 65 %' - for the DECODERS log and notifications."""
    bits = []
    for k, v in row["values"].items():
        if k == "battery_ok":
            if v in (0, False):
                bits.append("battery LOW")
            continue
        u = READ_UNITS.get(k)
        if u is not None and isinstance(v, (int, float)):
            bits.append(f"{v:g}{u}")
    who = row["model"] + (f" ch {row['channel']}" if row["channel"] not in ("", None) else "") + (f" #{row['id']}" if row["id"] not in ("", None) else "")
    return f"Sensor: {who}" + (": " + ", ".join(bits) if bits else "")


class IsmSensors:
    def __init__(self, emit, freq_fn=None, on_new=None, clock=time.time):
        self.emit = emit
        self.freq_fn = freq_fn or (lambda: 0)
        self.on_new = on_new                # on_new(row, text) once per message (the DECODERS log shows it)
        self.clock = clock
        self._lock = threading.Lock()
        self.rows = collections.OrderedDict()      # key -> row, least recently heard first
        self.total = 0
        self.status = {"running": False, "freq": 0, "msg": ""}

    # ------------------------------------------------------------------ input
    def feed_line(self, line):
        """Called with every line rtl_433 prints.  Never raises.  Returns the row if it was a sensor message."""
        try:
            d = parse_line(line)
            if d is None:
                return None
            return self._heard(d)
        except Exception as e:      # noqa: BLE001
            log.debug("ism: %s", e)
            return None

    def _heard(self, d):
        now = self.clock()
        model = str(d.get("model"))[:60]
        sid = d.get("id", "")
        sid = sid if isinstance(sid, (int, str)) and not isinstance(sid, bool) else ""
        sid = sid[:40] if isinstance(sid, str) else sid
        ch = d.get("channel", "")
        ch = ch if isinstance(ch, (int, str)) and not isinstance(ch, bool) else ""
        ch = ch[:20] if isinstance(ch, str) else ch
        key = f"{model}|{sid}|{ch}"
        vals = {}
        for k, v in d.items():
            if k in META or not isinstance(k, str) or len(vals) >= MAX_VALUES:
                continue
            v = _clean_value(v)
            if v is not None:
                vals[k[:40]] = v
        freq_mhz = None
        for fk in ("freq", "freq1"):
            if isinstance(d.get(fk), (int, float)) and d[fk] > 0:
                freq_mhz = float(d[fk])
                break
        if freq_mhz is None and self.freq_fn():
            freq_mhz = self.freq_fn() / 1e6
        rssi = d.get("rssi") if isinstance(d.get("rssi"), (int, float)) else None
        snr = d.get("snr") if isinstance(d.get("snr"), (int, float)) else None
        with self._lock:
            row = self.rows.pop(key, None)
            if row is None:
                row = {"key": key, "model": model, "id": sid, "channel": ch, "first": round(now, 1), "n": 0,
                       "values": {}, "freq": None, "rssi": None, "snr": None}
            row["n"] += 1
            row["last"] = round(now, 1)
            row["values"].update(vals)                  # keep readings a later, shorter message did not repeat
            row["freq"] = round(freq_mhz, 4) if freq_mhz else row["freq"]
            row["rssi"] = rssi if rssi is not None else row["rssi"]
            row["snr"] = snr if snr is not None else row["snr"]
            self.rows[key] = row
            while len(self.rows) > MAX_SENSORS:
                self.rows.popitem(last=False)
            self.total += 1
            out = self._public(row)
        try:
            self.emit("ism", out)
        except Exception:      # noqa: BLE001
            pass
        if self.on_new:
            try:
                self.on_new(row, summary(row))
            except Exception:      # noqa: BLE001
                pass
        return out

    # ------------------------------------------------------------------ for the screens
    @staticmethod
    def _public(row):
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in row.items()}

    def snapshot(self):
        with self._lock:
            return {"rows": [self._public(r) for r in reversed(self.rows.values())],        # newest first
                    "total": self.total, "status": dict(self.status), "now": round(self.clock(), 1)}

    def set_status(self, running, freq=0, msg=""):
        self.status = {"running": bool(running), "freq": int(freq or 0), "msg": str(msg)[:200]}
        try:
            self.emit("ism_status", self.status)
        except Exception:      # noqa: BLE001
            pass

    def clear(self):
        with self._lock:
            self.rows.clear()
            self.total = 0
        try:
            self.emit("ism_all", {"rows": [], "total": 0, "status": dict(self.status)})
        except Exception:      # noqa: BLE001
            pass

    CSV_BASE = ("first_heard", "last_heard", "model", "id", "channel", "messages", "frequency_mhz", "rssi_db", "snr_db")

    def export_csv(self):
        """Every sensor as one CSV line (oldest first): the base columns, then one column per reading seen."""
        def safe(v):
            v = "" if v is None else str(v)
            return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
        with self._lock:
            rows = list(self.rows.values())
        rows.sort(key=lambda r: r["first"])
        cols = []
        for r in rows:
            for k in r["values"]:
                if k not in cols:
                    cols.append(k)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(list(self.CSV_BASE) + cols)
        for r in rows:
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["first"])),
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["last"])),
                        safe(r["model"]), safe(r["id"]), safe(r["channel"]), r["n"],
                        r["freq"] if r["freq"] is not None else "", r["rssi"] if r["rssi"] is not None else "",
                        r["snr"] if r["snr"] is not None else ""] + [safe(r["values"].get(c, "")) for c in cols])
        return buf.getvalue()
