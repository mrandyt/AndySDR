"""Stand-in for `satdump live <pipeline> <out> --source rtltcp ... --http_server host:port` used by test_aero.py.

It behaves like the real thing in the ways AndySDR depends on: connects to the rtl_tcp bridge and reads IQ, serves module
statistics as JSON on /api, and writes one JSON file per decoded message into the output folder.  Only the 600 bit/s pipeline
'locks' (so AUTO mode has to step to it)."""
import http.server
import json
import os
import socket
import struct
import sys
import threading
import time

a = sys.argv[1:]
assert a[0] == "live", a
pipe, out = a[1], a[2]
opt = {a[i][2:]: a[i + 1] for i in range(3, len(a) - 1, 2) if a[i].startswith("--")}
LOCKS = pipe == "inmarsat_aero_6"
state = {"bytes": 0}
print(f"fake satdump pipeline={pipe}", flush=True)

s = socket.create_connection((opt["ip_address"], int(opt["port"])), timeout=5)
hdr = s.recv(12)
assert hdr[:4] == b"RTL0", hdr
s.sendall(struct.pack(">BI", 2, int(opt["samplerate"])))


def reader():
    while True:
        d = s.recv(65536)
        if not d:
            return
        state["bytes"] += len(d)


threading.Thread(target=reader, daemon=True).start()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        st = {"demod": {"snr": 7.5 if LOCKS else 0.4, "peak_snr": 9.1 if LOCKS else 1.0},
              "inmarsat_aero_decoder": {"correlator_lock": LOCKS, "correlator_corr": 40.0 if LOCKS else 3.0,
                                        "viterbi_ber": 0.05 if LOCKS else 0.48,
                                        "lock_state": "SYNCED" if LOCKS else "NOSYNC"}}
        b = json.dumps(st).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


host, port = opt["http_server"].split(":")
srv = http.server.ThreadingHTTPServer((host, int(port)), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

if LOCKS:
    time.sleep(1.0)
    d = os.path.join(out, "ACARS")
    os.makedirs(d, exist_ok=True)
    msg = {"mode": 50, "tak": 33, "label": "H1", "bi": 65, "plane_reg": ".G-EUUU", "has_text": True,
           "message": "TEST UPLINK 1", "more_to_come": False, "msg_name": "ACARS", "timestamp": time.time(),
           "signal_unit": {"aes_id": 0x4CA123, "ges_id": 5, "message_type": "ISU"}}
    print("[12:00:00 - 25/11/2026] (I) Packet : AES System Table Broadcast Index", flush=True)
    print("[12:00:00 - 25/11/2026] (I) ACARS message (G-EUUU) : ", flush=True)
    print("TEST UPLINK 1", flush=True)
    print("[12:00:01 - 25/11/2026] (W) Invalid CRC!", flush=True)
    with open(os.path.join(d, "20261125T120000Z.json"), "w") as f:
        json.dump(msg, f)
    d2 = os.path.join(out, "AES System Table Broadcast Index")
    os.makedirs(d2, exist_ok=True)
    with open(os.path.join(d2, "20261125T120001Z.json"), "w") as f:
        json.dump({"msg_name": "AES System Table Broadcast Index", "timestamp": time.time()}, f)
while True:
    time.sleep(0.2)
    print(f"bytes={state['bytes']}", flush=True)
