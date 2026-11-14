"""End-to-end: the real Radio engine on the built-in Simulator, ADS-B decoder started, aircraft must appear.
Run:  python -I tests/test_radio_adsb.py   (takes about 10 s)"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home

from andysdr.config import Settings  # noqa: E402
from andysdr.engine import Radio  # noqa: E402

s = Settings()
s["device"] = "sim"
s["adsb_lat"], s["adsb_lon"] = 51.5, -1.0
r = Radio(s)
events, last = [], {}


def on(kind, payload):
    if kind == "adsb":
        last.update(payload)
    elif kind in ("event", "decoder"):
        events.append((kind, payload.get("text")))


r.add_listener(on)
r.start()
time.sleep(1.5)
r.set_decoder("adsb")
t0 = time.time()
while time.time() - t0 < 12 and (last.get("stats", {}).get("aircraft", 0) < 8):
    time.sleep(0.5)
st = last.get("stats", {})
print("stats:", st)
print("state decoder:", r.state()["decoder"], " centre:", r.dev.center, " freq:", r.freq)
for a in last.get("aircraft", [])[:4]:
    print("  ", {k: a[k] for k in ("icao", "flight", "alt", "gs", "trk", "lat", "lon", "dist", "country", "airline")})
snap = r.adsb.snapshot()
ok = st.get("aircraft", 0) >= 8 and st.get("with_pos", 0) >= 5 and r.dev.center == 1090000000
ok = ok and any(a["trail"] for a in snap["aircraft"]) and st.get("max_range", 0) > 5
# tuning away must switch the decoder off and restore normal tuning
r.set_frequency(145500000, "NFM")
time.sleep(1.0)
ok = ok and r.state()["decoder"] in (None, "") and r.dev.center != 145500000
print("after retune: decoder", r.state()["decoder"], "centre", r.dev.center)
# SBS / raw feed
import socket
r.adsb.sbs.port, r.adsb.raw.port = 39003, 39002
r.set_decoder("adsb")
r.adsb.set_share(True)
time.sleep(0.5)
c = socket.create_connection(("127.0.0.1", 39003), timeout=3)
time.sleep(3)
c.settimeout(1)
data = c.recv(4096).decode()
print("SBS sample:", data.splitlines()[0] if data else "(nothing)")
ok = ok and data.startswith("MSG,")
r.stop()
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
