"""MeshCore messaging for AndySDR.

AndySDR's SDRs are receive-only, so sending MeshCore messages goes through a MeshCore
*companion radio* (Heltec V3/V4, RAK4631, T-Deck, T-Beam, Seeed Xiao... running the
"Companion" firmware) connected by USB, Wi-Fi (TCP) or Bluetooth LE. AndySDR acts as that
board's chat app, using the official `meshcore` Python library.

Everything runs on a private asyncio loop in its own thread; the GUI talks to it through
the server (commands "mesh_*") and gets "mesh" (status/contacts/channels) and
"mesh_msg" (one message) events back.
"""
import asyncio
import json
import logging
import os
import threading
import time

from .config import data_dir

log = logging.getLogger("andysdr.mesh")

MAX_HISTORY = 3000
# UK/EU narrow preset used by most UK MeshCore users
UK_PRESET = {"freq": 869.618, "bw": 62.5, "sf": 8, "cr": 8}


def _lib():
    import meshcore  # noqa: F401  (imported lazily so AndySDR starts without it)
    return meshcore


def available():
    try:
        _lib()
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in list_ports.comports():
        desc = " ".join(x for x in (p.description, p.manufacturer or "") if x and x != "n/a")
        out.append({"port": p.device, "desc": desc.strip() or p.device,
                    "likely": any(k in (desc + (p.hwid or "")).lower() for k in
                                  ("cp210", "ch340", "ch910", "usb serial", "jtag", "esp32", "rak", "nrf",
                                   "303a:", "10c4:", "1a86:", "239a:", "2886:"))})
    out.sort(key=lambda d: (not d["likely"], d["port"]))
    return out


class MeshLink:
    def __init__(self, settings, emit):
        self.s = settings
        self.emit = emit
        self.mc = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="meshcore", daemon=True)
        self.thread.start()
        self.status = "disconnected"   # disconnected | connecting | connected | error
        self.error = ""
        self.conn = dict(self.s.get("mesh_conn") or {})
        self.self_info = {}
        self.device_info = {}
        self.battery = None
        self.contacts = {}
        self.channels = []
        self.history_path = os.path.join(data_dir(), "meshcore_messages.jsonl")
        self.history = self._load_history()
        self._subs = []
        self._pending_acks = {}
        self._want_connected = False
        self._watchdog = None
        ok, err = available()
        log.info("MeshCore library: %s", "OK" if ok else "MISSING (" + err + ")")

    # ------------------------------------------------------------------ threading glue
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ------------------------------------------------------------------ history
    def _load_history(self):
        out = []
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out[-MAX_HISTORY:]

    def _store(self, msg):
        self.history.append(msg)
        if len(self.history) > MAX_HISTORY * 1.2:
            self.history = self.history[-MAX_HISTORY:]
            try:
                with open(self.history_path, "w", encoding="utf-8") as f:
                    for m in self.history:
                        f.write(json.dumps(m) + "\n")
            except OSError:
                pass
        else:
            try:
                with open(self.history_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(msg) + "\n")
            except OSError:
                pass
        self.emit("mesh_msg", msg)

    def clear_history(self, conv=None):
        if conv:
            self.history = [m for m in self.history if m.get("conv") != conv]
        else:
            self.history = []
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                for m in self.history:
                    f.write(json.dumps(m) + "\n")
        except OSError:
            pass
        self.emit("mesh_history", self.history)

    # ------------------------------------------------------------------ public state
    def snapshot(self):
        ok, err = available()
        si = self.self_info or {}
        return {
            "status": self.status, "error": self.error or ("" if ok else f"meshcore library missing: {err}"),
            "conn": self.conn, "library": ok,
            "node": {"name": si.get("name", ""), "public_key": si.get("public_key", ""),
                     "freq": si.get("radio_freq"), "bw": si.get("radio_bw"), "sf": si.get("radio_sf"),
                     "cr": si.get("radio_cr"), "tx_power": si.get("tx_power"),
                     "max_tx_power": si.get("max_tx_power"), "lat": si.get("adv_lat"), "lon": si.get("adv_lon"),
                     "model": self.device_info.get("model", ""), "fw": self.device_info.get("ver", "") or
                     self.device_info.get("fw_build", ""), "battery_mv": self.battery},
            "contacts": sorted(self.contacts.values(), key=lambda c: -(c.get("last_advert") or 0)),
            "channels": self.channels,
            "ports": list_serial_ports(),
            "uk_preset": UK_PRESET,
        }

    def _push(self):
        self.emit("mesh", self.snapshot())

    # ------------------------------------------------------------------ connect
    def connect(self, kind, target, pin="", remember=True):
        self.conn = {"kind": kind, "target": (target or "").strip(), "pin": (pin or "").strip(),
                     "auto": True}
        if remember:
            self.s["mesh_conn"] = self.conn
            self.s.save()
        self._want_connected = True
        self._submit(self._connect())

    def autoconnect(self):
        if self.conn.get("auto") and self.conn.get("kind") and available()[0]:
            self._want_connected = True
            self._submit(self._connect())

    def disconnect(self, forget=False):
        self._want_connected = False
        if forget:
            self.conn["auto"] = False
            self.s["mesh_conn"] = self.conn
            self.s.save()
        self._submit(self._disconnect())

    async def _disconnect(self):
        mc, self.mc = self.mc, None
        if mc:
            try:
                await mc.stop_auto_message_fetching()
            except Exception:  # noqa: BLE001
                pass
            try:
                await mc.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.status = "disconnected"
        self._push()

    async def _connect(self):
        try:
            lib = _lib()
        except Exception as e:  # noqa: BLE001
            self.status, self.error = "error", f"meshcore library missing: {e}"
            self._push()
            return
        await self._disconnect()
        self.status, self.error = "connecting", ""
        self._push()
        k, t = self.conn.get("kind"), self.conn.get("target", "")
        MeshCore, EventType = lib.MeshCore, lib.EventType
        try:
            if k == "serial":
                if not t:
                    ports = list_serial_ports()
                    if not ports:
                        raise RuntimeError("no COM ports found – plug the companion board in by USB")
                    t = ports[0]["port"]
                mc = await asyncio.wait_for(MeshCore.create_serial(t, 115200, auto_reconnect=True,
                                                                   max_reconnect_attempts=10), 20)
            elif k == "tcp":
                host, _, port = t.partition(":")
                mc = await asyncio.wait_for(MeshCore.create_tcp(host, int(port or 5000), auto_reconnect=True,
                                                                max_reconnect_attempts=10), 20)
            elif k == "ble":
                kw = {"pin": self.conn["pin"]} if self.conn.get("pin") else {}
                mc = await asyncio.wait_for(MeshCore.create_ble(t or None, **kw), 45)
            else:
                raise RuntimeError(f"unknown connection type {k!r}")
            if mc is None:
                raise RuntimeError("the board did not answer – is it running MeshCore Companion firmware "
                                   "(USB/BLE/Wi-Fi companion, not Repeater or Room Server)?")
        except Exception as e:  # noqa: BLE001
            self.status, self.error = "error", f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            log.warning("MeshCore connect failed: %s", self.error)
            self._push()
            self._schedule_retry()
            return

        self.mc = mc
        self._wire_events(mc, EventType)
        try:
            if not mc.self_info:
                await mc.commands.send_appstart()
            self.self_info = dict(mc.self_info or {})
            r = await mc.commands.send_device_query()
            if r.type != EventType.ERROR:
                self.device_info = dict(r.payload or {})
            # keep the board's clock right (messages are timestamped by it)
            try:
                await mc.commands.set_time(int(time.time()))
            except Exception:  # noqa: BLE001
                pass
            await self._load_contacts()
            await self._load_channels()
            await self._battery()
            await mc.start_auto_message_fetching()
            await self._drain()
        except Exception as e:  # noqa: BLE001
            log.exception("MeshCore setup")
            self.error = f"connected, but setup failed: {e}"
        self.status = "connected"
        n = self.self_info.get("name", "?")
        self.emit("event", {"level": "info", "text": f"MeshCore: connected to {n} via {k} {t}".strip()})
        self._push()
        if not self._watchdog:
            self._watchdog = asyncio.ensure_future(self._watch())

    def _schedule_retry(self):
        if self._want_connected and self.conn.get("kind") in ("serial", "tcp"):
            self.loop.call_later(20, lambda: self._want_connected and self.status != "connected"
                                 and asyncio.ensure_future(self._connect()))

    async def _watch(self):
        """Battery every 2 min, contacts every 10 min, reconnect if the link drops."""
        n = 0
        while True:
            await asyncio.sleep(60)
            n += 1
            mc = self.mc
            if not self._want_connected:
                continue
            if mc is None or not mc.is_connected:
                if self.status == "connected":
                    self.status = "error"
                    self.error = "link lost – reconnecting"
                    self._push()
                    await self._connect()
                continue
            try:
                if n % 2 == 0:
                    await self._battery()
                    self._push()
                if n % 10 == 0:
                    await self._load_contacts()
                    self._push()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ device data
    async def _load_contacts(self):
        lib = _lib()
        r = await self.mc.commands.get_contacts()
        if r.type == lib.EventType.ERROR:
            return
        self.contacts = {k: self._contact(c) for k, c in (r.payload or {}).items()}

    @staticmethod
    def _contact(c):
        kinds = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}
        return {"key": c.get("public_key", ""), "name": c.get("adv_name", "") or c.get("public_key", "")[:12],
                "type": kinds.get(c.get("type"), "node"), "last_advert": c.get("last_advert", 0),
                "lat": c.get("adv_lat", 0), "lon": c.get("adv_lon", 0),
                "hops": c.get("out_path_len", -1)}

    async def _load_channels(self):
        lib = _lib()
        mx = int(self.device_info.get("max_channels") or 8)
        chans = []
        for i in range(min(mx, 40)):
            try:
                r = await self.mc.commands.get_channel(i)
            except Exception:  # noqa: BLE001
                break
            if r.type == lib.EventType.ERROR:
                break
            p = r.payload or {}
            name = (p.get("channel_name") or "").strip("\x00 ")
            secret = p.get("channel_secret") or b""
            if isinstance(secret, str):
                secret = bytes.fromhex(secret) if all(ch in "0123456789abcdefABCDEF" for ch in secret) else b""
            if not name and not any(secret):
                continue
            chans.append({"idx": i, "name": name or f"Channel {i}"})
        self.channels = chans

    async def _battery(self):
        try:
            r = await self.mc.commands.get_bat()
            if r.type != _lib().EventType.ERROR:
                self.battery = (r.payload or {}).get("level")
        except Exception:  # noqa: BLE001
            pass

    async def _drain(self):
        lib = _lib()
        for _ in range(200):
            r = await self.mc.commands.get_msg()
            if r.type in (lib.EventType.NO_MORE_MSGS, lib.EventType.ERROR):
                break

    def _wire_events(self, mc, EventType):
        async def on_dm(ev):
            p = ev.payload or {}
            pre = p.get("pubkey_prefix", "")
            c = next((c for k, c in self.contacts.items() if k.startswith(pre)), None)
            if c is None and mc.get_contact_by_key_prefix(pre):
                c = self._contact(mc.get_contact_by_key_prefix(pre))
                self.contacts[c["key"]] = c
            key = c["key"] if c else pre
            self._store({"id": f"r{time.time_ns()}", "conv": f"c:{key}", "dir": "in",
                         "from": c["name"] if c else pre, "text": p.get("text", ""),
                         "ts": p.get("sender_timestamp") or int(time.time()), "rx": int(time.time()),
                         "hops": p.get("path_len"), "snr": p.get("SNR") or p.get("snr")})

        async def on_chan(ev):
            p = ev.payload or {}
            text = p.get("text", "")
            sender = ""
            if ": " in text[:40]:  # channel texts are sent as "Name: message"
                sender, text = text.split(": ", 1)
            self._store({"id": f"r{time.time_ns()}", "conv": f"ch:{p.get('channel_idx', 0)}", "dir": "in",
                         "from": sender or "?", "text": text,
                         "ts": p.get("sender_timestamp") or int(time.time()), "rx": int(time.time()),
                         "hops": p.get("path_len"), "snr": p.get("SNR") or p.get("snr")})

        async def on_ack(ev):
            code = (ev.payload or {}).get("code", "")
            mid = self._pending_acks.pop(code, None)
            if mid:
                for m in reversed(self.history):
                    if m.get("id") == mid:
                        m["status"] = "delivered"
                        self.emit("mesh_msg", m)
                        break

        async def on_contact(ev):
            c = self._contact(ev.payload or {})
            if c["key"]:
                new = c["key"] not in self.contacts
                self.contacts[c["key"]] = c
                if new:
                    self.emit("event", {"level": "info", "text": f"MeshCore: new node heard – {c['name']}"})
                self._push()

        async def on_advert(ev):
            # an existing contact re-advertised: refresh its "last heard"
            try:
                await self._load_contacts()
                self._push()
            except Exception:  # noqa: BLE001
                pass

        async def on_disc(ev):
            if self.status == "connected":
                self.status = "error"
                self.error = "link lost – " + str((ev.payload or {}).get("reason", "reconnecting"))
                self._push()

        async def on_conn(ev):
            if self.mc is mc and self.status != "connected":
                self.status, self.error = "connected", ""
                self._push()

        self._subs = [
            mc.subscribe(EventType.CONTACT_MSG_RECV, on_dm),
            mc.subscribe(EventType.CHANNEL_MSG_RECV, on_chan),
            mc.subscribe(EventType.ACK, on_ack),
            mc.subscribe(EventType.NEW_CONTACT, on_contact),
            mc.subscribe(EventType.ADVERTISEMENT, on_advert),
            mc.subscribe(EventType.DISCONNECTED, on_disc),
            mc.subscribe(EventType.CONNECTED, on_conn),
        ]

    # ------------------------------------------------------------------ actions
    def send(self, conv, text):
        text = (text or "").strip()
        if not text:
            return
        self._submit(self._send(conv, text[:200]))

    async def _send(self, conv, text):
        lib = _lib()
        mid = f"s{time.time_ns()}"
        me = (self.self_info or {}).get("name", "me")
        msg = {"id": mid, "conv": conv, "dir": "out", "from": me, "text": text,
               "ts": int(time.time()), "rx": int(time.time()), "status": "sending"}
        if not self.mc or self.status != "connected":
            msg["status"] = "failed – not connected"
            self._store(msg)
            return
        self._store(msg)
        try:
            if conv.startswith("ch:"):
                r = await self.mc.commands.send_chan_msg(int(conv[3:]), text)
                msg["status"] = "failed" if r.type == lib.EventType.ERROR else "sent"
            else:
                key = conv[2:]
                dst = (self.mc.contacts or {}).get(key) or key  # contact dict or hex public key
                r = await self.mc.commands.send_msg(dst, text)
                if r.type == lib.EventType.ERROR:
                    msg["status"] = f"failed ({r.payload})"
                else:
                    msg["status"] = "sent"
                    ack = (r.payload or {}).get("expected_ack")
                    if ack:
                        self._pending_acks[ack.hex() if isinstance(ack, (bytes, bytearray)) else str(ack)] = mid
        except Exception as e:  # noqa: BLE001
            msg["status"] = f"failed ({e})"
        self.emit("mesh_msg", msg)

    def advert(self, flood=True):
        async def go():
            if self.mc:
                await self.mc.commands.send_advert(flood=flood)
                self.emit("event", {"level": "info", "text": "MeshCore: advert sent" + (" (flood)" if flood else "")})
        self._submit(go())

    def refresh(self):
        async def go():
            if self.mc and self.status == "connected":
                await self._load_contacts()
                await self._load_channels()
                await self._battery()
            self._push()
        self._submit(go())

    def set_name(self, name):
        async def go():
            if self.mc and name:
                await self.mc.commands.set_name(name[:31])
                await self.mc.commands.send_appstart()
                self.self_info = dict(self.mc.self_info or {})
                self._push()
        self._submit(go())

    def set_radio(self, freq, bw, sf, cr):
        async def go():
            if self.mc:
                r = await self.mc.commands.set_radio(float(freq), float(bw), int(sf), int(cr))
                await self.mc.commands.send_appstart()
                self.self_info = dict(self.mc.self_info or {})
                self.emit("event", {"level": "info" if r.type != _lib().EventType.ERROR else "error",
                                    "text": f"MeshCore radio set to {freq} MHz BW {bw} kHz SF{sf} CR{cr}"})
                self._push()
        self._submit(go())

    def add_channel(self, idx, name, secret_hex=""):
        """Join a channel: '#name' hashtag channels derive their key from the name, as the MeshCore apps do."""
        async def go():
            if not self.mc:
                return
            import hashlib
            nm = name.strip()
            if secret_hex:
                secret = bytes.fromhex(secret_hex.strip())[:16]
            elif nm.startswith("#"):
                secret = hashlib.sha256(nm.encode()).digest()[:16]
            else:
                self.emit("event", {"level": "error", "text": "MeshCore: a private channel needs its 32-hex-digit key"})
                return
            await self.mc.commands.set_channel(int(idx), nm, secret)
            await self._load_channels()
            self._push()
        self._submit(go())

    def remove_channel(self, idx):
        async def go():
            if self.mc:
                await self.mc.commands.set_channel(int(idx), "", bytes(16))
                await self._load_channels()
                self._push()
        self._submit(go())

    def scan_ble(self):
        async def go():
            found = []
            try:
                from bleak import BleakScanner
                devs = await BleakScanner.discover(timeout=6)
                for d in devs:
                    if d.name and d.name.lower().startswith("meshcore"):
                        found.append({"address": d.address, "name": d.name})
            except Exception as e:  # noqa: BLE001
                self.emit("event", {"level": "error", "text": f"Bluetooth scan failed: {e}"})
            self.emit("mesh_ble", found)
        self._submit(go())

    def stop(self):
        self._want_connected = False
        try:
            self._submit(self._disconnect()).result(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
