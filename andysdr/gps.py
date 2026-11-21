"""USB GPS unit support: reads NMEA sentences from a serial (COM) port and keeps AndySDR's position up to date.

Works with any ordinary GPS/GNSS "puck" or dongle that shows up as a COM port (u-blox, SiRF, Prolific, CP210x ...).
The position feeds the satellite pass predictions and the ADS-B distance/bearing."""
import logging
import math
import threading
import time

log = logging.getLogger("andysdr.gps")

BAUDS = (9600, 4800, 38400, 115200)
MOVE_M = 100.0          # only re-apply the position when the unit has moved this far (metres)
FIX_AFTER_SATS = 3


def nmea_ok(line):
    """True if the sentence starts with $ and its checksum (when present) is right."""
    line = (line or "").strip()
    if not line.startswith("$"):
        return False
    if "*" in line:
        body, _, cs = line[1:].partition("*")
        try:
            want = int(cs[:2], 16)
        except ValueError:
            return False
        got = 0
        for ch in body:
            got ^= ord(ch)
        return got == want
    return True


def _deg(val, hemi):
    if not val or "." not in val:
        return None
    dot = val.index(".")
    d, m = val[:dot - 2], val[dot - 2:]
    try:
        deg = float(d or 0) + float(m) / 60.0
    except ValueError:
        return None
    return -deg if hemi in ("S", "W") else deg


def parse_nmea(line):
    """Return a dict of what the sentence tells us (any of: fix, lat, lon, alt, sats, speed_kn, utc) or {}."""
    if not nmea_ok(line):
        return {}
    f = line.strip()[1:].split("*")[0].split(",")
    kind = f[0][-3:]
    out = {}
    try:
        if kind == "GGA" and len(f) >= 10:
            q = int(f[6] or 0)
            out["fix"] = q > 0
            out["sats"] = int(f[7] or 0)
            if q > 0:
                out["lat"], out["lon"] = _deg(f[2], f[3]), _deg(f[4], f[5])
                if f[9]:
                    out["alt"] = float(f[9])
            if f[1]:
                out["utc"] = f[1][:6]
        elif kind == "RMC" and len(f) >= 8:
            ok = f[2] == "A"
            out["fix"] = ok
            if ok:
                out["lat"], out["lon"] = _deg(f[3], f[4]), _deg(f[5], f[6])
                if f[7]:
                    out["speed_kn"] = float(f[7])
            if f[1]:
                out["utc"] = f[1][:6]
        elif kind == "GLL" and len(f) >= 7:
            ok = (f[6] == "A")
            out["fix"] = ok
            if ok:
                out["lat"], out["lon"] = _deg(f[1], f[2]), _deg(f[3], f[4])
    except (ValueError, IndexError):
        return {}
    if out.get("lat") is None or out.get("lon") is None:
        out.pop("lat", None)
        out.pop("lon", None)
    return out


def distance_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in lp.comports():
        desc = " ".join(x for x in (p.description, p.manufacturer or "") if x and x != "n/a").strip() or p.device
        hw = (p.hwid or "").lower()
        likely = any(k in (desc.lower() + hw) for k in ("gps", "gnss", "u-blox", "ublox", "sirf", "1546:", "067b:", "navilock", "globalsat"))
        out.append({"port": p.device, "desc": desc, "likely": likely})
    out.sort(key=lambda d: (not d["likely"], d["port"]))
    return out


class GpsLink:
    """Background reader.  `apply(lat, lon, alt)` is called when a good fix arrives or the unit moves."""

    def __init__(self, settings, emit, apply, opener=None):
        self.s = settings
        self.emit = emit
        self.apply = apply
        self.opener = opener or self._open_serial
        self.thread = None
        self.stop_ev = threading.Event()
        self.state = {"status": "off", "port": "", "baud": 0, "fix": False, "lat": None, "lon": None,
                      "alt": None, "sats": 0, "utc": "", "error": "", "age": None}
        self.applied = None
        self._done = False          # got a settled fix this round (daily mode: close the port and wait)
        self._fix_since = 0.0
        self.next_check = None
        self._last_emit = 0.0
        self._last_fix = 0.0

    # ---- public
    def snapshot(self, with_ports=False):
        d = dict(self.state)
        d["enabled"] = bool(self.s.get("gps_enabled"))
        d["cfg_port"] = self.s.get("gps_port") or "auto"
        d["cfg_baud"] = int(self.s.get("gps_baud") or 0)
        d["cfg_hours"] = self._hours()
        d["next_check"] = self.next_check
        if self._last_fix:
            d["age"] = round(time.time() - self._last_fix, 1)
        if with_ports:
            d["ports"] = list_ports()
        return d

    def _hours(self):
        try:
            return max(0.0, float(self.s.get("gps_check_hours", 24)))
        except (TypeError, ValueError):
            return 24.0

    def configure(self, enabled=None, port=None, baud=None, hours=None):
        if hours is not None:
            try:
                self.s["gps_check_hours"] = max(0.0, float(hours))
            except (TypeError, ValueError):
                pass
        if port is not None:
            self.s["gps_port"] = str(port or "auto")
        if baud is not None:
            try:
                self.s["gps_baud"] = int(baud)
            except (TypeError, ValueError):
                self.s["gps_baud"] = 0
        if enabled is not None:
            self.s["gps_enabled"] = bool(enabled)
        self.s.save()
        self.restart()

    def autostart(self):
        if self.s.get("gps_enabled"):
            self.start()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_ev.clear()
        self.state.update(status="searching", error="")
        self.thread = threading.Thread(target=self._run, name="gps", daemon=True)
        self.thread.start()
        self._push(force=True)

    def stop(self):
        self.stop_ev.set()
        t = self.thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self.thread = None
        self.state.update(status="off", fix=False, sats=0, error="")
        self._push(force=True)

    def restart(self):
        self.stop()
        self.applied = None
        if self.s.get("gps_enabled"):
            self.start()
        else:
            self._push(force=True)

    # ---- internals
    def _push(self, force=False):
        now = time.time()
        if force or now - self._last_emit >= 2.0:
            self._last_emit = now
            try:
                self.emit("gps", self.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("gps emit")

    @staticmethod
    def _open_serial(port, baud):
        import serial
        return serial.Serial(port, baud, timeout=1.0)

    def _candidates(self):
        port = self.s.get("gps_port") or "auto"
        baud = int(self.s.get("gps_baud") or 0)
        ports = [port] if port != "auto" else [p["port"] for p in list_ports()]
        bauds = [baud] if baud else list(BAUDS)
        return [(p, b) for p in ports for b in bauds]

    def _probe(self, ser, seconds=3.0):
        """Read for a few seconds; True if valid NMEA arrives."""
        end = time.time() + seconds
        while time.time() < end and not self.stop_ev.is_set():
            line = self._readline(ser)
            if line and nmea_ok(line):
                return True
        return False

    @staticmethod
    def _readline(ser):
        raw = ser.readline()
        if not raw:
            return ""
        return raw.decode("ascii", "ignore").strip()

    def _run(self):
        backoff = 3.0
        while not self.stop_ev.is_set():
            ser = None
            found = False
            try:
                cands = self._candidates()
                if not cands:
                    self.state.update(status="no_port", error="No serial (COM) ports found – plug in the GPS unit.")
                    self._push(force=True)
                for port, baud in cands:
                    if self.stop_ev.is_set():
                        return
                    self.state.update(status="searching", port=port, baud=baud, error="")
                    self._push(force=True)
                    try:
                        ser = self.opener(port, baud)
                    except Exception as e:  # noqa: BLE001
                        self.state["error"] = f"{port}: {e}"
                        ser = None
                        continue
                    if self._probe(ser):
                        found = True
                        break
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
                    ser = None
                if found and ser is not None:
                    self.state.update(status="no_fix", error="")
                    self._done = False
                    self._fix_since = 0.0
                    self._push(force=True)
                    self._read_loop(ser)
                    if self._done and self._hours() > 0 and not self.stop_ev.is_set():
                        try:
                            ser.close()
                        except Exception:  # noqa: BLE001
                            pass
                        ser = None
                        wait = self._hours() * 3600.0
                        self.next_check = time.time() + wait
                        self.state.update(status="waiting", error="")
                        self._push(force=True)
                        self.stop_ev.wait(wait)
                        self.next_check = None
                        continue
                elif cands:
                    self.state.update(status="no_data", error=self.state.get("error") or "No GPS data found on the serial ports.")
                    self._push(force=True)
            except Exception as e:  # noqa: BLE001
                log.warning("gps: %s", e)
                self.state.update(status="error", error=str(e))
                self._push(force=True)
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
            self.stop_ev.wait(backoff)

    def _read_loop(self, ser):
        last_data = time.time()
        while not self.stop_ev.is_set():
            try:
                line = self._readline(ser)
            except Exception as e:  # noqa: BLE001
                self.state.update(status="error", error=f"Lost the GPS unit ({e})", fix=False)
                self._push(force=True)
                return
            if not line:
                if time.time() - last_data > 10:
                    self.state.update(status="error", error="GPS unit stopped sending data.", fix=False)
                    self._push(force=True)
                    return
                continue
            last_data = time.time()
            self.handle(line)
            if self._hours() > 0 and self.state["fix"] and self.applied is not None:
                now = time.time()
                self._fix_since = self._fix_since or now
                if self.state["sats"] >= 4 or now - self._fix_since > 60:
                    self._done = True
                    return
            elif not self.state["fix"]:
                self._fix_since = 0.0

    def handle(self, line):
        d = parse_nmea(line)
        if not d:
            return
        st = self.state
        if "sats" in d:
            st["sats"] = d["sats"]
        if "utc" in d:
            st["utc"] = d["utc"]
        if "fix" in d:
            if d["fix"] and "lat" in d:
                st.update(fix=True, lat=round(d["lat"], 6), lon=round(d["lon"], 6), status="fix", error="")
                if "alt" in d:
                    st["alt"] = round(d["alt"], 1)
                self._last_fix = time.time()
                self._maybe_apply()
            elif not d["fix"]:
                st["fix"] = False
                if st["status"] == "fix":
                    st["status"] = "no_fix"
        self._push()

    def _maybe_apply(self):
        st = self.state
        pos = (st["lat"], st["lon"])
        if self.applied is None or distance_m(self.applied, pos) >= MOVE_M:
            self.applied = pos
            try:
                self.apply(st["lat"], st["lon"], st["alt"])
            except Exception:  # noqa: BLE001
                log.exception("gps apply")
