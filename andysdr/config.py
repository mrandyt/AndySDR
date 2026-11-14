"""AndySDR settings: stored as JSON next to the exe (or in %APPDATA%\\AndySDR)."""
import json
import os
import secrets
import sys
import threading

APP_NAME = "AndySDR"
VERSION = "1.5.0"
GITHUB_REPO = "mrandyt/AndySDR"   # where updates are published (GitHub Releases)


def app_dir():
    """Folder that holds the program (exe folder when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_dir():
    """Folder holding bundled read-only resources (web GUI)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """Writable folder for settings, memories, images, recordings."""
    base = os.environ.get("APPDATA")
    if base and sys.platform.startswith("win"):
        d = os.path.join(base, APP_NAME)
    else:
        d = os.path.join(os.path.expanduser("~"), ".andysdr")
    os.makedirs(d, exist_ok=True)
    for sub in ("images", "recordings", "logs"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


DEFAULTS = {
    "device": "",                 # device key chosen in the Device panel ("" = first found)
    "sample_rate": 2400000,
    "frequency": 145500000,       # tuned (VFO) frequency in Hz
    "mode": "NFM",
    "step": 12500,
    "rf_gain": 30.0,              # dB
    "agc": False,
    "squelch": -60.0,             # dBFS, -120 = open
    "volume": 0.7,
    "ppm": 0,
    "antenna": "",
    "bias_tee": False,
    "deemphasis_us": 50,          # UK/Europe broadcast FM
    "server_host": "0.0.0.0",     # 0.0.0.0 = allow other PCs on the network
    "server_port": 8045,
    "password": "",               # generated on first run
    "require_password_local": False,
    "scan_delay": 2.0,            # seconds to wait after a transmission ends
    "scan_dwell": 0.12,           # seconds per memory channel
    "search_start": 144000000,
    "search_stop": 146000000,
    "search_step": 12500,
    "search_threshold_db": 10.0,  # dB above noise floor to count as a live signal
    "search_mode": "stop",        # "stop" = stop on activity, "log" = sweep & log only
    "decoder": "",                # active decoder id
    "decoder_paths": {},          # user overrides of external decoder exe paths
    "window_open_browser": False,
    "theme": "pro2045",           # pro2045 | ubcd260 | trx2
    "auto_decoder": True,         # start a memory channel's decoder automatically
    "auto_identify": True,        # identify signals and start the right decoder by itself
    "schedule": [],               # timed captures (WEFAX, SSTV, LRPT, recordings)
    "rtl_tcp_servers": [],
    "mesh_conn": {},              # MeshCore companion board: {"kind": serial|tcp|ble, "target": ..., "auto": True}        # e.g. ["192.168.1.20:1234"]
    "tetra_udp_port": 42000,
    "dsd_extra_args": "",
    "pager_aliases": {},          # capcode -> name shown in the Pager Messages tab
    "pager_highlight": [],        # capcodes to highlight (and pop up a notice for)
    "pager_log": True,            # write pager messages to %APPDATA%\\AndySDR\\pager\\pager_YYYY-MM-DD.csv
    "adsb_lat": None,             # receiver position (decimal degrees) - gives distance/bearing and faster fixes
    "adsb_lon": None,
    "adsb_log": True,             # one CSV line per aircraft every 30 s in %APPDATA%\\AndySDR\\adsb
    "adsb_share": False,          # re-broadcast as SBS (30003) and raw AVR (30002) for other programs
    "adsb_sbs_port": 30003,
    "adsb_raw_port": 30002,
    "update_check": True,         # look for a newer AndySDR on GitHub (start-up + every 12 h) and ask before doing anything
    "update_auto_download": False,  # download in the background, then ask before installing
    "update_skip": "",            # version the user chose to skip
    "update_last_check": 0,
}


class Settings:
    def __init__(self):
        self.path = os.path.join(data_dir(), "settings.json")
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass
        if not self.data.get("password"):
            # Easy to read out loud / type on another PC
            words = secrets.token_hex(3).upper()
            self.data["password"] = f"ANDY-{words}"
        self.save()

    def __getitem__(self, k):
        return self.data.get(k, DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, default=None):
        return self.data.get(k, default)

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
