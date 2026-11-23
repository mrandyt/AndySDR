"""ISM sensors (rtl_433): reading its JSON lines into one row per sensor, CSV, the rtl_tcp feed and the running engine.
Run:  python -I tests/test_ism.py"""
import csv
import io
import os
import stat
import sys
import tempfile
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["APPDATA"] = home

import numpy as np  # noqa: E402
from andysdr.ism import IsmSensors, parse_line, summary  # noqa: E402
from andysdr.iqbridge import IqBridge  # noqa: E402

fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

# ---------------------------------------------------------------- parsing
check("a JSON message is read", parse_line('{"model":"X","id":1}') == {"model": "X", "id": 1})
check("rtl_433's own chatter is not", parse_line("rtl_433 version 25.12") is None and parse_line("[Input] Using rtl_tcp") is None)
check("broken or model-less JSON is ignored", parse_line('{"model":') is None and parse_line('{"id":1}') is None and parse_line("") is None and parse_line(None) is None)

# ---------------------------------------------------------------- the table
now = [1_700_000_000.0]
events, logged = [], []
t = IsmSensors(lambda k, p: events.append((k, p)), freq_fn=lambda: 433920000, on_new=lambda r, text: logged.append(text), clock=lambda: now[0])
L1 = '{"time":"2026-11-23 16:12:01","model":"Acurite-Tower","id":1234,"channel":"A","battery_ok":1,"temperature_C":12.3,"humidity":65,"mic":"CRC","rssi":-9.4,"snr":14.1,"freq":433.92}'
row = t.feed_line(L1)
check("the first message makes a sensor row", row and row["model"] == "Acurite-Tower" and row["id"] == 1234 and row["channel"] == "A" and row["n"] == 1)
check("... with its readings but not the reception fields", row["values"] == {"battery_ok": 1, "temperature_C": 12.3, "humidity": 65})
check("... signal strength and frequency in their own fields", row["rssi"] == -9.4 and row["snr"] == 14.1 and row["freq"] == 433.92)
check("... pushed to the screens, and written to the log", events[-1][0] == "ism" and logged[-1] == "Sensor: Acurite-Tower ch A #1234: 12.3°C, 65%")
now[0] += 60
row = t.feed_line(L1.replace("12.3", "12.9").replace('"humidity":65,', ""))
check("the same sensor again updates its row", len(t.rows) == 1 and row["n"] == 2 and row["values"]["temperature_C"] == 12.9)
check("... a reading a later message left out is kept", row["values"]["humidity"] == 65)
t.feed_line('{"model":"Acurite-Tower","id":1234,"channel":"B","temperature_C":5}')
t.feed_line('{"model":"Toyota","type":"TPMS","id":"abcd1234","pressure_kPa":230.5,"temperature_C":18,"battery_ok":0}')
check("a different channel or ID is a different sensor", len(t.rows) == 3)
check("a TPMS ID given as text works and LOW battery is called out", "battery LOW" in logged[-1] and "230.5 kPa" in logged[-1])
snap = t.snapshot()
check("the snapshot is newest first", snap["rows"][0]["model"] == "Toyota" and snap["total"] == 4)
check("the snapshot is a copy", snap["rows"][2]["values"] is not t.rows[next(iter(t.rows))]["values"])
r3 = t.feed_line('{"model":"NoFreq","id":9}')
check("with no frequency in the message the receiver's is used", r3["freq"] == 433.92)

junk = t.feed_line('{"model":"Weird","id":true,"channel":{"a":1},"x":[1,2],"y":{"z":1},"ok":"fine","big":1e300,"nan":NaN}')
check("odd values from the air are dropped, not stored", junk is not None and junk["id"] == "" and junk["channel"] == "" and junk["values"] == {"ok": "fine"})
longname = t.feed_line('{"model":"' + "M" * 500 + '","id":1,"s":"' + "v" * 500 + '"}')
check("very long text is cut short", len(longname["model"]) == 60 and len(longname["values"]["s"]) == 80)
for i in range(400):
    t.feed_line('{"model":"Bulk","id":%d,"v":%d}' % (i, i))
check("the table is capped and drops the oldest", len(t.rows) == 300 and "Bulk|399|" in t.rows and "Acurite-Tower|1234|A" not in t.rows)

# ---------------------------------------------------------------- CSV
t2 = IsmSensors(lambda k, p: None, clock=lambda: now[0])
t2.feed_line(L1)
now[0] += 5
t2.feed_line('{"model":"=HYPERLINK(\\"x\\")","id":2,"power_W":300,"channel":"-1"}')
rows = list(csv.reader(io.StringIO(t2.export_csv())))
check("CSV: base columns then one per reading, oldest first", rows[0][:9] == list(IsmSensors.CSV_BASE) and rows[0][9:] == ["battery_ok", "temperature_C", "humidity", "power_W"] and rows[1][2] == "Acurite-Tower")
check("CSV: a sensor that never sent a reading has it blank", rows[2][9:] == ["", "", "", "300"])
check("CSV: a model name that looks like a formula is made safe", rows[2][2].startswith("'=") and rows[2][4] == "'-1")
check("CSV: an empty table is only a header", len(list(csv.reader(io.StringIO(IsmSensors(lambda k, p: None).export_csv())))) == 1)
t2.clear()
check("clear empties it and tells the screens", not t2.rows and t2.snapshot()["total"] == 0)
t2.set_status(True, 868300000, "")
check("status is kept and pushed", t2.snapshot()["status"]["running"] is True and t2.snapshot()["status"]["freq"] == 868300000)

# ---------------------------------------------------------------- the rtl_tcp feed: a steady scale for rtl_433
br = IqBridge(out_rate=250000, agc_target=12.0)
rng = np.random.default_rng(1)
def block(level, n=240000):
    return (rng.normal(0, level, n) + 1j * rng.normal(0, level, n)).astype(np.complex64)
fs = 2400000
for _ in range(30):
    out = np.frombuffer(br.convert(block(0.02, 240000), fs), np.uint8).astype(float) - 127.5
sd = out.std()
check("noise is scaled to about the target (8-bit steps)", 6 < sd < 25)
big = np.frombuffer(br.convert(block(0.02) + 0.5, fs), np.uint8)
check("a strong signal is not rescaled away: the noise floor stays put", abs((np.frombuffer(br.convert(block(0.02), fs), np.uint8).astype(float) - 127.5).std() - sd) < 5)
old = IqBridge(out_rate=250000)
a = np.frombuffer(old.convert(block(1.0), fs), np.uint8).astype(float) - 127.5
b = np.frombuffer(old.convert(block(0.002), fs), np.uint8).astype(float) - 127.5
check("the SatDump setting is unchanged: loud blocks are scaled to their peak, quiet ones not blown up", 120 < a.max() <= 128 and b.std() < 3)
check("output length matches the new rate", len(br.convert(block(0.02, 240000), fs)) == 2 * 25000)

# ---------------------------------------------------------------- the real engine on the simulator, with a stand-in rtl_433
exe = os.path.join(home, "rtl_433")
with open(exe, "w") as f:
    f.write(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json, socket, sys, time
        a = sys.argv[1:]
        spec = a[a.index("-d") + 1]
        host, port = spec.split(":")[1], int(spec.split(":")[2])
        s = socket.create_connection((host, port), timeout=5)
        hdr = s.recv(12)
        print("Registered 150 out of 250 device decoding protocols", flush=True)
        got = 0; t0 = time.time()
        while got < 100000 and time.time() - t0 < 10:
            d = s.recv(65536)
            if not d: break
            got += len(d)
        print(json.dumps({"model": "Fake-Thermo", "id": 77, "channel": 1, "temperature_C": 21.5, "battery_ok": 1, "rssi": -8.2, "bytes": got, "args": " ".join(a), "hdr": hdr[:4].decode()}), flush=True)
        time.sleep(60)
    '''))
os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)

from andysdr.config import Settings  # noqa: E402
from andysdr.engine import Radio  # noqa: E402

s = Settings()
s["device"] = "sim"
s["decoder_paths"] = {"rtl_433": exe}
r = Radio(s)
evs = []
r.add_listener(lambda k, p: evs.append((k, p)))
r.start()
time.sleep(1.5)
WIN = sys.platform.startswith("win")      # the stand-in program is a script with a #! line: that is only run on Linux / macOS
if WIN:
    print("SKIP engine run with a stand-in rtl_433 (Windows)")
else:
    r.start_ism(868300000)
    for _ in range(60):
        time.sleep(0.25)
        if r.ism.rows:
            break
    check("decoder active", r.decoders.active == "ism")
    check("tuned to the sensor frequency", abs(r.freq - 868300000) < 1000)
    check("a sensor appeared from the program's output", len(r.ism.rows) == 1)
    row = list(r.ism.rows.values())[0] if r.ism.rows else {"values": {}}
    check("... which really received samples over the rtl_tcp link", row["values"].get("bytes", 0) >= 100000 and row["values"].get("hdr") == "RTL0")
    check("... started with the right options", "-s 250000" in row["values"].get("args", "") and "-F json" in row["values"].get("args", "") and "-f 868.3M" in row["values"].get("args", "") and "-C si" in row["values"].get("args", ""))
    check("the screens were told", any(k == "ism" for k, _ in evs) and any(k == "ism_status" and p["running"] for k, p in evs))
    check("the Decoders log shows the sensor and the program's start-up line", any(k == "decoder" and "Sensor: Fake-Thermo" in p["text"] for k, p in evs) and any(k == "decoder" and "rtl_433: Registered" in p["text"] for k, p in evs))
    r.stop_ism()
    time.sleep(1.0)
    check("stopped cleanly", r.decoders.active in (None, "") and r.decoders.bridge is None and r.ism.snapshot()["status"]["running"] is False)
    r.start_ism()
    time.sleep(2.0)
    check("start with no frequency uses 433.92 MHz", abs(r.freq - 433920000) < 1000)
    r.decoders.stop()
s["decoder_paths"] = {"rtl_433": os.path.join(home, "missing.exe")}
r.start_ism()
time.sleep(1.5)
check("a missing rtl_433 is reported, not a crash", r.decoders.active in (None, "") and r.decoders.status == "error")
r.stop()

# ---------------------------------------------------------------- the download rule picks the right file
from andysdr import fetchdecoders as fd  # noqa: E402
names = ["rtl_433-rtlsdr-MacOS-arm64-25.12.zip", "rtl_433-rtlsdr-openssl3-Linux-amd64-25.12.zip", "rtl_433-soapysdr-openssl3-Linux-arm64-25.12.zip",
         "rtl_433-win-x32-25.12.zip", "rtl_433-win-msvc-x64-25.12.zip"]
import json as _json
fake = _json.dumps([{"tag_name": "25.12", "draft": False, "prerelease": False,
                     "assets": [{"name": n, "browser_download_url": "https://x/" + n} for n in names]}]).encode()
fd._get = lambda url, token=None: fake
hit = fd.find_release_asset("rtl_433", log=lambda m: None)
check("the 64-bit Windows zip is the one chosen", hit and hit[1] == "rtl_433-win-msvc-x64-25.12.zip")

print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
