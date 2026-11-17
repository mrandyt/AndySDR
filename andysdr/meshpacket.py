"""MeshCore over-the-air packet parsing for the receive-only SDR decoder.

Adverts (repeaters, room servers, companions, sensors) are not encrypted, so every node that announces itself can be
listed with its name and position.  Channel messages are decrypted with the channel secrets AndySDR knows (the public
channel and any #hashtag channel you add).  Direct messages and other private traffic stay encrypted – they are
counted and shown by type, hops and path only.
"""
import base64
import hashlib
import hmac
import struct

ROUTE = {0: "transport flood", 1: "flood", 2: "direct", 3: "transport direct"}
PTYPE = {0: "request", 1: "response", 2: "direct message", 3: "ack", 4: "advert", 5: "channel text", 6: "channel data",
         7: "anonymous request", 8: "path", 9: "trace", 10: "multipart", 11: "control", 15: "custom"}
NODE_TYPE = {0: "unknown", 1: "chat", 2: "repeater", 3: "room server", 4: "sensor"}

# The MeshCore "Public" channel secret (a well-known shared key).
PUBLIC_SECRET = base64.b64decode("izOH6cXN6mrJ5e26oRXNcg==")


# --------------------------------------------------------------------------- AES-128 (decrypt only, no dependencies)
def _xt(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


def _make_sbox():
    inv = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if _mul(a, b) == 1:
                inv[a] = b
                break
    rot = lambda v, n: ((v << n) | (v >> (8 - n))) & 0xFF
    return [inv[a] ^ rot(inv[a], 1) ^ rot(inv[a], 2) ^ rot(inv[a], 3) ^ rot(inv[a], 4) ^ 0x63 for a in range(256)]


_SBOX = _make_sbox()
_INV = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV[_v] = _i


def _expand(key):
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rc = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[t[1]] ^ rc, _SBOX[t[2]], _SBOX[t[3]], _SBOX[t[0]]]
            rc = _xt(rc)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def aes128_decrypt_block(key, block):
    rk = _expand(key)
    s = [b ^ k for b, k in zip(block, rk[10])]
    for rnd in range(9, -1, -1):
        # inverse shift rows
        s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11], s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
        s = [_INV[b] for b in s]
        s = [b ^ k for b, k in zip(s, rk[rnd])]
        if rnd:
            out = []
            for c in range(4):
                a = s[c * 4:c * 4 + 4]
                out += [_mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9),
                        _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13),
                        _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11),
                        _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)]
            s = out
    return bytes(s)


def aes128_encrypt_block(key, block):
    """Forward cipher – only used by the self-tests to build sample packets."""
    rk = _expand(key)
    s = [b ^ k for b, k in zip(block, rk[0])]
    for rnd in range(1, 11):
        s = [_SBOX[b] for b in s]
        s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3], s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
        if rnd < 10:
            out = []
            for c in range(4):
                a = s[c * 4:c * 4 + 4]
                out += [_mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3], a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3],
                        a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3), _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)]
            s = out
        s = [b ^ k for b, k in zip(s, rk[rnd])]
    return bytes(s)


def aes128_ecb_decrypt(key, data):
    return b"".join(aes128_decrypt_block(key, data[i:i + 16]) for i in range(0, len(data) - len(data) % 16, 16))


# --------------------------------------------------------------------------- channels
def channel_secret(spec):
    """'#name' -> sha256('#name')[:16];  32 hex digits or base64 -> the secret itself;  'public' -> the public key."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.lower() == "public":
        return PUBLIC_SECRET
    if spec.startswith("#"):
        return hashlib.sha256(spec.encode()).digest()[:16]
    try:
        if len(spec) == 32:
            return bytes.fromhex(spec)
        b = base64.b64decode(spec)
        if len(b) == 16:
            return b
    except Exception:  # noqa: BLE001
        pass
    return None


def chan_hash(secret):
    return hashlib.sha256(secret).digest()[0]


def try_decrypt(secret, mac, cipher):
    if len(cipher) == 0 or len(cipher) % 16:
        return None
    calc = hmac.new(secret.ljust(32, b"\0"), cipher, hashlib.sha256).digest()[:2]
    if calc != mac:
        return None
    return aes128_ecb_decrypt(secret, cipher)


# --------------------------------------------------------------------------- packets
def parse(raw):
    """Raw LoRa payload -> dict, or None if it is not a MeshCore packet."""
    if len(raw) < 3:
        return None
    h = raw[0]
    route, ptype, ver = h & 3, (h >> 2) & 0xF, h >> 6
    i = 1
    d = {"route": route, "route_name": ROUTE[route], "ptype": ptype, "type_name": PTYPE.get(ptype, f"type {ptype}"),
         "ver": ver}
    if route in (0, 3):
        if len(raw) < i + 4:
            return None
        d["transport"] = raw[i:i + 4].hex()
        i += 4
    if len(raw) <= i:
        return None
    pl = raw[i]
    i += 1
    hops, hs = pl & 0x3F, (pl >> 6) + 1
    if hs == 4:                                     # reserved -> old firmware: the byte is a plain hop count
        hops, hs = pl, 1
    if i + hops * hs > len(raw):
        return None
    d["hops"] = hops
    d["hash_size"] = hs
    d["path"] = [raw[i + k * hs:i + (k + 1) * hs].hex() for k in range(hops)]
    i += hops * hs
    d["payload"] = raw[i:]
    if ptype not in PTYPE or len(d["payload"]) == 0:
        return None
    return d


def parse_advert(p):
    """Advert payload -> dict(name, node_type, lat, lon, pubkey, timestamp) or None."""
    if len(p) < 32 + 4 + 64 + 1:
        return None
    pub = p[:32]
    ts = struct.unpack("<I", p[32:36])[0]
    ap = p[100:]
    flags = ap[0]
    j = 1
    out = {"pubkey": pub.hex(), "timestamp": ts, "node_type": flags & 0x0F, "type_name": NODE_TYPE.get(flags & 0x0F, "unknown"),
           "lat": None, "lon": None, "name": ""}
    if flags & 0x10:
        if len(ap) < j + 8:
            return None
        la, lo = struct.unpack("<ii", ap[j:j + 8])
        j += 8
        if la or lo:
            out["lat"], out["lon"] = la / 1e6, lo / 1e6
    if flags & 0x20:
        j += 2
    if flags & 0x40:
        j += 2
    if flags & 0x80:
        out["name"] = ap[j:].split(b"\0")[0].decode("utf-8", "replace").strip()
    return out


def decode_group(p, channels):
    """Channel text payload -> (channel name, sender, text, timestamp) if a known key opens it, else (None, ...)."""
    if len(p) < 3 + 16:
        return None
    ch, mac, cipher = p[0], p[1:3], p[3:]
    hit_hash = False
    for name, secret in channels.items():
        if chan_hash(secret) != ch:
            continue
        hit_hash = True
        pt = try_decrypt(secret, mac, cipher)
        if pt is None or len(pt) < 6:
            continue
        ts = struct.unpack("<I", pt[:4])[0]
        txt = pt[5:].split(b"\0")[0].decode("utf-8", "replace")
        sender, _, msg = txt.partition(": ")
        if not msg:
            sender, msg = "", txt
        return {"channel": name, "sender": sender, "text": msg, "timestamp": ts, "txt_type": pt[4] >> 2}
    return {"hash": "%02x" % ch, "known_hash": hit_hash}


def summarize(d, advert=None, group=None):
    """One-line description of a parsed packet for the live packet log."""
    t = d["type_name"]
    hop = f"{d['hops']} hop{'s' if d['hops'] != 1 else ''}"
    if advert:
        pos = f" @ {advert['lat']:.4f},{advert['lon']:.4f}" if advert["lat"] is not None else ""
        return f"{advert['type_name']} advert: {advert['name'] or advert['pubkey'][:12]}{pos}"
    if group and "text" in group:
        return f"[{group['channel']}] {group['sender']}: {group['text']}".replace(" : ", " ")
    if group:
        return "channel message (encrypted, channel %s%s)" % (group["hash"], "" if group.get("known_hash") else ", key unknown")
    if d["ptype"] in (0, 1, 2, 7) and len(d["payload"]) >= 2:
        return f"{t}: {d['payload'][1]:02x} → {d['payload'][0]:02x} (encrypted, {hop})"
    return f"{t} ({hop}, {len(d['payload'])} bytes)"
