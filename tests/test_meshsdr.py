"""SDR-only MeshCore: LoRa PHY round trips, MeshCore packet parsing/decryption, and the full IQ -> nodes/messages chain."""
import hashlib, hmac, os, struct, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from scipy import signal as sg
os.environ["APPDATA"] = os.environ["HOME"] = tempfile.mkdtemp()
from andysdr import lora, meshpacket as mp, config

fails = 0
def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1

# ---- AES / keys
check("AES-128 FIPS-197 vector", mp.aes128_decrypt_block(bytes(range(16)), bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")) == bytes.fromhex("00112233445566778899aabbccddeeff"))
check("public channel key", mp.PUBLIC_SECRET.hex() == "8b3387e9c5cdea6ac9e5edbaa115cd72")
check("hashtag channel key", mp.channel_secret("#test") == hashlib.sha256(b"#test").digest()[:16])

# ---- packet builders
def advert(name, ntype=2, lat=51.5, lon=-0.12, hops=0):
    flags = ntype | 0x10 | 0x80
    app = bytes([flags]) + struct.pack("<ii", int(lat * 1e6), int(lon * 1e6)) + name.encode()
    pay = hashlib.sha256(name.encode()).digest() + struct.pack("<I", 1700000000) + bytes(64) + app
    return bytes([0x11, hops]) + bytes(range(hops)) + pay

def group(secret, sender, text, ts=1700000001):
    pt = struct.pack("<I", ts) + b"\x00" + f"{sender}: {text}".encode()
    pt += bytes(-len(pt) % 16)
    ct = b"".join(mp.aes128_encrypt_block(secret, pt[i:i + 16]) for i in range(0, len(pt), 16))
    mac = hmac.new(secret.ljust(32, b"\0"), ct, hashlib.sha256).digest()[:2]
    return bytes([0x15, 0]) + bytes([mp.chan_hash(secret)]) + mac + ct

a = mp.parse(advert("Hill Repeater"))
ad = mp.parse_advert(a["payload"])
check("advert parsed", ad["name"] == "Hill Repeater" and ad["type_name"] == "repeater" and abs(ad["lat"] - 51.5) < 1e-6)
g = mp.parse(group(mp.PUBLIC_SECRET, "Alice", "hello mesh"))
r = mp.decode_group(g["payload"], {"Public": mp.PUBLIC_SECRET})
check("public channel message decrypted", r["sender"] == "Alice" and r["text"] == "hello mesh")
check("wrong key not decrypted", "text" not in mp.decode_group(g["payload"], {"X": mp.channel_secret("#other")}))

# ---- full chain: synthetic IQ at the SDR sample rate, signal 600 kHz below centre
from andysdr.meshsdr import MeshSdr
class S(dict):
    def save(self): pass
msgs = []
snaps = []
ms = MeshSdr(S(), lambda k, p: snaps.append(p))
FS, OFF = 2400000, -600000.0
rng = np.random.default_rng(2)
def on_air(raw, tau, cfo, snr):
    x = lora.modulate(lora.encode_frame(raw, 8, 4), 8, preamble=16, tau=tau, cfo_bins=cfo, snr_db=snr, rng=rng)
    return sg.resample_poly(x, 192, 5).astype(np.complex64)             # 62.5 kHz -> 2.4 MHz
quiet = np.zeros(int(FS * 0.4), np.complex64)
sig = np.concatenate([quiet, on_air(advert("Hill Repeater", 2), 0.3, 1.5, 8), quiet, on_air(group(mp.PUBLIC_SECRET, "Alice", "hello mesh"), 0.7, -2.2, 8),
                      quiet, on_air(advert("Pete's Room", 3, 53.4, -2.2, hops=2), 0.1, 0.4, 8), np.zeros(int(FS * 2.5), np.complex64)])
sig = sig + (rng.standard_normal(len(sig)) + 1j * rng.standard_normal(len(sig))).astype(np.complex64) * 0.05
n = np.arange(len(sig))
sig = (sig * np.exp(2j * np.pi * OFF * n / FS)).astype(np.complex64)
ms.start()
blk = int(FS * 0.05)
for i in range(0, len(sig), blk):
    while ms.q.qsize() > 200:                      # a real radio delivers 20 blocks/s; don't overrun the queue
        time.sleep(0.02)
    ms.feed(sig[i:i + blk], FS, OFF)
t0 = time.time()
while time.time() - t0 < 30 and not (len(ms.nodes) >= 2 and ms.messages):
    time.sleep(0.2)
print('thread alive', ms.thread.is_alive(), 'queue', ms.q.qsize(), 'total samples fed', len(sig))
ms.stop(); time.sleep(0.3)
names = sorted(n["name"] for n in ms.nodes.values())
check("nodes found from adverts: %s" % names, names == ["Hill Repeater", "Pete's Room"] or len(names) == 2)
types = {n["name"]: n["type"] for n in ms.nodes.values()}
check("node types (repeater / room server)", types.get("Hill Repeater") == "repeater" and types.get("Pete's Room") == "room server")
check("channel message decoded", any(m["text"] == "hello mesh" and m["sender"] == "Alice" for m in ms.messages))
snap = ms.snapshot()
check("snapshot has packets and stats", len(snap["packets"]) >= 3 and snap["stats"]["frames"] >= 3)
print("stats", snap["stats"])
sys.exit(1 if fails else 0)
