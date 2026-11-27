"""AndySDR settings: stored as JSON next to the exe (or in %APPDATA%\\AndySDR)."""
import json
import os
import secrets
import sys
import threading

APP_NAME = "AndySDR"
VERSION = "1.25.3"
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
    "satrx_enabled": False,       # a second receiver (RTL-SDR dongle) used only for 137 MHz Meteor passes
    "satrx_device": "",           # its key ("" = the first RTL-SDR dongle the main radio is not using)
    "tidy_min_fill": 0,           # set aside satellite pictures that are less than this % filled in (0 = off)
    "satrx_both": False,          # record a Meteor pass on BOTH receivers and merge the frames they each heard
    "satrx_gain": 38.0,           # dB
    "satrx_gain_mode": "fixed",   # fixed | auto (learn the best)
    "satrx_bias": False,          # bias-T (powers an LNA through the coax)
    "satrx_ppm": 0,
    "keep_iq": False,             # keep the raw Meteor IQ recording after SatDump has made the pictures (large files)
    "hotspot_name": "My hotspot", "hotspot_freq": 438800000, "hotspot_cc": 1, "hotspot_ts": 2, "hotspot_tg": "",   # saved DMR hotspot
    "notch_on": False, "notch_hz": 1000, "notch_q": 20, "auto_notch": False,   # audio notch filters
    "noise_blanker": True,        # remove impulse noise (ignition, switching) in AM / SSB / CW
    "bw_auto": True,              # channel bandwidth follows the suggestion (decoder / Auto ID / band plan)
    "bw_modes": {},               # bandwidth (Hz) chosen by hand, per mode
    "squelch": -60.0,             # dBFS, -120 = open
    "volume": 0.7,
    "ppm": 0,
    "antenna": "",
    "bias_tee": False,
    "dev_switches": {},           # per receiver: bias-T / notch switches restored at start
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
    "theme": "modern",            # modern | modernlight | bluedark | bluelight | softdark | softlight | steel | steelgrey
    "auto_decoder": True,         # start a memory channel's decoder automatically
    "auto_identify": True,        # identify signals and start the right decoder by itself
    "schedule": [],               # timed captures (WEFAX, SSTV, LRPT, recordings)
    "rtl_tcp_servers": [],
    "mesh_conn": {},              # MeshCore companion board: {"kind": serial|tcp|ble, "target": ..., "auto": True}        # e.g. ["192.168.1.20:1234"]
    "tetra_udp_port": 42000,
    "dsd_extra_args": "",
    "dsd_dc_remove": True,           # centre the discriminator audio before DSD-FME (cures tuning-error decode failures)
    "dsd_memory": {},                 # frequency (kHz) -> the DSD-FME frame type that locked there, so Auto starts on it
    "dsd_gate": False,                # feed DSD-FME silence while the squelch is closed (stops "voice" decoded from noise)
    "auto_gain": False,               # ease the RF gain down by itself when a signal is strong enough to distort (the knob is the maximum)
    "mute_digital_audio": True,       # silence the raw data noise while a digital decoder (DSD, TETRA, pager) runs
    "dsd_gain": 6000,                # discriminator -> 16-bit scale for DSD-FME
    "pager_aliases": {},          # capcode -> name shown in the Pager Messages tab
    "pager_highlight": [],        # capcodes to highlight (and pop up a notice for)
    "pager_log": True,            # write pager messages to %APPDATA%\\AndySDR\\pager\\pager_YYYY-MM-DD.csv
    "adsb_lat": None,             # receiver position (decimal degrees) - gives distance/bearing and faster fixes
    "adsb_lon": None,
    "gps_enabled": False,         # take my position from a USB GPS unit
    "gps_port": "auto",           # COM port of the GPS unit, or auto to find it
    "gps_check_hours": 24,        # look for a new position this often (0 = keep listening and follow movement)
    "gps_baud": 0,                # 0 = try the usual speeds
    "adsb_log": True,             # one CSV line per aircraft every 30 s in %APPDATA%\\AndySDR\\adsb
    "adsb_lookup": True,          # look up registration / type / owner / photo online (sends the hex code only)
    "adsb_share": False,          # re-broadcast as SBS (30003) and raw AVR (30002) for other programs
    "adsb_sbs_port": 30003,
    "adsb_raw_port": 30002,
    "alert_pager": True, "alert_emergency": True, "alert_priority": True, "alert_capture": True, "alert_watch": True, "alert_pass": True,   # what the tablet app is notified about
    "watch_list": [],             # callsigns, radio IDs and talkgroups to alert on when heard (DMR / P25 / NXDN / M17)
    "dmr_db_auto": True,          # download / refresh the RadioID callsign list by itself (names in DMR Last heard)
    "dmr_db_url": "",             # optional own address for that list (blank = RadioID.net)
    "hidden_buttons": None,       # front-panel buttons hidden with CUSTOMISE, shared by every screen (None = nobody has customised yet)
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
