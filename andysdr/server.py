"""AndySDR network server: serves the GUI and streams spectrum + audio over WebSockets.

Pure Python standard library (asyncio) – no web framework needed.
Any PC/laptop/tablet on the network opens  http://<this-pc>:8045  and enters the password.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import socket
import struct
import time
import urllib.parse

from .config import VERSION, data_dir, resource_dir

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

    # ---------------------------------------------------------------- radio -> clients
    def _from_radio(self, kind, payload):
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._broadcast, kind, payload)

    def _broadcast(self, kind, payload):
        if kind == "spectrum":
            hdr = struct.pack("<Bddd", 1, payload["center"], payload["fs"], payload["freq"])
            frame = _ws_frame(hdr + payload["data"], 2)
            for c in self.clients:
                c.push(frame)
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
                c.push(frame, droppable=(kind == "state"))

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
        if path.startswith("/images/") or path.startswith("/api/"):
            if not self._token_ok(ip, (query.get("token") or [""])[0]):
                await self._send(writer, 403, b"Password required", "text/plain")
                return
        if path == "/api/export.csv":
            data = self.radio.memory.export_csv().encode("utf-8-sig")
            await self._send(writer, 200, data, "text/csv",
                             extra={"Content-Disposition": 'attachment; filename="andysdr_memory.csv"'})
            return
        if path.startswith("/images/"):
            await self._file(writer, self.images, path[len("/images/"):])
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
            "device": r.device_info(), "decoders": r.decoders.available(),
            "activity": sorted(r.activity.values(), key=lambda x: -x["count"])[:300],
            "images": self._images(),
            "schedule": r.scheduler.status(),
            "settings": {k: self.s[k] for k in ("scan_delay", "scan_dwell", "search_threshold_db",
                                                 "server_port", "require_password_local",
                                                 "deemphasis_us", "theme")},
            "server": {"addresses": lan_addresses(), "port": self.s["server_port"],
                       "password": self.s["password"] if self._is_local(c.peer) else "",
                       "host": socket.gethostname()},
        }
        c.push(_ws_frame(json.dumps({"type": "hello", "data": hello}).encode(), 1), droppable=False)

    def _images(self):
        out = []
        for root, _, files in os.walk(self.images):
            for f in files:
                if f.lower().endswith((".png", ".jpg")):
                    full = os.path.join(root, f)
                    out.append((os.path.getmtime(full), os.path.relpath(full, self.images).replace("\\", "/")))
        return [p for _, p in sorted(out, reverse=True)[:200]]

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
            elif cmd == "priority":
                r.set_priority(m.get("bank"), m.get("ch"))
            elif cmd == "record":
                r.record(bool(m["on"]))
            elif cmd == "decoder":
                r.set_decoder(m.get("id", ""), m.get("sub", "auto"))
            elif cmd == "device":
                r.set_device(m["key"])
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
                                                           bool(m.get("replace")), m.get("bank_name"))
                if res["talkgroups"]:
                    r.memory.import_talkgroups(res["talkgroups"])
                self._broadcast("memory", r.memory.all())
                self._reply(c, "import_result", {"added": added, "overflow": overflow,
                                                 "skipped": res["skipped"],
                                                 "talkgroups": len(res["talkgroups"])})
            elif cmd == "clear_activity":
                r.clear_activity()
            elif cmd == "audio":
                c.audio = bool(m.get("on", True))
            elif cmd == "settings":
                allowed = {"scan_delay": float, "scan_dwell": float, "search_threshold_db": float,
                           "require_password_local": bool, "deemphasis_us": int, "theme": str,
                           "password": str}
                for k, v in m.get("values", {}).items():
                    if k in allowed:
                        if k == "password" and (not self._is_local(c.peer) or len(str(v)) < 4):
                            continue
                        self.s[k] = allowed[k](v)
                self.s.save()
                if not m.get("quiet"):
                    self._reply(c, "event", {"level": "info", "text": "Settings saved"})
            elif cmd == "images":
                self._reply(c, "images", self._images())
            elif cmd == "decoders":
                self._reply(c, "decoders", r.decoders.available())
            elif cmd == "fetch_decoders":
                r.decoders.download_missing(
                    done=lambda: self._from_radio("decoders", r.decoders.available()))
            elif cmd == "auto_identify":
                r.set_auto_identify(bool(m.get("on", True)))
            elif cmd == "schedule":
                self._reply(c, "schedule", r.scheduler.status())
            elif cmd == "schedule_save":
                r.scheduler.save_job(m["job"])
            elif cmd == "schedule_delete":
                r.scheduler.delete_job(str(m["id"]))
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
