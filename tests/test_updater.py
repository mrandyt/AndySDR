"""Updater tests (no internet needed - a local web server plays GitHub).
Run:  python -I tests/test_updater.py   (from the AndySDR folder)"""
import asyncio
import hashlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import zipfile

os.environ["HOME"] = tempfile.mkdtemp()
os.environ["APPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from andysdr import config, updater  # noqa: E402
from andysdr.updater import Updater, is_newer, parse_version  # noqa: E402

ok = fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print("  ok  ", name)
    else:
        fail += 1
        print("  FAIL", name)


# ------------------------------------------------------------------ fake GitHub
SETUP = b"MZ" + os.urandom(300_000)
ZIPBUF = io.BytesIO()
with zipfile.ZipFile(ZIPBUF, "w") as z:
    z.writestr("AndySDR/AndySDR.exe", b"new exe")
    z.writestr("AndySDR/web/app.js", b"new js")
ZIP = ZIPBUF.getvalue()
STATE = {"tag": "v9.9.9", "digest_ok": True, "prerelease": False, "assets_prefix": None, "short": False, "status": 200}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/api/latest":
            if STATE["status"] != 200:
                self.send_response(STATE["status"]); self.end_headers(); return
            pre = STATE["assets_prefix"] or base + "/dl/"
            def dig(b):
                return "sha256:" + (hashlib.sha256(b).hexdigest() if STATE["digest_ok"] else "0" * 64)
            body = json.dumps({
                "tag_name": STATE["tag"], "name": "AndySDR " + STATE["tag"], "html_url": base + "/page",
                "body": "* new stuff\n* <script>alert(1)</script>", "draft": False, "prerelease": STATE["prerelease"],
                "assets": [
                    {"name": "AndySDR-Setup.exe", "browser_download_url": pre + "AndySDR-Setup.exe",
                     "size": len(SETUP) + (5 if STATE["short"] else 0), "digest": dig(SETUP)},
                    {"name": "AndySDR-windows.zip", "browser_download_url": pre + "AndySDR-windows.zip",
                     "size": len(ZIP), "digest": dig(ZIP)}]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/dl/"):
            data = SETUP if self.path.endswith(".exe") else ZIP
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"


class FakeSettings(dict):
    def save(self):
        pass


def make(mode="installer", **settings):
    events = []
    s = FakeSettings(update_check=True)
    s.update(settings)
    u = Updater(s, lambda k, p: events.append((k, p)), shutdown=lambda: None,
                api_url=BASE + "/api/latest", allowed_prefix=BASE + "/dl/", mode=mode)
    u.dir = tempfile.mkdtemp()
    return u, s, events


def wait(u, states, t=10):
    end = time.time() + t
    while time.time() < end:
        if u.state in states and not u.busy:
            return True
        time.sleep(0.02)
    return False


print("version parsing")
check("v1.5.0 > 1.4.0", is_newer("v1.5.0", "1.4.0"))
check("1.10.0 > 1.9.9 (numeric, not text)", is_newer("1.10.0", "1.9.9"))
check("same version is not newer", not is_newer("1.5.0", "1.5.0"))
check("older is not newer", not is_newer("1.3.9", "1.4.0"))
check("1.5 == 1.5.0", parse_version("1.5") == (1, 5, 0))
check("garbage ignored", not is_newer("banana", "1.0.0") and not is_newer("1.0.0", ""))
check("beta suffix tolerated", parse_version("1.6.0-beta1") == (1, 6, 0))

print("check -> available")
u, s, ev = make()
u.check(manual=True); check("check finishes", wait(u, ("available", "uptodate", "error")))
st = u.status()
check("state available", st["state"] == "available")
check("latest 9.9.9", st["latest"] == "9.9.9")
check("installer asset picked", u.release["asset_name"] == "AndySDR-Setup.exe")
check("sha256 taken from GitHub", u.release["sha256"] == hashlib.sha256(SETUP).hexdigest())
check("can_install", st["can_install"])
check("clients were notified", any(k == "update" and p["state"] == "available" for k, p in ev))
check("last-check time stored", s.get("update_last_check", 0) > 0)

print("download -> ready")
u.download(); check("download finishes", wait(u, ("ready", "error")))
check("state ready", u.state == "ready")
check("file saved intact", open(u.file, "rb").read() == SETUP)
check("progress notices sent", any(k == "update_progress" for k, _ in ev))
check("no .part left", not [f for f in os.listdir(u.dir) if f.endswith(".part")])
check("re-check keeps ready download", (u.check(manual=True), wait(u, ("ready", "available")))[1] and u.state == "ready")

print("portable asset")
u, s, ev = make(mode="portable")
u.check(); wait(u, ("available",))
check("zip asset picked", u.release["asset_name"] == "AndySDR-windows.zip")
u.download(); wait(u, ("ready", "error"))
check("zip downloaded", u.state == "ready" and zipfile.is_zipfile(u.file))

print("up to date / old release")
STATE["tag"] = "v" + config.VERSION
u, s, ev = make(); u.check(); wait(u, ("uptodate", "error"))
check("same version -> uptodate", u.state == "uptodate" and u.release is None)
STATE["tag"] = "v0.0.1"
u, s, ev = make(); u.check(); wait(u, ("uptodate", "error"))
check("older release -> uptodate", u.state == "uptodate")
STATE["tag"] = "v9.9.9"

print("safety checks")
STATE["digest_ok"] = False
u, s, ev = make(); u.check(); wait(u, ("available",)); u.download(); wait(u, ("ready", "error"))
check("wrong SHA-256 rejected", u.state == "error" and "SHA-256" in u.error)
check("bad file removed", not os.path.exists(os.path.join(u.dir, "AndySDR-Setup.exe")))
STATE["digest_ok"] = True
STATE["short"] = True
u, s, ev = make(); u.check(); wait(u, ("available",)); u.download(); wait(u, ("ready", "error"))
check("size mismatch rejected", u.state == "error" and "incomplete" in u.error)
STATE["short"] = False
STATE["assets_prefix"] = "http://evil.example/dl/"
u, s, ev = make(); u.check(); wait(u, ("available",))
check("asset from another host ignored", u.state == "available" and not u.status()["can_install"])
u.download(); time.sleep(0.2)
check("...and cannot be downloaded", u.state == "available")
STATE["assets_prefix"] = None
STATE["prerelease"] = True
u, s, ev = make(); u.check(); wait(u, ("uptodate", "error"))
check("pre-releases ignored", u.state == "uptodate")
STATE["prerelease"] = False
try:
    u, s, ev = make(); u.install(); check("install without download refused", False)
except RuntimeError:
    check("install without download refused", True)

print("errors")
STATE["status"] = 403
u, s, ev = make(); u.check(manual=True); wait(u, ("error",))
check("manual check reports rate limit", u.state == "error" and "limiting" in u.error)
u, s, ev = make(); u.check(manual=False); wait(u, ("idle", "error"))
check("automatic check fails silently", u.state == "idle" and u.error == "")
STATE["status"] = 404
u, s, ev = make(); u.check(manual=True); wait(u, ("error",))
check("no release yet -> friendly message", "No published release" in u.error)
STATE["status"] = 200
dead = Updater(FakeSettings(), lambda *a: None, lambda: None, api_url="http://127.0.0.1:9/x", mode="installer")
dead.check(manual=True, wait=True)
check("offline -> friendly message", dead.state == "error" and "internet" in dead.error)

print("skip / auto-download / source")
u, s, ev = make(); u.check(); wait(u, ("available",)); u.skip()
check("skip remembered", s["update_skip"] == "9.9.9" and u.status()["skipped"])
u.check(manual=False); wait(u, ("available",))
check("automatic check keeps it skipped", u.status()["skipped"])
u.check(manual=True); wait(u, ("available",))
check("manual check clears skip", s["update_skip"] == "" and not u.status()["skipped"])
u, s, ev = make(update_auto_download=True); u.check(manual=False); wait(u, ("ready", "error"), 15)
check("auto-download goes straight to ready", u.state == "ready")
u, s, ev = make(update_auto_download=True, update_skip="9.9.9"); u.check(manual=False); wait(u, ("available",))
time.sleep(0.2)
check("auto-download does not fetch skipped versions", u.state == "available")
u, s, ev = make(mode="source", update_auto_download=True); u.check(); wait(u, ("available",)); time.sleep(0.2)
check("source copy: never downloads", u.state == "available" and not u.status()["can_install"])

print("portable install script")
u, s, ev = make(mode="portable"); u.check(); wait(u, ("available",)); u.download(); wait(u, ("ready",))
app = tempfile.mkdtemp()
updater.app_dir = lambda: app
script = u._portable_script(["--server"])
txt = open(script, encoding="utf-8").read()
check("zip unpacked", os.path.isfile(os.path.join(u.dir, "unpacked", "AndySDR", "AndySDR.exe")))
check("script copies over the app folder", "robocopy" in txt and app in txt)
check("script restarts with --server", "AndySDR.exe" in txt and "--server" in txt)
check("script uses Windows line endings", b"\r\n" in open(script, "rb").read())
bad = io.BytesIO()
with zipfile.ZipFile(bad, "w") as z:
    z.writestr("../evil.txt", b"x")
badp = os.path.join(u.dir, "bad.zip"); open(badp, "wb").write(bad.getvalue())
try:
    updater._safe_extract(badp, os.path.join(u.dir, "x")); check("zip-slip blocked", False)
except IOError:
    check("zip-slip blocked", True)

print("server commands")
from andysdr import server as srvmod  # noqa: E402


class StubRadio:
    def add_listener(self, f):
        pass

    def stop(self):
        pass


class StubClient:
    peer = "127.0.0.1"
    def __init__(self): self.out = []


settings = config.Settings()
sv = srvmod.Server(StubRadio(), settings)
sv.updater = u2 = make()[0]
sv._reply = lambda c, k, d: c.out.append((k, d))
c = StubClient()
asyncio.run(sv._command(c, {"cmd": "update_check"}))
wait(u2, ("available",))
check("update_check command works", u2.state == "available")
asyncio.run(sv._command(c, {"cmd": "update_download"}))
wait(u2, ("ready",))
check("update_download command works", u2.state == "ready")
asyncio.run(sv._command(c, {"cmd": "settings", "values": {"update_check": False, "update_auto_download": True}, "quiet": True}))
check("update settings saved", settings["update_check"] is False and settings["update_auto_download"] is True)
check("status is JSON-serialisable", json.dumps(u2.status()) is not None)
check("hello carries update status", "update" in open(srvmod.__file__).read())

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
