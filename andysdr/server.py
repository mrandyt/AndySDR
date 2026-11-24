"""AndySDR network server: serves the GUI and streams spectrum + audio over WebSockets.

Pure Python standard library (asyncio) – no web framework needed.
Any PC/laptop/tablet on the network opens  http://<this-pc>:8045  and enters the password.
"""
import asyncio
import base64
import hashlib
import ctypes
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

from . import capturemeta
from .alerts import Alerts, KINDS as ALERT_KINDS, clean_watch_list
from . import diagnose
from . import repeaters
from .dsp import BANDWIDTHS
from .config import VERSION, data_dir, resource_dir
from .updater import Updater

log = logging.getLogger("andysdr.server")
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOCAL = {"127.0.0.1", "::1", "localhost"}


def lan_addresses():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


class Client:
    def __init__(self, reader, writer, peer):
        self.reader, self.writer, self.peer = reader, writer, peer
        self.q = asyncio.Queue(maxsize=120)
        self.authed = False
        self.alive = True
        self.audio = True

    def push(self, item, droppable=True):
        if not self.authed or not self.alive:
            return
        if self.q.full():
            if droppable:
                return
            try:
                self.q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.q.put_nowait(item)


def downloads_dir():
    """The user's Downloads folder (follows Windows' own setting, e.g. when moved to OneDrive)."""
    if sys.platform.startswith("win"):
        try:
            class _GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
            g = _GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            buf = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(buf)) == 0 and buf.value:
                path = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                return path
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Downloads")


def _diag_extra(radio=None):
    """Extra files for the diagnostics zip: what the decoders printed recently (it never reaches andysdr.log)."""
    try:
        from .decoders import RECENT, RECENT_DSD
        out = {"decoder-recent.txt": "\n".join(RECENT), "decoder-dsd.txt": "\n".join(RECENT_DSD)}
        if radio is not None:
            out["satellite-passes.jsonl"] = radio.passlog.text()
            if getattr(radio, "satrx", None) is not None:
                out["satellite-passes-rx2.jsonl"] = radio.satrx.passlog.text()
        return out
    except Exception:              # noqa: BLE001 - the zip must still be made
        return {}


class Server:
    def __init__(self, radio, settings):
        self.radio = radio
        self.s = settings
        self.clients = set()
        self.loop = None
        self.web = os.path.join(resource_dir(), "web")
        self.images = os.path.join(data_dir(), "images")
        self.fail_times = {}
        radio.add_listener(self._from_radio)
        self.updater = Updater(settings, self._from_radio, shutdown=radio.stop)
        self.alerts = Alerts(settings, on_add=self._alert_added)
        self._last_check = None

    # ---------------------------------------------------------------- radio -> clients
    def _from_radio(self, kind, payload):
        try:
            self.alerts.feed(kind, payload)       # the few events worth a notification on the tablet app
        except Exception:
            log.exception("alert feed")
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._broadcast, kind, payload)
        except RuntimeError:                 # the server loop has stopped: stop sending (and stop flooding the log)
            self.loop = None

    def _alert_added(self, item):
        """A watch-list hit also pops up on every PC / tablet screen that has AndySDR open (the rest are for the phone app)."""
        if item.get("kind") != "watch" or self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._broadcast, "watch_alert", item)
        except RuntimeError:
            self.loop = None

    def _broadcast(self, kind, payload):
        if kind == "spectrum":
            hdr = struct.pack("<Bddd", 1, payload["center"], payload["fs"], payload["freq"])
            frame = _ws_frame(hdr + payload["data"], 2)
            for c in self.clients:
                c.push(frame)
        elif kind == "satspec":
            hdr = struct.pack("<Bddd", 3, payload["center"], payload["fs"], payload["freq"])
            frame = _ws_frame(hdr + payload["data"], 2)
            for c in self.clients:
                c.push(frame, droppable=True)
        elif kind == "audio":
            frame = _ws_frame(b"\x02" + payload, 2)
            for c in self.clients:
                if c.audio:
                    c.push(frame)
        else:
            if kind == "state":
                payload = dict(payload, listeners=len(self.clients))
            frame = _ws_frame(json.dumps({"type": kind, "data": payload}).encode(), 1)
            for c in self.clients:
                c.push(frame, droppable=(kind in ("state", "adsb", "meshsdr", "update_progress", "bias", "gps", "datalink_status", "satrx_live")))

    # ---------------------------------------------------------------- HTTP
    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
        except Exception:
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            writer.close()
            return
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        url = urllib.parse.urlparse(target)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)

        if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            await self._websocket(reader, writer, headers, ip)
            return
        if path == "/andysdr.json":
            # who-am-I card for the Android app's "Find my radio" scan: name, version and port only
            # (nothing secret, so it needs no password; the radio itself stays behind the password)
            info = {"app": "AndySDR", "version": VERSION, "host": socket.gethostname(),
                    "port": int(self.s["server_port"])}
            await self._send(writer, 200, json.dumps(info).encode(), "application/json",
                             extra={"Cache-Control": "no-store"})
            return
        if path.startswith(("/images/", "/recordings/", "/api/")):
            if not self._token_ok(ip, (query.get("token") or [""])[0]):
                await self._send(writer, 403, b"Password required", "text/plain")
                return
        if path == "/api/alerts":                     # polled by the Android app's background service
            try:
                since = int((query.get("since") or ["-1"])[0])
            except ValueError:
                since = -1
            await self._send(writer, 200, json.dumps(self.alerts.since(since)).encode(), "application/json",
                             extra={"Cache-Control": "no-store"})
            return
        if path == "/api/replay.wav":
            try:
                secs = float((query.get("secs") or ["30"])[0])
            except ValueError:
                secs = 30
            data = self.radio.replay_wav(secs)
            if not data:
                await self._send(writer, 404, b"Nothing heard in that time", "text/plain")
            else:
                await self._send(writer, 200, data, "audio/wav", extra={"Cache-Control": "no-store"})
            return
        if path == "/api/export.csv":
            data = self.radio.memory.export_csv().encode("utf-8-sig")
            await self._send(writer, 200, data, "text/csv",
                             extra={"Content-Disposition": 'attachment; filename="andysdr_memory.csv"'})
            return
        if path == "/api/pager.csv":
            data = self.radio.pager.export_csv().encode("utf-8-sig")
            fname = time.strftime("andysdr_pager_%Y%m%d_%H%M.csv")
            await self._send(writer, 200, data, "text/csv",
                             extra={"Content-Disposition": f'attachment; filename="{fname}"'})
            return
        if path == "/api/heard.csv":
            data = self.radio.lastheard.export_csv().encode("utf-8-sig")
            fname = time.strftime("andysdr_lastheard_%Y%m%d_%H%M.csv")
            await self._send(writer, 200, data, "text/csv",
                             extra={"Content-Disposition": f'attachment; filename="{fname}"'})
            return
        if path == "/api/ism.csv":
            data = self.radio.ism.export_csv().encode("utf-8-sig")
            fname = time.strftime("andysdr_sensors_%Y%m%d_%H%M.csv")
            await self._send(writer, 200, data, "text/csv",
                             extra={"Content-Disposition": f'attachment; filename="{fname}"'})
            return
        if path in ("/api/acars.csv", "/api/sondes.csv"):
            which = "acars" if "acars" in path else "sondes"
            data = self.radio.datalink.export_csv(which).encode("utf-8-sig")
            fname = time.strftime(f"andysdr_{which}_%Y%m%d_%H%M.csv")
            await self._send(writer, 200, data, "text/csv",
                             extra={"Content-Disposition": f'attachment; filename="{fname}"'})
            return
        if path == "/api/diagnostics.zip":
            rep = self._last_check if (self._last_check and time.time() - self._last_check["when"] < 120) else None
            if rep is None:
                rep = await asyncio.get_running_loop().run_in_executor(None, self._run_checks)
            data = await asyncio.get_running_loop().run_in_executor(
                None, lambda: diagnose.build_zip(rep, self.s.data, extra=_diag_extra(self.radio)))
            fname = time.strftime("AndySDR-diagnostics-%Y%m%d-%H%M.zip")
            await self._send(writer, 200, data, "application/zip",
                             extra={"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})
            return
        if path == "/api/adsb.csv":
            data = self.radio.adsb.tracker.export_csv(time.time()).encode("utf-8-sig")
            fname = time.strftime("andysdr_aircraft_%Y%m%d_%H%M.csv")
            await self._send(writer, 200, data, "text/csv",
                             extra={"Content-Disposition": f'attachment; filename="{fname}"'})
            return
        if path == "/api/aircraft.json":              # same layout as dump1090 / readsb
            data = json.dumps(self.radio.adsb.tracker.aircraft_json(time.time())).encode()
            await self._send(writer, 200, data, "application/json")
            return
        if path.startswith("/images/"):
            await self._file(writer, self.images, path[len("/images/"):])
            return
        if path.startswith("/recordings/"):
            await self._stream_file(writer, os.path.join(data_dir(), "recordings"), path[len("/recordings/"):])
            return
        if path in ("/", ""):
            path = "/index.html"
        await self._file(writer, self.web, path.lstrip("/"))

    async def _file(self, writer, root, rel):
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(os.path.realpath(root)) or not os.path.isfile(full):
            await self._send(writer, 404, b"Not found", "text/plain")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "application/javascript"
        with open(full, "rb") as f:
            data = f.read()
        await self._send(writer, 200, data, ctype)

    async def _stream_file(self, writer, root, rel):
        """Send a (possibly large) file in pieces - recordings can be hundreds of MB."""
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(full):
            await self._send(writer, 404, b"Not found", "text/plain")
            return
        size = os.path.getsize(full)
        ctype = "audio/wav" if full.lower().endswith(".wav") else (mimetypes.guess_type(full)[0] or "application/octet-stream")
        head = (f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {size}\r\n"
                "Cache-Control: no-cache\r\nConnection: close\r\n\r\n").encode()
        try:
            writer.write(head)
            left = size
            with open(full, "rb") as f:
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    writer.write(chunk)
                    await writer.drain()
        except Exception:
            pass
        writer.close()

    async def _send(self, writer, code, body, ctype, extra=None):
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found"}.get(code, "OK")
        h = [f"HTTP/1.1 {code} {reason}", f"Content-Type: {ctype}", f"Content-Length: {len(body)}",
             "Cache-Control: no-cache", "Connection: close"]
        for k, v in (extra or {}).items():
            h.append(f"{k}: {v}")
        writer.write(("\r\n".join(h) + "\r\n\r\n").encode() + body)
        try:
            await writer.drain()
        except Exception:
            pass
        writer.close()

    def _is_local(self, ip):
        return ip in LOCAL or ip.startswith("127.") or ip == "::ffff:127.0.0.1"

    def _token_ok(self, ip, token):
        if self._is_local(ip) and not self.s["require_password_local"]:
            return True
        return hmac.compare_digest(str(token or ""), str(self.s["password"]))

    # ---------------------------------------------------------------- WebSocket
    async def _websocket(self, reader, writer, headers, ip):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        await writer.drain()
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        c = Client(reader, writer, ip)
        self.clients.add(c)
        sender = asyncio.ensure_future(self._sender(c))
        try:
            if self._token_ok(ip, None):
                await self._welcome(c)
            else:
                writer.write(_ws_frame(json.dumps({"type": "auth_required"}).encode(), 1))
                await writer.drain()
            while c.alive:
                op, data = await _ws_read(reader)
                if op is None or op == 8:
                    break
                if op == 9:
                    writer.write(_ws_frame(data, 10))
                    continue
                if op != 1:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                if not c.authed:
                    if msg.get("cmd") == "auth":
                        last = self.fail_times.get(ip, 0)
                        if time.time() - last < 2:
                            await asyncio.sleep(2)
                        if self._token_ok(ip, msg.get("password")):
                            await self._welcome(c, token=msg.get("password"))
                        else:
                            self.fail_times[ip] = time.time()
                            writer.write(_ws_frame(json.dumps({"type": "auth_failed"}).encode(), 1))
                            await writer.drain()
                    continue
                await self._command(c, msg)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("websocket")
        finally:
            c.alive = False
            self.clients.discard(c)
            sender.cancel()
            try:
                writer.close()
            except Exception:
                pass
            self._broadcast("state", self.radio.state())

    async def _sender(self, c):
        try:
            while c.alive:
                frame = await c.q.get()
                c.writer.write(frame)
                await c.writer.drain()
        except Exception:
            c.alive = False

    async def _welcome(self, c, token=None):
        c.authed = True
        r = self.radio
        hello = {
            "version": VERSION, "local": self._is_local(c.peer), "token": token or "",
            "state": r.state(), "memory": r.memory.all(),
            "devices": [{"key": d.key, "label": d.label, "kind": d.kind} for d in r.dev_list],
            "device": r.device_info(), "bias": r.bias_snapshot(), "gps": r.gps.snapshot(with_ports=True), "decoders": r.decoders.available(),
            "activity": sorted(r.activity.values(), key=lambda x: -x["count"])[:300],
            "images": self._images(),
            "schedule": r.scheduler.status(),
            "mesh": r.mesh.snapshot(), "mesh_history": r.mesh.history[-1500:],
            "meshsdr": r.meshsdr.snapshot(),
            "ism": r.ism.snapshot(),
            "pager": r.pager.snapshot(),
            "datalink": r.datalink.snapshot(),
            "lastheard": r.lastheard.snapshot(), "dmrdb": r.dmrdb.status(), "repeaters": r.repeaters.status(),
            "adsb": r.adsb.snapshot(),
            "ft8": r.decoders.ft8_history[-300:],
            "morse": {"text": r.decoders.morse_text[-2000:], "status": r.decoders.morse_status},
            "bandwidths": {k: [[hz, lab] for hz, lab in v] for k, v in BANDWIDTHS.items()},
            "update": self.updater.status(),
            "settings": {k: self.s[k] for k in ("scan_delay", "scan_dwell", "search_threshold_db",
                                                 "server_port", "require_password_local",
                                                 "deemphasis_us", "theme",
                                                 "update_check", "update_auto_download", "hidden_buttons",
                                                 "alert_pager", "alert_emergency", "alert_priority", "alert_capture", "alert_watch",
                                                 "watch_list", "dmr_db_auto")},
            "server": {"addresses": lan_addresses(), "port": self.s["server_port"],
                       "password": self.s["password"] if self._is_local(c.peer) else "",
                       "host": socket.gethostname()},
        }
        c.push(_ws_frame(json.dumps({"type": "hello", "data": hello}).encode(), 1), droppable=False)

    def _images(self):
        """Newest 200 pictures, each with satellite / station name, frequency and time."""
        out = []
        for root, _, files in os.walk(self.images):
            for f in files:
                if f.lower().endswith((".png", ".jpg")):
                    full = os.path.join(root, f)
                    out.append((os.path.getmtime(full), os.path.relpath(full, self.images).replace("\\", "/"),
                                os.path.getsize(full)))
        ents = capturemeta.entries()
        return [capturemeta.describe(rel, mt, sz, ents) for mt, rel, sz in sorted(out, reverse=True)[:200]]

    def _recordings(self):
        """Sound recordings (and Meteor IQ recordings), newest first."""
        folder = os.path.join(data_dir(), "recordings")
        out = []
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        for f in names:
            full = os.path.join(folder, f)
            if f.lower().endswith(".wav") and os.path.isfile(full):
                out.append((os.path.getmtime(full), f, os.path.getsize(full)))
        ents = capturemeta.entries()
        res = []
        for mt, f, sz in sorted(out, reverse=True)[:200]:
            d = capturemeta.describe(f, mt, sz, ents)
            d["iq"] = f.startswith("lrpt_")          # raw 240 kS/s baseband for SatDump - not listenable
            if d["iq"]:
                d["name"] += " (raw IQ)"
            res.append(d)
        return res

    def _delete_capture(self, c, kind, rel):
        """Delete one picture or recording (the person pressed Delete and confirmed)."""
        root = {"images": self.images, "recordings": os.path.join(data_dir(), "recordings")}.get(kind)
        if not root:
            return
        src = os.path.realpath(os.path.join(root, str(rel or "")))
        if not src.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(src):
            self._reply(c, "event", {"level": "warn", "text": "That file no longer exists"})
        else:
            try:
                os.remove(src)
                self._reply(c, "event", {"level": "info", "text": f"Deleted {os.path.basename(src)}"})
            except OSError as e:
                self._reply(c, "event", {"level": "warn", "text": f"Could not delete: {e}"})
        if kind == "images":
            self._reply(c, "images", self._images())
        else:
            self._reply(c, "recordings", self._recordings())

    def _save_download(self, c, kind, rel, name):
        """Copy a picture / recording into the Downloads folder of the PC AndySDR runs on (the desktop window
        cannot do browser downloads; remote browsers use their own normal download instead)."""
        root = {"images": self.images, "recordings": os.path.join(data_dir(), "recordings")}.get(kind)
        if not root or not self._is_local(c.peer):
            return
        src = os.path.realpath(os.path.join(root, str(rel or "")))
        if not src.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(src):
            self._reply(c, "event", {"level": "warn", "text": "That file no longer exists"})
            return
        fname = re.sub(r'[\\/:*?"<>|]+', "_", os.path.basename(str(name or "")) or os.path.basename(src))
        dest_dir = downloads_dir()

        def run():
            try:
                os.makedirs(dest_dir, exist_ok=True)
                base, ext = os.path.splitext(fname)
                dest, n = os.path.join(dest_dir, fname), 1
                while os.path.exists(dest):
                    n += 1
                    dest = os.path.join(dest_dir, f"{base} ({n}){ext}")
                shutil.copy2(src, dest)
                text, lvl = f"Saved to your Downloads folder: {os.path.basename(dest)}", "info"
            except Exception as e:
                text, lvl = f"Could not save the file: {e}", "warn"
            self.loop.call_soon_threadsafe(self._reply, c, "event", {"level": lvl, "text": text})
        threading.Thread(target=run, name="save-download", daemon=True).start()

    def _open_folder(self, c, which):
        """Open the images or recordings folder in the file manager - only on the radio PC itself."""
        sub = {"images": "images", "recordings": "recordings"}.get(which)
        if not sub:
            return
        folder = os.path.join(data_dir(), sub)
        os.makedirs(folder, exist_ok=True)
        if not self._is_local(c.peer):
            if sub == "recordings":
                self._reply(c, "event", {"level": "info",
                            "text": "Recordings are saved on the radio PC in " + folder})
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            self._reply(c, "event", {"level": "warn", "text": f"Could not open {folder}: {e}"})

    def _run_checks(self):
        rep = diagnose.run_checks(self.radio, self.s, lan_addresses(), upd=self.updater, dmr=self.radio.dmrdb)
        self._last_check = rep
        return rep

    def _reply(self, c, kind, data):
        c.push(_ws_frame(json.dumps({"type": kind, "data": data}).encode(), 1), droppable=False)

    async def _command(self, c, m):
        r = self.radio
        cmd = m.get("cmd")
        try:
            if cmd == "tune":
                r.set_frequency(int(m["freq"]), m.get("mode"))
            elif cmd == "step":
                r.step(int(m.get("dir", 1)))
            elif cmd == "mode":
                r.set_mode(m["mode"])
            elif cmd == "bandwidth":
                r.set_bandwidth(int(m.get("bw") or 0), bool(m.get("auto")))
            elif cmd == "gain":
                r.set_gain(float(m["value"]))
            elif cmd == "agc":
                r.set_agc(bool(m["on"]))
            elif cmd == "squelch":
                r.set_squelch(float(m["value"]))
            elif cmd == "stepsize":
                r.set_step(int(m["hz"]))
            elif cmd == "scan":
                r.start_scan()
            elif cmd == "search":
                r.start_search(m.get("start"), m.get("stop"), m.get("step"), m.get("mode"),
                               m.get("search_mode"))
            elif cmd == "manual":
                r.stop_scan()
            elif cmd == "recall":
                r.recall(m["bank"], m["ch"])
            elif cmd == "store":
                r.store(int(m["bank"]), m.get("ch"), m.get("name", ""))
            elif cmd == "lockout":
                r.lockout()
            elif cmd == "notch":
                r.set_notch(m.get("on"), m.get("hz"), m.get("q"), m.get("auto"))
            elif cmd == "hotspot_save":
                r.set_hotspot(m.get("name"), m.get("freq"), m.get("cc"), m.get("ts"), m.get("tg"))
            elif cmd == "skip":
                r.skip()
            elif cmd == "lockout_temp":
                r.lockout_temp(float(m.get("minutes", 30)))
            elif cmd == "lockout_clear":
                r.lockout_clear()
            elif cmd == "priority":
                r.set_priority(m.get("bank"), m.get("ch"))
            elif cmd == "record":
                r.record(bool(m["on"]))
            elif cmd == "decoder":
                r.set_decoder(m.get("id", ""), m.get("sub", "auto"))
            elif cmd == "device":
                r.set_device(m["key"])
            elif cmd == "gps_set":
                r.gps.configure(enabled=m.get("enabled"), port=m.get("port"), baud=m.get("baud"), hours=m.get("hours"))
            elif cmd == "gps_ports":
                r._emit("gps", r.gps.snapshot(with_ports=True))
            elif cmd == "bias_test":
                r.bias_test()
            elif cmd == "device_setting":
                r.device_setting(m["key"], m["value"])
            elif cmd == "refresh_devices":
                devs = await asyncio.get_running_loop().run_in_executor(None, r.refresh_devices)
                self._reply(c, "devices", devs)
            elif cmd == "add_rtltcp":
                lst = list(self.s.get("rtl_tcp_servers", []))
                if m["addr"] not in lst:
                    lst.append(m["addr"])
                self.s["rtl_tcp_servers"] = lst
                self.s.save()
                devs = await asyncio.get_running_loop().run_in_executor(None, r.refresh_devices)
                self._reply(c, "devices", devs)
            elif cmd == "mem_update":
                r.memory.update(m["bank"], m["ch"], m["fields"])
                self._broadcast("memory", r.memory.all())
            elif cmd == "mem_delete":
                r.memory.delete(m["bank"], m["ch"])
                self._broadcast("memory", r.memory.all())
            elif cmd == "mem_bank":
                r.memory.set_bank(m["bank"], m["fields"])
                self._broadcast("memory", r.memory.all())
            elif cmd == "mem_clear":
                r.memory.clear_bank(m["bank"])
                self._broadcast("memory", r.memory.all())
            elif cmd == "import_csv":
                from .memory import parse_radioreference_csv
                res = parse_radioreference_csv(m["text"])
                added, overflow = r.memory.import_channels(int(m.get("bank", 1)), res["channels"],
                                                           bool(m.get("replace")), m.get("bank_name"), m.get("banks", 1),
                                                           {"file": m.get("filename"), "file_date": m.get("file_date")} if m.get("filename") else None)
                if res["talkgroups"]:
                    r.memory.import_talkgroups(res["talkgroups"])
                self._broadcast("memory", r.memory.all())
                self._reply(c, "import_result", {"added": added, "overflow": overflow, "file": m.get("filename") or "",
                                                 "skipped": res["skipped"],
                                                 "talkgroups": len(res["talkgroups"])})
            elif cmd == "clear_activity":
                r.clear_activity()
            elif cmd == "audio":
                c.audio = bool(m.get("on", True))
            elif cmd == "alert_test":
                self.alerts.add("test", "Test alert from AndySDR", time.strftime("Sent at %H:%M:%S – if you can read this on the tablet, alerts work."))
                self._reply(c, "event", {"level": "info", "text": "Test alert sent – the tablet app shows it within a few seconds"})
            elif cmd == "hidden_buttons":
                # CUSTOMISE: the buttons hidden on the front panel are kept here so every screen (PC, tablet, phone) matches
                ids = [str(x)[:80] for x in (m.get("ids") or []) if isinstance(x, str)][:300]
                self.s["hidden_buttons"] = ids
                self.s.save()
                self._broadcast("hidden_buttons", ids)
            elif cmd == "settings":
                allowed = {"scan_delay": float, "scan_dwell": float, "search_threshold_db": float,
                           "require_password_local": bool, "deemphasis_us": int, "theme": str,
                           "password": str, "mute_digital_audio": bool, "auto_gain": bool, "dsd_gate": bool, "update_check": bool, "update_auto_download": bool,
                           "alert_pager": bool, "alert_emergency": bool, "alert_priority": bool, "alert_capture": bool,
                           "alert_watch": bool, "watch_list": clean_watch_list,
                           "dmr_db_auto": bool}
                for k, v in m.get("values", {}).items():
                    if k in allowed:
                        if k == "password" and (not self._is_local(c.peer) or len(str(v)) < 4):
                            continue
                        self.s[k] = allowed[k](v)
                self.s.save()
                if not m.get("quiet"):
                    self._reply(c, "event", {"level": "info", "text": "Settings saved"})
            elif cmd == "replay_save":
                name = r.replay_save(m.get("secs") or 30)
                self._reply(c, "event", {"level": "info" if name else "warn",
                                         "text": f"Saved the last {int(m.get('secs') or 30)} s as a recording ({name})" if name else "Nothing was heard in that time"})
                if name:
                    self._reply(c, "recordings", self._recordings())
            elif cmd == "delete_capture":
                self._delete_capture(c, m.get("kind"), m.get("f"))
            elif cmd == "save_download":
                self._save_download(c, m.get("kind"), m.get("f"), m.get("name"))
            elif cmd == "open_folder":
                self._open_folder(c, m.get("which"))
            elif cmd == "images":
                self._reply(c, "images", self._images())
            elif cmd == "recordings":
                self._reply(c, "recordings", self._recordings())
            elif cmd == "decoders":
                self._reply(c, "decoders", r.decoders.available())
            elif cmd == "mesh_connect":
                r.mesh.connect(m.get("kind", "serial"), m.get("target", ""), m.get("pin", ""))
            elif cmd == "mesh_disconnect":
                r.mesh.disconnect(forget=True)
            elif cmd == "mesh_send":
                r.mesh.send(str(m.get("conv", "")), str(m.get("text", "")))
            elif cmd == "mesh_advert":
                r.mesh.advert(bool(m.get("flood", True)))
            elif cmd == "mesh_refresh":
                r.mesh.refresh()
            elif cmd == "mesh_name":
                r.mesh.set_name(str(m.get("name", "")).strip())
            elif cmd == "mesh_radio":
                r.mesh.set_radio(m["freq"], m["bw"], m["sf"], m["cr"])
            elif cmd == "mesh_channel_add":
                r.mesh.add_channel(int(m.get("idx", 1)), str(m.get("name", "")), str(m.get("secret", "")))
            elif cmd == "mesh_channel_remove":
                r.mesh.remove_channel(int(m["idx"]))
            elif cmd == "mesh_clear":
                r.mesh.clear_history(m.get("conv"))
            elif cmd == "mesh_ble_scan":
                r.mesh.scan_ble()
            elif cmd == "fetch_decoders":
                r.decoders.download_missing(
                    done=lambda: self._from_radio("decoders", r.decoders.available()))
            elif cmd == "pager_clear":
                r.pager.clear()
            elif cmd == "diag_save":
                if not self._is_local(c.peer):
                    return
                def save_diag():
                    try:
                        rep = self._last_check if (self._last_check and time.time() - self._last_check["when"] < 120) else self._run_checks()
                        data = diagnose.build_zip(rep, self.s.data, extra=_diag_extra(r))
                        dest_dir = downloads_dir()
                        os.makedirs(dest_dir, exist_ok=True)
                        dest = os.path.join(dest_dir, time.strftime("AndySDR-diagnostics-%Y%m%d-%H%M.zip"))
                        with open(dest, "wb") as f:
                            f.write(data)
                        text, lvl = f"Saved to your Downloads folder: {os.path.basename(dest)}", "info"
                    except Exception as e:      # noqa: BLE001
                        text, lvl = f"Could not save the diagnostics: {e}", "warn"
                    self.loop.call_soon_threadsafe(self._reply, c, "event", {"level": lvl, "text": text})
                threading.Thread(target=save_diag, name="diag-save", daemon=True).start()
            elif cmd == "check_system":
                def run_check():
                    try:
                        rep = self._run_checks()
                    except Exception as e:      # noqa: BLE001
                        rep = {"items": [diagnose.item("err", "info", "The check could not run", str(e)[:200])],
                               "summary": {"ok": 0, "warn": 0, "bad": 0, "info": 1}, "when": time.time(), "version": VERSION}
                    self.loop.call_soon_threadsafe(self._reply, c, "check_result", rep)
                threading.Thread(target=run_check, name="check-system", daemon=True).start()
            elif cmd == "rep_marks":
                obs = r.scheduler.sats.observer()
                try:
                    km = float(m.get("max_km") or 0)
                except (TypeError, ValueError):
                    km = 0
                if r.repeaters.due():
                    r.repeaters.refresh()
                self._reply(c, "rep_marks", {"marks": r.repeaters.marks(km, obs[0] if obs else None, obs[1] if obs else None),
                                             "status": r.repeaters.status(), "pos": bool(obs), "km": km})
            elif cmd in ("rep_query", "rep_store_all", "rep_scan"):
                obs = r.scheduler.sats.observer()
                lat, lon = (obs[0], obs[1]) if obs else (None, None)
                try:
                    km = float(m.get("max_km") or 0)
                except (TypeError, ValueError):
                    km = 0
                if cmd == "rep_query":
                    rows, total = r.repeaters.query(m.get("text", ""), m.get("band", ""), m.get("mode", ""), km, lat, lon,
                                                    limit=int(m.get("limit") or 400))
                    if not r.repeaters.rows and r.repeaters.due():
                        r.repeaters.refresh()
                    elif r.repeaters.rows and r.repeaters.due():
                        r.repeaters.refresh()                    # stale: refresh quietly, keep showing the old list
                    self._reply(c, "repeaters", {"rows": rows, "total": total, "status": r.repeaters.status(),
                                                 "bands": r.repeaters.bands(), "pos": [lat, lon] if obs else None})
                else:
                    rows, total = r.repeaters.query(m.get("text", ""), m.get("band", ""), m.get("mode", ""), km, lat, lon, limit=100)
                    chans = []
                    for x in rows:
                        digital = [k for k in x["modes"] if k != "FM"]
                        use = "FM" if "FM" in x["modes"] else digital[0]
                        chans.append({"freq": int(round(x["out"] * 1e6)), "mode": "NFM" if use == "FM" else "DIG",
                                      "name": repeaters.memory_name(x), "decoder": "dsd" if use != "FM" else "",
                                      "tone": f"{x['tone']:g}" if x.get("tone") else ""})
                    if cmd == "rep_scan" and not obs:
                        self._reply(c, "event", {"level": "warn", "text": "Set your position first so AndySDR knows which repeaters are nearest"})
                    elif not chans:
                        self._reply(c, "event", {"level": "warn", "text": "Nothing in the list to store"})
                    elif cmd == "rep_scan":
                        r.store_repeaters(int(m.get("bank") or 0), chans, replace=True, bank_name="NEAR REPEATERS", scan=True)
                    else:
                        r.store_repeaters(int(m.get("bank") or 0), chans)
            elif cmd == "rep_refresh":
                started = r.repeaters.refresh()
                self._reply(c, "event", {"level": "info", "text": "Downloading the UK repeater list ..." if started
                                         else "A download is already running"})
            elif cmd == "rep_store":
                r.store_repeater(int(m.get("bank") or 0), int(round(float(m["freq"]) * 1e6)), m.get("mode") or "NFM",
                                 str(m.get("name", ""))[:16], str(m.get("tone") or ""), str(m.get("decoder") or ""))
            elif cmd == "heard_clear":
                r.lastheard.clear()
            elif cmd == "dmrdb_refresh":
                started = r.dmrdb.refresh(force=True)
                self._reply(c, "event", {"level": "info", "text": "Downloading the callsign list ..." if started
                                         else "A download is already running"})
            elif cmd == "datalink_clear":
                r.datalink.clear(m.get("which", "all"))
            elif cmd == "pager_alias":
                r.pager.set_alias(m["capcode"], m.get("name", ""))
            elif cmd == "pager_highlight":
                r.pager.set_highlight(m["capcode"], bool(m.get("on", True)))
            elif cmd == "ism_start":
                try:
                    r.start_ism(int(float(m.get("freq") or 0)))
                except (TypeError, ValueError):
                    r.start_ism()
            elif cmd == "ism_stop":
                r.stop_ism()
            elif cmd == "ism_clear":
                r.ism.clear()
            elif cmd == "meshsdr_start":
                if any(k in m for k in ("freq", "bw", "sf")):
                    r.meshsdr.set_cfg(freq=m.get("freq"), bw=m.get("bw"), sf=m.get("sf"))
                r.start_meshsdr()
            elif cmd == "meshsdr_stop":
                r.stop_meshsdr()
            elif cmd == "meshsdr_clear":
                r.meshsdr.clear()
            elif cmd == "meshsdr_channel_add":
                if not r.meshsdr.add_channel(str(m.get("spec", ""))):
                    self._reply(c, "event", {"level": "warn", "text": "Enter a #hashtag channel name or a 32-digit hex key"})
            elif cmd == "meshsdr_channel_remove":
                r.meshsdr.remove_channel(str(m.get("spec", "")))
            elif cmd == "adsb_start":
                r.start_adsb()
            elif cmd == "adsb_stop":
                r.stop_adsb()
            elif cmd == "adsb_clear":
                r.adsb.clear()
            elif cmd == "adsb_pos":
                r.adsb.set_position(m.get("lat"), m.get("lon"))
            elif cmd == "adsb_lookup":
                r.adsb.lookup(m.get("icao"))
            elif cmd == "adsb_share":
                r.adsb.set_share(bool(m.get("on")))
            elif cmd == "adsb_option":
                r.adsb.set_option(str(m.get("key", "")), m.get("value"))
            elif cmd == "update_check":
                self.updater.check(manual=True)
            elif cmd == "update_download":
                self.updater.download()
            elif cmd == "update_install":
                self.updater.install()
            elif cmd == "update_skip":
                self.updater.skip()
            elif cmd == "update_open":
                if self._is_local(c.peer):
                    self.updater.open_page()
            elif cmd == "auto_identify":
                r.set_auto_identify(bool(m.get("on", True)))
            elif cmd == "schedule":
                self._reply(c, "schedule", r.scheduler.status())
            elif cmd == "schedule_save":
                r.scheduler.save_job(m["job"])
            elif cmd == "schedule_delete":
                r.scheduler.delete_job(str(m["id"]))
            elif cmd == "sat_cfg":
                kw = {k: m[k] for k in ("enabled", "freq", "min_el", "from", "to", "days", "track", "gain_mode", "gain") if k in m}
                if "gain_mode" in kw and kw["gain_mode"] not in ("knob", "fixed", "auto"):
                    kw["gain_mode"] = "knob"
                if "gain" in kw:
                    try:
                        kw["gain"] = None if kw["gain"] in (None, "") else float(kw["gain"])
                    except (TypeError, ValueError):
                        kw["gain"] = None
                if "freq" in kw:
                    kw["freq"] = int(kw["freq"])
                if "min_el" in kw:
                    kw["min_el"] = max(0.0, min(80.0, float(kw["min_el"])))
                for b in ("enabled", "track"):
                    if b in kw:
                        kw[b] = bool(kw[b])
                r.scheduler.sats.set_cfg(str(m.get("id")), **kw)
                r.scheduler.sats.refresh_tles()
                r.scheduler._emit()
            elif cmd == "satrx_cfg":
                kw = {k: m[k] for k in ("enabled", "device", "gain", "gain_mode", "bias", "ppm") if k in m}
                try:
                    r.satrx.configure(**kw)
                except (TypeError, ValueError):
                    r._emit("event", {"level": "warn", "text": "Second receiver: a setting was not understood"})
                r.scheduler._emit()
            elif cmd == "satrx_check":
                lvl, text = await asyncio.get_running_loop().run_in_executor(None, r.satrx.check)
                self._reply(c, "event", {"level": lvl, "text": text, "rx2": True})
                r.scheduler._emit()
            elif cmd == "satrx_gain":
                try:
                    r.satrx.set_gain(m.get("gain"))
                except (TypeError, ValueError):
                    r._emit("event", {"level": "warn", "text": "Second receiver: that gain was not understood"})
                if m.get("final"):
                    r.scheduler._emit()
            elif cmd == "satrx_view":
                if m.get("on", True):
                    ok, text = await asyncio.get_running_loop().run_in_executor(None, lambda: r.satrx.begin_preview(m.get("freq"), m.get("bw")))
                    self._reply(c, "event", {"level": "info" if ok else "warn", "text": text, "rx2": True})
                else:
                    await asyncio.get_running_loop().run_in_executor(None, r.satrx.stop_preview)
                r.scheduler._emit()
            elif cmd == "satrx_refresh":
                await asyncio.get_running_loop().run_in_executor(None, lambda: r.satrx.candidates(True))
                r.scheduler._emit()
            elif cmd == "sat_observer":
                if r.scheduler.sats.set_observer(m.get("lat"), m.get("lon"), m.get("alt")):
                    r.scheduler.sats.refresh_tles()
                else:
                    r._emit("event", {"level": "warn", "text": "Satellites: latitude / longitude not understood"})
                r.scheduler._emit()
            elif cmd == "sat_refresh":
                r.scheduler.sats.refresh_tles(force=True)
            elif cmd == "schedule_run":
                r.scheduler.run_now(str(m["id"]))
            elif cmd == "schedule_stop":
                r.scheduler.stop_job()
            elif cmd == "mem_add_many":
                chans = [{"freq": int(x["freq"]), "mode": str(x.get("mode", "NFM")), "name": str(x.get("name", ""))[:32],
                          "tag": str(x.get("tag", ""))[:32], "decoder": str(x.get("decoder", ""))}
                         for x in m.get("channels", [])[:1000]]
                added, overflow = r.memory.import_channels(int(m.get("bank", 1)), chans, False, m.get("bank_name"))
                self._broadcast("memory", r.memory.all())
                self._reply(c, "event", {"level": "info", "text": f"Added {added} channels to bank {int(m.get('bank', 1))}"
                                         + (f" ({overflow} did not fit)" if overflow else "")})
        except Exception as e:
            log.exception("command %s", cmd)
            self._reply(c, "event", {"level": "error", "text": f"{cmd}: {e}"})

    # ---------------------------------------------------------------- run
    async def serve(self):
        self.loop = asyncio.get_running_loop()
        host = self.s["server_host"]
        port = int(self.s["server_port"])
        srv = await asyncio.start_server(self.handle, host, port, limit=8 * 1024 * 1024)
        log.info("AndySDR server on http://%s:%d  (LAN: %s)", host, port, lan_addresses())
        self.updater.start()
        self.radio.dmrdb.start()
        async with srv:
            await srv.serve_forever()


# ------------------------------------------------------------------- WebSocket framing
def _ws_frame(payload, opcode):
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return hdr + payload


async def _ws_read(reader):
    """Read one complete (possibly fragmented) message. Returns (opcode, bytes)."""
    chunks, first_op = [], None
    while True:
        b1, b2 = await reader.readexactly(2)
        fin, op = b1 & 0x80, b1 & 0x0F
        masked, n = b2 & 0x80, b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", await reader.readexactly(8))[0]
        if n > 32 * 1024 * 1024:
            return None, b""
        mask = await reader.readexactly(4) if masked else b"\x00\x00\x00\x00"
        data = bytearray(await reader.readexactly(n))
        if masked:
            # fast unmask
            m = (mask * (n // 4 + 1))[:n]
            data = bytes(a ^ b for a, b in zip(data, m)) if n < 4096 else \
                (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
        if op >= 8:            # control frame in the middle of a message
            return op, bytes(data)
        if first_op is None:
            first_op = op
        chunks.append(bytes(data))
        if fin:
            return first_op, b"".join(chunks)
