"""CHECK MY SYSTEM – a plain-English health check, and a zip of logs to send when something is wrong.

Each check gives one line:  level ("ok" / "warn" / "bad" / "info"), a title, what it found and what to do.  Nothing
is changed on the PC; the only programs run are Windows' own `sc` and `netsh` (read-only queries).  A check that cannot
run reports that and carries on, so one odd PC never stops the whole report.

The diagnostics zip holds the report, the last log files and the settings with the password (and any token) removed.
"""
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile

from .config import VERSION, data_dir

SECRET_KEYS = ("password", "token", "secret", "key")
LOG_TAIL_BYTES = 400_000


# ---------------------------------------------------------------------- helpers
def _run(args, timeout=6):
    """(returncode, text) of a small Windows query program; (None, "") when it cannot be run."""
    flags = 0x08000000 if sys.platform.startswith("win") else 0           # CREATE_NO_WINDOW
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None, ""


def item(iid, level, title, detail="", fix="", cmd="", actions=None):
    """actions: buttons the page shows under the item - [{"label": ..., "msg": {a command the page sends}}]."""
    return {"id": iid, "level": level, "title": title, "detail": detail, "fix": fix, "cmd": cmd, "actions": actions or []}


# ---------------------------------------------------------------------- the checks
def check_receiver(radio):
    out = []
    dev = radio.dev
    real = [d for d in radio.dev_list if getattr(d, "kind", "") != "sim"]
    if dev is not None and getattr(dev, "kind", "") != "sim":
        rate = getattr(dev, "sample_rate", 0) or 0
        out.append(item("rx", "ok", "Receiver: " + str(getattr(dev, "label", "?")),
                        f"Open and streaming at {rate / 1e6:.2f} MS/s." if rate else "Open."))
    elif real:
        out.append(item("rx", "warn", "A receiver was found but is not open – the simulator is running",
                        "Found: " + ", ".join(str(getattr(d, "label", "?")) for d in real) + ".",
                        "Another program (SDRuno, SDR++, SDRangel ...) may be using it – close it, then pick the receiver again "
                        "in Settings › Receivers (or unplug it and plug it back in)."))
    else:
        out.append(item("rx", "bad", "No receiver found – the simulator is running (test signals only)",
                        "AndySDR looked for SDRplay RSPs, RTL-SDR dongles (Nooelec) and SoapySDR devices.",
                        "Plug the receiver in (a USB 2.0 / 3.0 port on the PC itself, not a hub if you can), close other SDR programs "
                        "and press Find receivers in Settings › Receivers. RSP receivers need the SDRplay API; RTL dongles need the "
                        "WinUSB driver (Zadig)."))
    return out


def check_sdrplay(radio, run=_run, windows=None):
    from . import sdrplay
    windows = sys.platform.startswith("win") if windows is None else windows
    out = []
    if getattr(radio.dev, "kind", "") == "sdrplay":
        api = {"available": True, "version": None}
        try:
            api["version"] = sdrplay.api_status().get("version")
        except Exception:                                   # noqa: BLE001
            pass
    else:
        try:
            api = sdrplay.api_status()
        except Exception as e:                              # noqa: BLE001
            api = {"available": False, "error": str(e)}
    if api.get("available"):
        v = api.get("version")
        old = v is not None and v < 3.07
        out.append(item("sdrplay_api", "warn" if old else "ok",
                        "SDRplay API " + (f"{v:.2f} " if v else "") + ("is old" if old else "is installed and answering"),
                        "" if not old else "Version 3.07 or newer is needed for all the RSP features.",
                        "Install the latest SDRplay API from sdrplay.com/downloads." if old else ""))
    else:
        err = str(api.get("error", ""))
        try:
            have_dll = bool(sdrplay._load())              # the DLL is there, but the API would not open (service stopped?)
        except Exception:                                   # noqa: BLE001
            have_dll = False
        out.append(item("sdrplay_api", "info" if not windows or not have_dll else "bad",
                        "SDRplay API is not available",
                        (err[:200] if err else "") + (" (only needed for RSP1A / RSP2 / RSPdx / RSPduo receivers)" if not have_dll else ""),
                        "Install the SDRplay API (3.x) from sdrplay.com/downloads, then restart AndySDR. Do not install SDRuno's "
                        "old 2.x driver on top."))
    if windows:
        rc, txt = run(["sc", "query", "SDRplayAPIService"])
        up = txt.upper()
        if rc is None:
            out.append(item("sdrplay_service", "info", "Could not ask Windows about the SDRplay API service", "", ""))
        elif "RUNNING" in up:
            out.append(item("sdrplay_service", "ok", "SDRplay API service is running"))
        elif "1060" in txt or "does not exist" in txt.lower():
            out.append(item("sdrplay_service", "info" if not api.get("available") else "warn",
                            "The SDRplay API service is not installed",
                            "It starts the RSP receivers. Not needed for RTL / Nooelec dongles.",
                            "Install the SDRplay API from sdrplay.com/downloads."))
        elif "STOPPED" in up or "PAUSED" in up or "PENDING" in up:
            out.append(item("sdrplay_service", "bad", "The SDRplay API service is installed but not running",
                            "RSP receivers cannot open while it is stopped.",
                            "Open a Command Prompt as Administrator and run this (or restart the PC):",
                            "net start SDRplayAPIService"))
        else:
            out.append(item("sdrplay_service", "info", "SDRplay API service state unclear", txt.strip()[:160]))
    return out


def check_signal(radio):
    out = []
    try:
        b = radio.bias_snapshot()
    except Exception:                                       # noqa: BLE001
        return out
    rate = b.get("overload_rate") or 0
    if getattr(radio.dev, "kind", "") == "sim":
        return out
    if rate >= 3:
        acts = []
        try:
            for sw in radio.dev.extra_settings():
                if sw.get("key") in ("fm_notch", "dab_notch") and str(sw.get("value")).lower() in ("false", "0", ""):
                    acts.append({"label": "Turn the " + ("FM broadcast" if sw["key"] == "fm_notch" else "DAB") + " notch on",
                                 "msg": {"cmd": "device_setting", "key": sw["key"], "value": "true"}})
        except Exception:                                   # noqa: BLE001
            pass
        out.append(item("overload", "warn", f"The receiver is overloading ({rate:g} times a minute)",
                        "Strong signals (FM / DAB broadcast, a nearby transmitter) are swamping the input, which makes decoders drop out "
                        "and creates false signals across the spectrum.",
                        "Turn RF GAIN down, or on an RSP2 / RSPdx switch on the FM / DAB notch (Device settings); an LNA or the "
                        "high-gain IF setting often makes it worse." + (" The buttons below switch the notches on for you." if acts else ""),
                        actions=acts))
    else:
        out.append(item("overload", "ok", "No receiver overloads in the last minute"))
    st = b.get("state")
    if st == "drop":
        out.append(item("bias", "warn", "Bias-T power looks lost",
                        "The noise floor fell several dB below where it was, which is what happens when a powered antenna / LNA stops "
                        "getting power.",
                        "Check the antenna lead and the LNA / bias-T connection, and that bias-T is on the right antenna port "
                        "(RSP2: Antenna B only)."))
    elif st == "no_path":
        out.append(item("bias", "info", "Bias-T is on, but the selected input cannot carry it",
                        "", "On an RSP2 only Antenna B has bias-T."))
    return out


def check_decoders(radio):
    miss, have = [], 0
    for d in radio.decoders.available():
        if d["ready"]:
            have += 1
        else:
            miss.append(d["name"])
    if not miss:
        return [item("decoders", "ok", "All decoder programs are installed", f"{have} decoders ready.")]
    return [item("decoders", "warn", f"{len(miss)} decoder program{'s' if len(miss) != 1 else ''} not installed",
                 "Missing: " + "; ".join(miss[:8]) + ("; ..." if len(miss) > 8 else "") + f". ({have} ready.)",
                 "Press ⬇ Download missing decoders on the DECODERS page (free programs from GitHub; needs the internet once).")]


def _firewall_rules(run):
    rc, txt = run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in"], timeout=12)
    return rc, txt


def parse_firewall(txt, port, exe_names=("andysdr",)):
    """-> True (an enabled allow rule covers this program / port), False (rules were read but none does) or None (could not tell)."""
    rules, cur = [], {}
    for line in txt.splitlines():
        if ":" not in line:
            if cur and not line.strip():
                rules.append(cur)
                cur = {}
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower().replace(" ", "")
        if k == "rulename" and cur:
            rules.append(cur)
            cur = {}
        cur[k] = v.strip()
    if cur:
        rules.append(cur)
    if not any("rulename" in r and "action" in r for r in rules):
        return None
    def covers(r):
        prog = r.get("program", "").lower()
        ports = [p.strip() for p in r.get("localport", "").split(",")]
        if any(n in prog for n in exe_names) or r.get("rulename", "").lower().startswith("andysdr"):
            return True
        return str(port) in ports and r.get("protocol", "").lower() in ("tcp", "any")
    live = [r for r in rules if r.get("enabled", "").lower() == "yes" and covers(r)]
    if any(r.get("action", "").lower() == "block" for r in live):      # a Block rule (from clicking Cancel on Windows' question) wins
        return False
    return any(r.get("action", "").lower() == "allow" for r in live)


def check_network(settings, server_addresses, run=_run, windows=None):
    windows = sys.platform.startswith("win") if windows is None else windows
    out = []
    host, port = settings.get("server_host", "0.0.0.0"), int(settings.get("server_port", 8045))
    pw = str(settings.get("password", "") or "")
    if host in ("127.0.0.1", "localhost"):
        out.append(item("remote", "info", "Remote access is switched off",
                        f"The server only listens on this PC (address {host}:{port}), so the tablet / other PCs cannot connect.",
                        "To allow them, start AndySDR normally (or with --host 0.0.0.0)."))
        return out
    addr = ", ".join(f"http://{a}:{port}" for a in (server_addresses or [])[:3]) or f"port {port}"
    out.append(item("remote", "ok", "Remote access is on", f"Other devices on your network can open {addr}."))
    weak = len(pw) < 8 or pw.lower() in ("password", "andysdr", "12345678", "admin")
    if weak:
        out.append(item("password", "warn", "The remote password is short or easy to guess",
                        "Anyone on your Wi-Fi who guesses it can control the radio.",
                        "Set a longer one in Settings › Remote access (the generated ANDY-… password is fine)."))
    else:
        out.append(item("password", "ok", "A remote password is set"))
    if windows:
        rc, txt = _firewall_rules(run)
        if rc is None:
            out.append(item("firewall", "info", "Could not read the Windows Firewall rules", "",
                            f"If the tablet cannot connect, allow AndySDR / port {port} through the firewall."))
        else:
            res = parse_firewall(txt, port)
            if res is True:
                out.append(item("firewall", "ok", "Windows Firewall allows AndySDR in"))
            elif res is False:
                out.append(item("firewall", "warn", f"Windows Firewall may be blocking other devices from reaching port {port}",
                                "No allowing rule was found. (Windows normally asks the first time – if the question was closed, "
                                "or answered for Public networks only, other devices are blocked.)",
                                "Open a Command Prompt as Administrator and run this once (it opens the port on your private network only):",
                                f'netsh advfirewall firewall add rule name="AndySDR" dir=in action=allow protocol=TCP localport={port} profile=private'))
            else:
                out.append(item("firewall", "info", "Could not read the firewall rules (Windows in another language?)", "",
                                f"If the tablet cannot connect, allow port {port} through the firewall."))
    return out


def _newest_picture(folder):
    """(mtime, path) of the newest .png under the pictures folder, or None."""
    best = None
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".png"):
                p = os.path.join(root, f)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if best is None or m > best[0]:
                    best = (m, p)
    return best


def check_pictures(radio, now=None, images_dir=None, satdump_found=None):
    """Why are there no weather pictures?  Looks at what is switched on, the next passes, the decoder program and the
    newest picture on disk."""
    now = now or time.time()
    out = []
    sats = radio.scheduler.sats
    cfg = sats.cfg()
    on = [sid for sid, c in cfg.items() if c.get("enabled")]
    try:
        jobs = [j for j in radio.scheduler.jobs() if j.get("enabled", True)]
    except Exception:                                       # noqa: BLE001
        jobs = []
    pic_jobs = [j for j in jobs if j.get("action") in ("wefax", "lrpt", "goes", "metop", "sstv", "satdump")]
    folder = images_dir or getattr(radio.decoders, "images_dir", "")
    newest = _newest_picture(folder) if folder and os.path.isdir(folder) else None
    if newest:
        age_h = (now - newest[0]) / 3600.0
        when = time.strftime("%a %d %b %H:%M", time.localtime(newest[0]))
        out.append(item("pic_last", "ok" if age_h < 36 or not (on or pic_jobs) else "warn",
                        f"Newest picture on this PC: {when}" + (f" ({age_h / 24:.1f} days ago)" if age_h >= 36 else ""),
                        "", "" if age_h < 36 or not (on or pic_jobs) else "Pictures are expected - see the other lines in this section."))
    else:
        out.append(item("pic_last", "info", "No picture has been saved on this PC yet", "", ""))
    if not on and not pic_jobs:
        out.append(item("pic_none", "warn", "No satellite or weather-fax capture is switched on",
                        "Pictures only arrive when something is told to record them.",
                        "SCHEDULER page: tick Record next to a satellite, or add a capture."))
        return out
    if on:
        names = ", ".join(sorted(sid for sid in on))
        out.append(item("pic_sats", "ok", f"{len(on)} satellite{'s' if len(on) != 1 else ''} switched on for recording", names))
        if sats.observer() is not None:
            try:
                nxt = [p for p in sats.upcoming(now) if p["aos"] > now - 600]
            except Exception:                               # noqa: BLE001
                nxt = []
            day = [p for p in nxt if p["aos"] < now + 86400]
            if not nxt:
                out.append(item("pic_pass", "warn", "No passes in the next few days with the current settings",
                                "Min elevation, the day boxes or the date limits may rule every pass out.",
                                "SCHEDULER page: lower Min elev. (10-15° is usual), untick the day boxes, clear the dates."))
            else:
                p = nxt[0]
                out.append(item("pic_pass", "ok", f"Next pass: {p['name']} at {time.strftime('%a %H:%M', time.localtime(p['aos']))} "
                                f"(up to {p['max_el']}°)", f"{len(day)} pass{'es' if len(day) != 1 else ''} in the next 24 hours."))
                if not day:
                    out.append(item("pic_day", "info", "No pass in the next 24 hours", "Low or few passes happen some days.", ""))
        from .satpass import SATELLITES
        lrpt = [sid for sid in on if SATELLITES.get(sid, {}).get("action") in ("lrpt", "goes", "metop")]
        if lrpt and satdump_found is None:
            try:
                from .decoders import find_exe
                satdump_found = bool(find_exe("satdump", (radio.s.get("decoder_paths") if hasattr(radio, "s") else None) or {}))
            except Exception:                               # noqa: BLE001
                satdump_found = None
        if lrpt and satdump_found is False:
            out.append(item("pic_satdump", "bad", "SatDump (the satellite picture program) is not installed",
                            "Satellite recordings are saved but cannot be turned into pictures.",
                            "DECODERS page: ⬇ Download missing decoders (SatDump), or see Help > Decoders."))
    return out


def check_system(settings, radio, upd=None, dmr=None):
    out = []
    try:
        du = shutil.disk_usage(data_dir())
        free_gb = du.free / 1e9
        if free_gb < 1:
            out.append(item("disk", "bad", f"Only {free_gb:.1f} GB free on the drive AndySDR saves to",
                            "Pictures and recordings are stored here; with no space they fail silently.",
                            "Free some space, or delete old pictures / recordings in AndySDR."))
        elif free_gb < 5:
            out.append(item("disk", "warn", f"{free_gb:.1f} GB free on the drive AndySDR saves to", "", "Delete old recordings when it gets low."))
        else:
            out.append(item("disk", "ok", f"{free_gb:.0f} GB free for pictures and recordings"))
    except OSError:
        pass
    if upd is not None:
        st = upd.status()
        if st.get("state") in ("available", "downloading", "ready"):
            out.append(item("update", "info", f"AndySDR {st.get('latest')} is available (you have {st.get('current')})", "",
                            "Press Update now in the banner at the top."))
        elif st.get("state") == "error":
            out.append(item("update", "info", "Could not check for a new AndySDR", str(st.get("error", ""))[:160]))
        else:
            out.append(item("update", "ok", f"AndySDR {VERSION} is up to date"))
    try:
        sats = radio.scheduler.sats
        on = [sid for sid, c in sats.cfg().items() if c.get("enabled")]
        if on:
            if sats.observer() is None:
                out.append(item("sat_pos", "warn", "Satellite recording is on but your position is not set",
                                "Pass times cannot be worked out without it.", "SCHEDULER page: set your position (or Use GPS)."))
            else:
                from .satpass import SATELLITES
                ages = [sats.tle_age(SATELLITES[s]["norad"]) for s in on]
                ages = [a for a in ages if a is not None]
                if not ages:
                    out.append(item("sat_tle", "warn", "No satellite orbit data downloaded yet",
                                    "", "It downloads by itself when the internet is available; press Update TLEs on the SCHEDULER page."))
                elif max(ages) > 7 * 86400:
                    out.append(item("sat_tle", "warn", f"Satellite orbit data is {max(ages) / 86400:.0f} days old",
                                    "Pass times drift as it ages.", "Press Update TLEs on the SCHEDULER page (needs the internet)."))
                else:
                    out.append(item("sat_tle", "ok", "Satellite orbit data is fresh"))
    except Exception:                                       # noqa: BLE001
        pass
    if dmr is not None:
        st = dmr.status()
        if st["state"] == "ready":
            out.append(item("dmrdb", "ok", f"DMR callsign list: {st['count']:,} people"))
        elif st["state"] in ("missing", "error"):
            out.append(item("dmrdb", "info", "DMR callsign list not downloaded yet",
                            str(st.get("msg", ""))[:160], "It downloads by itself when the internet is available (DECODERS page → Update callsign list now)."))
    return out


# ---------------------------------------------------------------------- the whole report
def run_checks(radio, settings, server_addresses=None, upd=None, dmr=None, run=_run, windows=None):
    items = []
    for fn, args in ((check_receiver, (radio,)), (check_sdrplay, (radio, run, windows)), (check_signal, (radio,)),
                     (check_decoders, (radio,)), (check_pictures, (radio,)), (check_network, (settings, server_addresses, run, windows)),
                     (check_system, (settings, radio, upd, dmr))):
        try:
            items.extend(fn(*args))
        except Exception as e:                              # noqa: BLE001
            items.append(item("err_" + fn.__name__, "info", f"Could not run the {fn.__name__[6:]} check", f"{type(e).__name__}: {e}"[:200]))
    summary = {lvl: sum(1 for i in items if i["level"] == lvl) for lvl in ("ok", "warn", "bad", "info")}
    return {"items": items, "summary": summary, "when": time.time(), "version": VERSION}


# ---------------------------------------------------------------------- diagnostics zip
def redact(obj):
    if isinstance(obj, dict):
        return {k: ("(removed)" if any(s in str(k).lower() for s in SECRET_KEYS) and v else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def report_text(rep):
    sym = {"ok": "OK  ", "warn": "WARN", "bad": "BAD ", "info": "INFO"}
    lines = [f"AndySDR {rep['version']} – system check {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rep['when']))}",
             f"Windows/OS: {platform.platform()}   Python: {platform.python_version()}   {'frozen exe' if getattr(sys, 'frozen', False) else 'from source'}",
             ""]
    for i in rep["items"]:
        lines.append(f"[{sym.get(i['level'], '    ')}] {i['title']}")
        for k in ("detail", "fix", "cmd"):
            if i.get(k):
                lines.append(f"       {'run: ' if k == 'cmd' else ''}{i[k]}")
    return "\n".join(lines) + "\n"


def _tail(path, n=LOG_TAIL_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > n:
                f.seek(size - n)
                f.readline()                                # start on a whole line
            return f.read()
    except OSError:
        return b""


def build_zip(rep, settings_data, folder=None, extra=None):
    """-> bytes of a zip with the report, recent logs and the settings (secrets removed)."""
    folder = folder or data_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.txt", report_text(rep))
        z.writestr("report.json", json.dumps(rep, indent=1))
        z.writestr("settings.json", json.dumps(redact(dict(settings_data)), indent=1, default=str))
        for name, text in (extra or {}).items():
            if text:
                z.writestr(name, text)
        try:                                          # what SatDump itself said about the newest passes (why a pass made no pictures)
            found = []
            for d in os.listdir(os.path.join(folder, "images")):
                f = os.path.join(folder, "images", d, "satdump.log")
                if os.path.isfile(f):
                    found.append((os.path.getmtime(f), d, f))
            for _t, d, f in sorted(found)[-3:]:
                data = _tail(f)
                if data:
                    z.writestr("satdump/" + d + ".log", data)
        except OSError:
            pass
        logs = os.path.join(folder, "logs")
        try:
            names = sorted(os.listdir(logs))
        except OSError:
            names = []
        for n in names:
            if n.startswith(("andysdr.log", "crash")):
                data = _tail(os.path.join(logs, n))
                if data:
                    z.writestr("logs/" + n, data)
    return buf.getvalue()
