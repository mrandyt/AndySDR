"""The /andysdr.json card the Android app's "Find my radio" scan reads: no password, no secrets."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio, json, socket
from andysdr import server as srvmod
from andysdr.config import VERSION

fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

class R:
    def __init__(self, req): self.req = req
    async def readuntil(self, sep): return self.req
class W:
    def __init__(self): self.buf = b""; self.closed = False
    def write(self, b): self.buf += b
    async def drain(self): pass
    def close(self): self.closed = True
    def get_extra_info(self, k): return ("192.168.1.77", 51000)

def ask(path, password="secret"):
    s = srvmod.Server.__new__(srvmod.Server)
    s.s = {"server_port": 8045, "password": password, "require_password_local": False}
    s.radio = None
    w = W()
    asyncio.run(s.handle(R(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode()), w))
    head, _, body = w.buf.partition(b"\r\n\r\n")
    return head.decode(), body

head, body = ask("/andysdr.json")
check("answers 200 from another PC even with a password set", head.startswith("HTTP/1.1 200"))
card = json.loads(body)
check("says it is AndySDR", card.get("app") == "AndySDR")
check("carries the version", card.get("version") == VERSION)
check("carries the PC name", card.get("host") == socket.gethostname())
check("carries the port", card.get("port") == 8045)
check("no password or secrets in the card", set(card) == {"app", "version", "host", "port"} and "secret" not in body.decode())
check("is JSON", "application/json" in head)
head, body = ask("/api/export.csv")
check("the real data stays behind the password", head.startswith("HTTP/1.1 403"))
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
